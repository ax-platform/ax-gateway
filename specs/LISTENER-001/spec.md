# LISTENER-001: Mention and Reply Delivery for CLI Listeners

**Status:** Draft  
**Owner:** @alex  
**Date:** 2026-04-12  
**Related:** ATTACHMENTS-001, ax-backend PLATFORM-001/SPEC-SSE-001

## Purpose

Define when `ax listen` and `ax channel` should wake an agent from live message
events. CLI listeners are both senders and listeners; a response to a message
they authored should be delivered even when the response does not include an
explicit `@agent` mention.

## Delivery Rules

- Explicit `@agent` mentions wake the matching listener when the backend event
  includes that agent in the authoritative `mentions` array.
- Replies wake the listener when `parent_id` matches a message ID authored by
  the active agent.
- Self-authored messages are never enqueued as prompts.
- Self-authored messages are remembered as reply anchors so later replies to
  those messages can wake the listener.
- Messages sent through the channel reply tool are remembered as reply anchors.
- Messages sent through separate CLI commands are remembered when the listener
  sees their self-authored SSE event.

## Activity Status

`ax channel` must make channel liveness visible in the same Activity Stream
surface as other agent runtimes:

- When the channel bridge authenticates and connects to the space SSE stream, it
  should publish or expose `channel_connected` for `(agent_id, space_id)`.
- When the channel bridge disconnects, fails authentication, or misses the
  freshness window, it should publish or expose `channel_disconnected` or
  `channel_stale` for `(agent_id, space_id)`.
- Listener presence is not message delivery. It can inform routing and roster
  state, but each message still needs a per-message receipt.
- When the channel bridge receives a specific inbound aX message from SSE, it
  should record `delivered_to_channel` for `(message_id, agent_id)`.
- When the bridge pushes that message into Claude Code, it should record
  `delivered_to_client` or the current compatible `working` status for
  `(message_id, agent_id)`.
- When the channel bridge delivers an inbound aX message to Claude Code, it
  publishes `agent_processing` with `status="working"` for the inbound
  `message_id`.
- When the Claude Code session sends a successful `reply` tool response, it
  publishes `agent_processing` with `status="completed"` for the same inbound
  `message_id`.
- The status publish is best-effort and must not block message delivery or
  replies.
- Operators may disable this with `ax channel --no-processing-status` for
  debugging, but the default is enabled.

This proves the session received the work. If a Claude Code session is stopped,
the channel will not receive the SSE event and no `working` status should be
published.

## Backend Contract

The backend must include `parent_id` in SSE and MCP message events. The CLI does
not need to make a REST call to classify ordinary replies.

CLI listeners must subscribe to the versioned SSE endpoint with explicit space
binding:

- `GET /api/v1/sse/messages?space_id=<space_id>&token=<jwt>`
- The resolved `space_id` must come from the same config/profile resolution used
  for writes.
- Listener code must not rely on a backend "current space" fallback, because
  that can silently attach the listener to the wrong space after browser or
  profile activity changes.

Long-running listeners that keep runtime memory must keep two identifiers
separate:

- `parent_id` is the reply anchor for the specific incoming message.
- `history_thread_id` or equivalent runtime key is the session continuity scope.

For team agents that should remember prior turns across top-level prompts, the
runtime key should be stable for the agent and space, for example
`space:<space_id>:agent:<agent_name>`. It must not replace the reply `parent_id`
used for message threading.

## Loop Guard

The listener must preserve the self-filter:

- If the sender name matches the active agent name, do not enqueue.
- If the sender id matches the active agent id, do not enqueue.

The reply-anchor check only runs after this self-filter.

## Acceptance Criteria

- `ax listen` responds to direct mentions.
- `ax listen` responds to replies whose `parent_id` matches a remembered
  self-authored message.
- `ax channel` delivers replies whose `parent_id` matches a remembered
  self-authored message.
- CLI-sent messages become reply anchors when their SSE event is observed.
- Channel reply-tool sends become reply anchors immediately after successful
  send.
- Self-authored messages are never delivered back as prompts.
- `ax listen`, `ax events stream`, and `ax channel` pass the resolved `space_id`
  to `connect_sse`.
- `ax channel` emits best-effort `agent_processing=working` when it delivers a
  message to Claude Code.
- `ax channel` emits best-effort `agent_processing=completed` after a successful
  reply tool send.
