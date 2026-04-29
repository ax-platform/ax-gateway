# HEARTBEAT-001: Local-First Agent Heartbeat Primitive

**Status:** v1 — CLI-first implementation landing in this PR
**Owner:** @orion
**Date:** 2026-04-25
**Source directives:**
- @madtank 2026-04-25 04:11 UTC — "we need to start getting features like heartbeat... we need to have our own pulse on the gateway"
- @madtank 2026-04-25 15:25 UTC — "keep moving and shipping faster, especially around gateway and connectedness and the registry... it's funny how we might be competing against AWS and Google"
- Heartbeat primitive memory note (2026-04-09) — "each agent declares its own cadence, routing asks 'did it meet its own cadence within tolerance?'"

## Why this exists

**Connectedness** is one of three primitives madtank named as urgent (gateway / connectedness / registry). The heartbeat is the foundation of connectedness: it decouples "alive" from "replied" by letting each agent declare its own cadence, then asking "did it meet its own cadence within tolerance?"

The platform already has a backend heartbeat endpoint (`POST /api/v1/agents/heartbeat`) used by SSE listeners. This spec adds the **CLI primitive** so any agent — not just SSE-connected ones — can ping presence on its own cadence.

**CLI-first.** Local store at `~/.ax/heartbeats.json`; offline-safe; pushes when online. Same offline-first pattern as TASK-LOOP-001. Promote richer protocol semantics to the platform after the CLI version is validated.

## Scope

In:
- `ax heartbeat send/list/status/push/watch` commands
- Local store with cadence, current status/note, push state, history (ring-buffer 100)
- Status vocabulary: `active` / `busy` / `delayed` / `sleeping` / `unresponsive` / `suspended` / `disabled` / `unknown` (with pass-through for unknown values so the protocol can evolve)
- Offline-first: network errors queue locally with `pushed=false` and `push_error`
- `ax heartbeat push` drains queued (presence is latest-wins; only the newest unpushed heartbeat hits the wire, older ones are local history)
- `ax heartbeat watch --interval N --max-ticks N` daemon mode

Out (follow-up):
- Backend richer heartbeat schema (currently backend treats body extras as ignorable; once the protocol matures, backend ingests `status`/`note`/`cadence_seconds`)
- Heartbeat-derived `responsive` axis on the AVAIL-CONTRACT-001 resolved DTO (depends on backend wiring)
- Gateway daemon emitting heartbeats on behalf of managed sentinels (separate task #19, GATEWAY-PULSE-001)
- MCP tool surface for heartbeats (separate, AWS-Agent-Registry-equivalent surface)

## Data model — local store at `~/.ax/heartbeats.json`

```json
{
  "version": 1,
  "agent_name": "orion",
  "agent_id": "...",
  "cadence_seconds": 60,
  "current_status": "active",
  "current_note": null,
  "last_sent_at": "ISO",
  "last_pushed_at": "ISO?",
  "next_due_at": "ISO",
  "history": [
    {
      "id": "hb-...",
      "status": "active",
      "note": "...",
      "sent_at": "ISO",
      "pushed": true,
      "pushed_at": "ISO?",
      "push_error": null,
      "backend_ttl_seconds": 30
    }
  ]
}
```

## CLI surface

```
ax heartbeat send [--status STATUS] [--note "..."] [--cadence N] [--skip-push] [--file PATH]
  # Records locally + POSTs to /api/v1/agents/heartbeat
  # On network error: queued with pushed=false + push_error
  # --skip-push: record locally only (offline mode without retry attempt)

ax heartbeat list [--limit N] [--unpushed]
  # Local heartbeat history, most recent first
  # --unpushed filters to queued records

ax heartbeat status [--skip-probe]
  # Online/offline (cheap GET /health probe; --skip-probe to assume offline)
  # Current status + cadence + last_sent_at + next_due_at + queued count
  # --json for tooling

ax heartbeat push
  # Drain queued (unpushed) heartbeats. Sends ONLY the latest — presence is
  # latest-wins; older heartbeats are local-only history. All older
  # unpushed records are marked pushed=true.

ax heartbeat watch --interval N [--status S] [--note "..."] [--max-ticks N]
  # Tick-based daemon. Each tick: send heartbeat, record locally, log result.
  # Use --max-ticks for bounded runs (CI smokes); 0 = run forever.
```

## Acceptance smokes (`tests/test_heartbeat_commands.py`)

11 pytest cases covering:

1. `send` records and pushes when online (verifies backend call, store update, ttl)
2. `send` queues locally on network error (verifies push_error, no last_pushed_at)
3. `send --skip-push` records local-only without calling backend
4. `send` rejects invalid cadence (`--cadence 0`)
5. `send --status future_value_xyz` passes unknown status through (protocol evolution)
6. `status` reports queued unpushed count + agent name + cadence + next_due
7. `status` handles empty store gracefully (no crash, sensible defaults)
8. `list --unpushed` returns only queued records, most recent first
9. `push` drains queue when online: sends LATEST status, marks all older unpushed as pushed
10. `push` returns clean (no error) when no queued records
11. `push` returns error + non-zero exit when offline; record updated with push_error

## Why this is a small spec

Per @orion's SDD critique 2026-04-25: implementation-first. The contract is the 11 pytests. Spec evolves with code.

## Out-of-scope cross-references

- **AGENT-AVAILABILITY-CONTRACT-001** (PR #97 merged) — heartbeats feed the Responsive axis. When backend wires `agent_state.responsive` from heartbeat freshness × declared cadence, this primitive becomes the data source.
- **GATEWAY-PULSE-001** (task #19) — Gateway daemon emitting heartbeats on behalf of managed sentinels. Will reuse this primitive's local store + push semantics.
- **AGENT-TRIGGER-SEMANTICS-001** (backend_sentinel pending) — vocabulary alignment when that frame lands.
