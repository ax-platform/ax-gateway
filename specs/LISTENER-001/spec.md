# LISTENER-001: Mention and Reply Delivery for CLI Listeners

**Status:** Draft  
**Owner:** @madtank  
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

## Backend Contract

The backend must include `parent_id` in SSE and MCP message events. The CLI does
not need to make a REST call to classify ordinary replies.

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

