# GATEWAY-RUNTIME-PERSISTENCE-001: Persistent Runtime Model for Conversational Agents

**Status:** v1 draft
**Owner:** @pulse, reviewer @orion
**Date:** 2026-04-25
**Source directives:**
- @madtank 2026-04-25: "I think what's happening is your invoking them like a individual call but I think we need to keep them running and we need to keep them available. That way when we send a message they're already running. They're already working and they hold the session context."
- @madtank: "We need to make sure that an agent at least be able to hold a conversation."

## Current PR boundary

This spec describes the **next persistent-runtime step**, not the current PR's
Ollama implementation.

Current PR behavior:

- Ollama remains `intake_model=launch_on_send`.
- Gateway launches `examples/gateway_ollama/ollama_bridge.py` per message.
- The bridge reconstructs conversation context from recent aX transcript
  history using the agent's managed token.
- The bridge emits pickup, request-preparation, streaming-preview, completion,
  and reply activity.

Follow-up behavior in this spec:

- promote conversational runtimes such as Ollama to a long-running listener
  option;
- keep in-process per-thread memory while still treating aX transcript history
  as canonical on cold start;
- add heartbeat/restart/runtime-log commands for the persistent bridge.

## Vocabulary alignment

This spec describes the follow-up transition from `intake_model=launch_on_send`
to `intake_model=live_listener` for conversational runtimes. Older notes used
`connection_mode`; current Gateway templates expose `intake_model`, `placement`,
and `activation`, with derived `Mode + Presence + Reply + Confidence` for
display. Do not introduce a second competing status vocabulary.

## Why this exists

The Ollama runtime today is `intake_model: launch_on_send`. Every incoming message:
1. cold-launches `python3 examples/gateway_ollama/ollama_bridge.py` as a subprocess
2. runs one Ollama generate call
3. exits

Effects:
- **No in-memory session context.** Each call is fresh. The current bridge
  fetches recent messages from aX and shapes them into model history, so
  conversation continuity works, but it is reconstructed per call.
- **Cold-start latency** every message (Python startup, Ollama load, reconnect to aX).
- **Activity bubbles look choppy** because the runtime isn't continuously emitting events between messages.
- **Doesn't match the user's mental model.** Users expect "I started this agent, it's running, it remembers what we talked about" — like Hermes or Claude.

We want a follow-up option where Ollama and other conversational runtimes can be
**persistent**: a long-lived listener process that holds session memory in
process, subscribes to incoming work via SSE, and replies inline.

## Scope

**In:**
- A new `connection_mode: live_listener` profile for Ollama (and other conversational runtimes).
- The persistent bridge subscribes to its own SSE stream, holds an in-memory message history, and serves multiple turns without restarting.
- Heartbeats from the persistent bridge so the gateway can detect crash/hang.
- An auto-restart supervisor: if the bridge crashes, gateway restarts it with the agent's last persisted state.
- Multi-thread awareness: the bridge keeps separate histories per `parent_id` thread, so conversations from different users don't bleed together.

**Out:**
- Multi-process scheduling / GPU contention (one Ollama runtime per agent for now).
- Distributed runtimes (everything runs on the user's local machine).
- Migrating Hermes (it's already persistent — `hermes_sentinel` runtime).

## Architecture

```
┌──────────────────────────┐         ┌────────────────────────────────────┐
│  ax gateway daemon       │         │  ollama_persistent_bridge.py        │
│                          │         │    (one subprocess per agent)       │
│  Reconcile loop:         │  spawn  │                                    │
│   desired_state=running  ├────────►│  ┌──────────────────────────────┐ │
│   intake_model=live_*    │         │  │ subscribe SSE for agent      │ │
│                          │         │  │ in-memory: thread_id → []    │ │
│  Health check:           │  ping   │  │ on message:                  │ │
│   heartbeat poll         │◄────────┤  │   load thread history (mem)  │ │
│                          │         │  │   call ollama /api/chat      │ │
│   restart on crash       │         │  │   stream events to gateway   │ │
│                          │         │  │   append reply to history    │ │
└──────────────────────────┘         │  └──────────────────────────────┘ │
                                     └────────────────────────────────────┘
```

## Lifecycle

| Event                           | Effective state    |
|---------------------------------|--------------------|
| `desired_state=running`, no proc | gateway spawns bridge |
| Bridge running, message arrives  | bridge handles inline; no spawn |
| Bridge dies / hangs > 30s no heartbeat | gateway respawns |
| `desired_state=stopped`          | gateway sends SIGTERM, bridge exits |
| Operator removes agent           | gateway SIGTERM + cleanup |

## Session memory

- Stored in bridge process memory, keyed by `(thread_id, agent_name)`.
- Sliding window: last 20 turns OR 12,000 chars per thread, whichever comes first.
- Persisted to disk on graceful shutdown so a restart resumes context (`~/.ax/gateway/agents/<name>/sessions.json`).
- aX message history is the canonical source on cold restart — bridge reconstructs in-memory history by fetching recent messages once at startup, then maintains in-memory thereafter.

## Bridge protocol (stdout events)

Same `AX_GATEWAY_EVENT` contract as today, plus:
- `{"kind":"started","agent_name":"<name>"}` — sent once when the bridge is ready to accept work.
- `{"kind":"heartbeat","ts":"...","cadence_seconds":N}` — sent on the bridge's *declared* cadence. Default declared cadence is 15s, but the bridge may declare any cadence and the gateway's stale-detection respects whatever the bridge sends in `cadence_seconds`. This aligns with **HEARTBEAT-001** (PR #100): cadence is a per-agent declaration, not a hardcoded gateway constant. Stale threshold = `cadence_seconds × 2` (orion's HEARTBEAT-001 default tolerance).
- `{"kind":"thread_loaded","thread_id":"...","turns":N}` — emitted before processing a message, signals to the UI "Recalling N prior turns".

## API + CLI

```
GET /api/agents/{name}/runtime/state
# returns: { running: bool, pid: int|null, started_at, last_heartbeat_at, threads_loaded: int }

POST /api/agents/{name}/runtime/restart
# graceful stop + spawn

ax gateway agents runtime status <name>
ax gateway agents runtime restart <name>
ax gateway agents runtime logs <name> --tail 50
```

## Acceptance smokes (CLI-driven)

```bash
# Follow-up: add an agent with persistent intake once implemented
ax gateway agents add memo-bot --template ollama --connection-mode live_listener
ax gateway agents runtime status memo-bot
# expect: running=true, pid=<int>, threads_loaded=0

# Test session memory across messages
curl -sS -X POST http://127.0.0.1:8765/api/agents/memo-bot/test \
  -d '{"content":"My favorite color is cobalt. Reply with just: noted."}' \
  -H 'Content-Type: application/json'
sleep 6
curl -sS -X POST http://127.0.0.1:8765/api/agents/memo-bot/test \
  -d '{"content":"What color did I just tell you was my favorite?"}' \
  -H 'Content-Type: application/json'
sleep 6
# expect: reply contains "cobalt"

# Verify activity bubbles fired for both messages
ax gateway agents runtime logs memo-bot --tail 20 | grep AX_GATEWAY_EVENT
# expect: thinking + processing + completed events for each turn

# Crash recovery
kill $(ax gateway agents runtime status memo-bot --json | jq -r .pid)
sleep 35  # wait past heartbeat timeout
ax gateway agents runtime status memo-bot
# expect: running=true, pid changed, threads_loaded=1 (history restored from disk)

# Cleanup
ax gateway agents remove memo-bot
```

## What survives a gateway restart (current state, 2026-04-26)

Operator-relevant question — added per madtank: "Do we remember stuff on restart and should we?" Answer: **most of it survives, by file**. The split:

**Survives restart (durable on disk):**

| Concern | File | Notes |
|---|---|---|
| Agent registry (names, ids, templates, space bindings, `last_reply_at`, `processed_count`, `pinned`, `desired_state`) | `~/.ax/gateway/registry.json` | Loaded fresh on every daemon boot via `load_gateway_registry()`. |
| Per-agent pending mailboxes (pass-through inbox queue) | `~/.ax/gateway/agents/<name>/pending.json` | Survives restart. Pass-through agents pick up where they left off. |
| Per-agent gateway-issued PATs | `~/.ax/gateway/agents/<name>/token` | Persistent token files, mode 0600. |
| Activity log (lifecycle events for the drawer + audit trail) | `~/.ax/gateway/activity.jsonl` | Append-only. The drawer reads recent N items via `load_recent_gateway_activity()`. |
| Approvals (pending / approved / rejected pass-through bindings) | inside `registry.json` under `approvals` | Pending approvals survive restart. |
| Identity bindings (gateway↔agent attestation) | inside `registry.json` under `identity_bindings` | Survives restart. |
| Gateway operator session (PAT, base_url, username) | `~/.ax/gateway/session.json` | Survives. Logout deletes the file → drops to T0 offline (per AUTH-TIERS). |

**Does NOT survive restart (in-memory only):**

| Concern | Why | Recovery on restart |
|---|---|---|
| `ManagedAgentRuntime` per-agent worker state (current_status, current_activity, current_tool, in-flight queue) | Lives in daemon process memory. | Reconcile loop respawns workers; in-flight messages are re-fetched via SSE. |
| Hermes sentinel subprocess + tool sandbox state | Subprocess dies with the daemon. | Reconcile loop relaunches the sentinel; Hermes reconstructs context from aX history. |
| SSE listener loops | TCP connections dropped. | Reconnect with exponential backoff. |
| `_send_client` AxClient cache | Re-instantiated lazily on first publish. | Lazy-init on next event (per ACTIVITY-VISIBILITY fix). |

**Should we persist more?** For demo: no — current persistence is enough. For production: the cosmetic `backlog_depth` mismatch case (when agent replies via PAT and the local registry doesn't know) is solved by the **pass-through ack endpoint** (see LOCAL-CONNECT-001). For session memory in conversational runtimes, the design is below — sliding window per thread, persisted on graceful shutdown, reconstructed from aX history on cold restart.

## Privacy decisions (locked)

- **Removal.** When an agent is removed (`DELETE /api/agents/{name}`), the gateway MUST delete `~/.ax/gateway/agents/<name>/sessions.json` along with the token file. No leftover session memory on disk.
- **Cross-space move.** When an agent is moved to a new space, **drop in-memory threads from the previous space**. The session memory for the new space starts empty. Privacy by default — we never let conversation context leak across space boundaries. If a user needs continuity, they can reissue messages in the new space and rebuild context naturally from aX history.

## Open questions

- Should `live_listener` Ollama be the default for `--template ollama`, or stay opt-in via `--connection-mode live_listener`? Default-on means new users get session memory automatically; default-off means we don't pin GPU/RAM unexpectedly. **Recommended default: opt-in for now**; flip to opt-out once we have CPU/RAM telemetry to back the change.
- Memory eviction policy: LRU per thread, or hard cap? For demo we use sliding window per thread; production might need cross-thread eviction.
