# GATEWAY-RUNTIME-PERSISTENCE-001: Persistent Runtime Model for Conversational Agents

**Status:** v1 draft
**Owner:** @pulse, reviewer @orion
**Date:** 2026-04-25
**Source directives:**
- @madtank 2026-04-25: "I think what's happening is your invoking them like a individual call but I think we need to keep them running and we need to keep them available. That way when we send a message they're already running. They're already working and they hold the session context."
- @madtank: "We need to make sure that an agent at least be able to hold a conversation."

## Vocabulary alignment

This spec **anchors on `connection_mode`** from AVAIL-CONTRACT-001 (`specs/AGENT-AVAILABILITY-CONTRACT-001/spec.md`). The four legal values are `live_listener | on_demand_warm | inbox_queue | disconnected`. We do **not** invent a new `intake_model` field — that would collide. Whenever this spec talks about "making Ollama persistent", we mean **flipping its `connection_mode` from `on_demand_warm` → `live_listener`**. Every other field on the agent record stays as-is.

## Why this exists

The Ollama runtime today is `intake_model: launch_on_send`. Every incoming message:
1. cold-launches `python3 examples/gateway_ollama/ollama_bridge.py` as a subprocess
2. runs one Ollama generate call
3. exits

Effects:
- **No in-memory session context.** Each call is fresh. The bridge currently fetches recent messages from aX as a workaround, but that's a per-call HTTP round trip.
- **Cold-start latency** every message (Python startup, Ollama load, reconnect to aX).
- **Activity bubbles look choppy** because the runtime isn't continuously emitting events between messages.
- **Doesn't match the user's mental model.** Users expect "I started this agent, it's running, it remembers what we talked about" — like Hermes or Claude.

We want Ollama (and any conversational runtime) to be **persistent**: a long-lived listener process that holds session memory in process, subscribes to incoming work via SSE, and replies inline.

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
- `{"kind":"heartbeat","ts":"..."}` — sent every 15s while idle.
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
# Add an agent with persistent intake
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

## Open questions

- Should `live_listener` Ollama be the default for `--template ollama`, or stay opt-in via `--connection-mode live_listener`? Default-on means new users get session memory automatically; default-off means we don't pin GPU/RAM unexpectedly.
- Memory eviction policy: LRU per thread, or hard cap? For demo we use sliding window per thread; production might need cross-thread eviction.
- When an agent is moved to a new space, do we drop in-memory threads from the old space? (Probably yes — privacy by default.)
