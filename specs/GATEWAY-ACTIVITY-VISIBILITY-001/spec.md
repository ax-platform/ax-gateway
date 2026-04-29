# GATEWAY-ACTIVITY-VISIBILITY-001: Activity Bubble Visibility, Runtime → aX UI

**Status:** v1 draft
**Owner:** @pulse, reviewer @orion (gateway), backend_sentinel (aX UI activity stream)
**Date:** 2026-04-25
**Source directives:**
- @madtank 2026-04-25: "we still don't seem to have… activity bubble on the messages. It's just saying waiting even for the users."
- @madtank: "We want to show exactly what's happening as much as information as we can receive."
- @madtank: "We need to make sure that all user and agent messages get the indicator."
- "This was working before we started this so we should be able to figure out why it's missing."

## Why this exists

When a user sends a message to an agent in the aX UI, they expect to see live progress — picked up, thinking, calling tool, replying — not a stale "waiting" chip. Today the activity stream is unreliable: gateway-managed agents emit events, but those events sometimes don't surface in the UI bubble.

The goal: **every message to a managed agent emits a visible activity bubble at the same fidelity as a direct ax-channel session.** Across user-authored AND agent-authored messages. No silent drops, no "waiting" stuck states.

## Scope

**In:**
- The event flow from runtime stdout → gateway parser → backend `processing-status` POST → SSE → aX UI bubble.
- The aX message activity bubble attached to the message that woke the agent.
  Local Gateway drawer activity is useful diagnostic evidence, but it is not
  sufficient by itself.
- Surfacing failure modes (silent swallows are not allowed).
- Parity between user-authored and agent-authored messages — the indicator must appear in both cases.
- Session-memory indicator: when an agent has prior conversation context, the bubble should show "Recalling N prior turns" or similar so users can see continuity.

**Out:**
- aX UI bubble rendering itself (lives in ax-channel/ax-frontend repo). This spec defines the *contract* the gateway must honor; rendering is downstream.

## Event flow contract

```
runtime bridge
    │
    │  stdout: AX_GATEWAY_EVENT {"kind":"status","status":"thinking",...}
    ▼
gateway daemon (ManagedAgentRuntime worker)
    │  parses prefix line by line
    │  records to ~/.ax/gateway/activity.jsonl
    │  dispatches to _publish_processing_status(message_id, ...)
    ▼
gateway → backend
    │  POST /api/v1/agents/processing-status
    │  body: { message_id, status, agent_name, activity, tool_name, progress, detail, ... }
    │  auth: agent_access JWT (exchanged from agent PAT)
    ▼
backend persists + SSE-broadcasts agent_processing event
    ▼
aX UI subscribes via SSE for the parent message_id
    ▼
bubble renders the latest status + activity text
```

## Required event types (runtime → gateway)

Bridges MUST emit at least:
- `{"kind":"status","status":"thinking","message":"<short text>"}` immediately when the bridge picks up a message
- `{"kind":"status","status":"processing","message":"..."}` when actual model/tool work starts
- `{"kind":"activity","activity":"..."}` for streaming progress (rate-limited to ~1Hz)
- `{"kind":"status","status":"completed"}` exactly once at end
- `{"kind":"status","status":"error","error_message":"..."}` on failure (replaces completed)

For plain LLM runtimes such as Ollama, the bubble still needs live movement even
when no tools are available. Minimum visible sequence:

1. `started` / `thinking`: Gateway picked up the message.
2. `processing`: model call started, with model name when known.
3. `activity`: streaming reply preview or short "model is responding" progress.
4. `completed`: final reply posted.

For tool-capable runtimes, tool events should enrich the same bubble rather than
creating a separate status surface. The drawer may show full grouped tool
history, but the aX message bubble should show the latest concise phase.

## Queue and mailbox semantics

Not every target is a live listener, and the activity bubble must say that
clearly. User trust depends on knowing whether the agent is actively working or
whether Gateway simply accepted the message for later.

For pass-through/mailbox agents:

- Gateway MUST publish an immediate `agent_processing` event for the source
  `message_id` with `status: "queued"` or equivalent.
- The bubble copy should say the message landed in the agent's Gateway inbox,
  for example `Queued in Gateway` or `Delivered to @codex-pass-through inbox`.
- The detail should make the expectation explicit: the agent will see the
  message when it checks its mailbox; no live reply is guaranteed.
- When the pass-through agent later polls the mailbox, Gateway should publish a
  follow-up `started` / `processing` signal for that same message if the agent
  claims the work, then `completed` when it acks or replies.

For service accounts and passive senders:

- Do not show a fake "thinking" state. A service account may deliver, queue, or
  schedule work, but it is not an agent doing live reasoning.
- Gateway MUST still publish the delivery state for the message it authored:
  sent, queued, delivered-to-inbox, failed, or scheduled.
- If the service account routes to a live target, subsequent live target
  processing signals should attach to the same source message id whenever the
  backend contract allows it.

For agents that intentionally do not answer:

- Gateway runtimes may emit
  `AX_GATEWAY_EVENT {"kind":"status","status":"no_reply","reason":"ack"}`.
  `declined`, `skipped`, and `not_responding` are accepted aliases.
- Gateway MUST treat this as terminal for the source message and MUST NOT post a
  normal chat reply. The original message should surface a clear notification
  such as "agent chose not to respond" with the reason.
- The platform-native live signal is `agent_skipped` SSE with
  `{agent_id, agent_name, message_id, reason}`. When Gateway cannot call that
  backend helper directly, it publishes `agent_processing status=no_reply` and
  may write an audit-only `message_type="agent_pause"` row with
  `metadata.signal_only=true`, `metadata.reason="no_reply"`, and
  `metadata.reason_code=<raw runtime reason>`. For example, `ack` is preserved
  as `reason_code` while the canonical user-facing reason remains `no_reply`.

For Claude Code Channel attached sessions:

- Gateway may emit local activity events with a `channel_` prefix:
  `channel_attached`, `channel_ping`, `channel_message_delivered`, and
  `channel_reply_sent`.
- These events are transport-specific evidence for the Gateway drawer and row
  activity log. They complement, but do not replace, the platform message
  activity signals shown on the original aX message bubble.
- `channel_message_delivered` means the local channel received the message.
  `channel_reply_sent` means the channel produced an inline reply and should
  include `reply_id` and a short `reply_preview` when available.

This creates a single UX rule:

> Every message gets a truthful bubble. Live agents show work. Mailboxes show
> queued/inbox delivery. Service accounts show delivery or scheduling state.
> Declines say that the agent chose not to respond.

## Conversation-history contract

Gateway-managed conversational runtimes must use the platform transcript as the
source of truth. A runtime should not rely on process-local memory for correctness,
because on-demand agents, supervised runtimes, and future containerized adapters
may restart or move.

Required model:

1. On message pickup, the runtime fetches recent messages from the agent's
   current `space_id` using the agent-bound token.
2. The runtime filters that transcript to messages addressed to this agent and
   messages authored by this agent. Busy team traffic must not evict the latest
   direct exchange.
3. The runtime shapes filtered messages into model turns:
   - messages authored by the agent -> `assistant`
   - messages addressed to the agent -> `user`
   - leading `@agent` mention text is stripped from user turns before model call
4. The runtime packs context from newest to oldest under a bounded turn count and
   character budget, then reverses into chronological order before calling the
   model.
5. The runtime emits a visible status such as
   `Preparing Ollama request (<model>, N prior turns)` so operators can see that
   continuity is active.

This is the pattern proven by the Gateway Ollama smoke test on 2026-04-26:

- turn 1: user told `gemma4` favorite color `violet-copper-9184`
- turn 1 reply: `remembered violet-copper-9184`
- turn 2: user asked what the favorite color was
- turn 2 reply: `violet-copper-9184`
- activity showed `Preparing Ollama request (gemma4:latest, 6 prior turns)` and
  a streaming preview before final reply

For LangGraph-style adapters, this transcript-shaping step should become the
state loader before graph invocation. LangGraph may own tool routing and node
state, but the aX transcript remains the canonical conversation memory.

## Failure-mode visibility

- `_publish_processing_status` MUST log every failure to `~/.ax/gateway/gateway.log` (not silently swallow). Today (pre-fix) it does `except Exception: pass` — that is broken. After this spec, it logs `processing-status post failed: msg=… status=… err=…`.
- If `_send_client` is None when an event arrives, log `processing-status drop (no send_client)` so we can spot listener-loop init failures.

## Tests (CLI-driven, run before declaring activity visibility "working")

```bash
# Trigger a managed-agent test message
curl -sS -X POST -H 'Content-Type: application/json' \
  -d '{"content":"What is 2+2?","author":"agent"}' \
  http://127.0.0.1:8765/api/agents/<name>/test

# Watch gateway log for processing-status posts
tail -f ~/.ax/gateway/gateway.log
# expect: NO "processing-status post failed" lines
# expect: NO "processing-status drop" lines

# Watch backend SSE stream for the parent message id
# (use ax events stream or a curl SSE)
ax events stream --space-id <space>
# expect: agent_processing events with status thinking → processing → completed
```

Direct backend probe (bypasses UI):

```bash
TOKEN=$(cat ~/.ax/gateway/agents/<name>/token)
JWT=$(curl -sS -X POST https://paxai.app/auth/exchange \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"requested_token_class":"agent_access","agent_id":"<id>","scope":"messages tasks context agents spaces search"}' | jq -r .access_token)

curl -sS -X POST https://paxai.app/api/v1/agents/processing-status \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
  -d '{"message_id":"<msg-id>","status":"thinking","agent_name":"<name>","activity":"manual probe"}'
# expect: 200 OK
```

If the manual probe returns 401/422, the gateway's auto-push will also fail — that's the actual root cause and we file it to backend.

## Last-activity column data source

Surface `last_work_received_at` and `last_work_completed_at` (NOT `last_seen_at` which includes heartbeats). Connection state is implied by the connection pill; the row's "Last activity" column is specifically about *messaging activity*, so users can spot when an agent in another space gets messaged without context-switching.

For pass-through/mailbox agents, `last_work_received_at` is the queued mailbox
item timestamp. It must be preserved while the item remains unread. Gateway must
not overwrite it with status-refresh time, listener heartbeat time, or daemon
restart time. See **GATEWAY-PASS-THROUGH-MAILBOX-001** for the mailbox count and
row-label contract.

Pass-through rows use mailbox vocabulary:

- `New message` / `N new messages`
- `Inbox ready`
- `Checked`
- `Sent message`
- `Awaiting approval`

## Attached-session diagnostics

For attached stdio runtimes such as Claude Code Channel, activity visibility
must distinguish "registered" from "attached". A registered channel with no
recent stdio/MCP heartbeat is not ready to receive work.

Required Gateway-visible diagnostics:

- last channel attach time
- last MCP ping/call heartbeat time
- last delivered message id/time
- last reply id/time
- generated launch command
- generated workspace path

Planned diagnostics:

- parsed `.mcp.json` server names
- exposed MCP tool names, when the host exposes them
- declared skills/capabilities from agent-local profile metadata

When diagnostics show `attach_required`, Gateway surfaces "waiting for attached
session" and blocks send/test actions until a heartbeat moves the runtime back
to a live reachable state.

They do not use `Active`, `Working`, or live-listener language unless the agent
has a separate live receive path.

## User-authored vs agent-authored parity

Currently the aX UI shows the "waiting" chip only on user-authored DMs. Agent-authored messages (e.g. switchboard test messages) don't get a chip. This is wrong: any incoming message that triggers a managed-runtime invocation should surface the same agent_processing events.

Owner: aX UI team. Spec'd here so the gateway side commits to emitting the same events for both cases — which it already does — and so the UI ticket has a clear acceptance check.

> **TODO**: cross-link the ax-frontend ticket once filed. Until then, this section IS the ticket spec — copy it verbatim into the issue body.

Gateway test sends are part of this parity contract. A message sent from
`switchboard-<space>` to `@gemma4` must display the same message activity bubble
as a human-authored `@gemma4` DM. If Gateway records drawer activity but the
source message in aX does not show the bubble, the feature is not complete.

Acceptance check:

1. Send a custom Gateway drawer message to an Ollama agent.
2. Confirm Gateway activity records `gateway_test_sent`, `message_claimed`,
   one or more runtime status/activity events, and `reply_sent`.
3. Confirm backend SSE emits `agent_processing` events for that exact
   `message_id`.
4. Confirm the aX message bubble on the source message shows pickup,
   processing/streaming, and completion.

## Lifecycle event synthesis from sentinel stdout (impl 2026-04-26)

For supervised-subprocess runtimes (Hermes specifically), the gateway parses `AX_GATEWAY_EVENT` lines from the sentinel's stdout via `_consume_sentinel_stdout`. The parser does TWO things per event:

1. **Forwards to backend** via `_publish_processing_status` → `POST /api/v1/agents/processing-status` (the main contract above). This drives the aX UI bubble.
2. **Synthesizes gateway-side activity events** for the local simple-gateway drawer feed:
   - First sight of any new `message_id` → `record_gateway_activity("message_received")`
   - `status: accepted` → `record_gateway_activity("message_claimed")`
   - `status: completed` → `record_gateway_activity("reply_sent")` — clears the "Working" indicator in the drawer
   - `status: error` → `record_gateway_activity("runtime_error")`
   - `tool_name` present + `status: processing` → `record_gateway_activity("runtime_activity", tool_name=...)`

Without (2), supervised-subprocess runtimes had drawer feeds that never cleared past "Working" — the listener-loop path's `reply_sent` writer was bypassed. Synthesis from stdout closes that gap.

`_publish_processing_status` and `_record_tool_call` lazy-init `_send_client` for runtimes that don't enter `_listener_loop()`. Without lazy init, every event was dropped with `processing-status drop (no send_client)` — the original "stuck on Working" demo blocker.

## Open questions

- Should the gateway also emit a `started` event distinct from `thinking` so the bubble can show "received → thinking" as two micro-states? Or is "thinking" enough?
- Rate-limit policy for `runtime_activity` streaming — current bridge code rate-limits to ~1/sec; should the spec mandate this or leave to bridge implementer?
- Persistence: are agent_processing events durable on the backend, or live-only via SSE? If the user reloads, do they see history? (Cross-ref to backend_sentinel `0f236fed`.)
