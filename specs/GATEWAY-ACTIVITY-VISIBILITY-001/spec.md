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

## User-authored vs agent-authored parity

Currently the aX UI shows the "waiting" chip only on user-authored DMs. Agent-authored messages (e.g. switchboard test messages) don't get a chip. This is wrong: any incoming message that triggers a managed-runtime invocation should surface the same agent_processing events.

Owner: aX UI team. Spec'd here so the gateway side commits to emitting the same events for both cases — which it already does — and so the UI ticket has a clear acceptance check.

## Open questions

- Should the gateway also emit a `started` event distinct from `thinking` so the bubble can show "received → thinking" as two micro-states? Or is "thinking" enough?
- Rate-limit policy for `runtime_activity` streaming — current bridge code rate-limits to ~1/sec; should the spec mandate this or leave to bridge implementer?
- Persistence: are agent_processing events durable on the backend, or live-only via SSE? If the user reloads, do they see history? (Cross-ref to backend_sentinel `0f236fed`.)
