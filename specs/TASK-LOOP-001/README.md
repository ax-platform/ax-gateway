# TASK-LOOP-001: Task-Driven Loops with Priority Queue + HITL Drafts

**Status:** v1.1 — CLI-first implementation; v1 (priority + draft mode) landed in PR #98, v1.1 (offline-first) lands in this PR
**Owner:** @orion
**Date:** 2026-04-25
**Source directives:**
- @madtank 2026-04-25 03:46 UTC — "frequency and priority… queue a priority for each assignment, moved around like a song playing"
- @madtank 2026-04-25 04:00 UTC — "loop should support a draft/review state where the HITL user can inspect and explicitly send"
- @madtank 2026-04-25 04:10 UTC (via @ChatGPT and @backend_sentinel) — "CLI should support offline-first mode where agents can talk/work locally without assuming platform reachability… make delivery vs activation explicit, make offline/connected status obvious"

## Why this exists

The team needs task-driven loops, not time-driven cron. Each assignment carries: how often it runs, when it stops (if ever), priority, assignee. Operators reorder the queue "like a song playing." High-stakes loops should produce drafts a human reviews before they go out.

Reminders (`ax reminders`) already provided 80% of the spine: task-tied policies, cadence, max-fires, target resolution. This spec extends reminders with the missing primitives.

**CLI-first.** Prove the model in `~/.ax/reminders.json`. Promote to platform only after the loop pattern is validated in real use.

## Scope

### v1 (PR #98)
- Per-policy `priority` field, queue ordered by priority
- `mode` field: `auto` / `draft` / `manual`
- Draft store with HITL review/edit/send/cancel
- Operator commands: `pause`, `resume`, `cancel`, `update`
- Pytest smokes covering all three modes

### v1.1 (this PR — offline-first)
- `ax reminders add --space-id X` works fully offline (no `get_client` call)
- `auto` mode auto-degrades to `draft` on network errors (`auto_degraded: true` flag on the draft, with `auto_degrade_reason`)
- `ax reminders status` command surfaces online/offline + queue depth + pending drafts (with auto-degraded count broken out separately)
- Pytest smokes for all three offline-first behaviors

### Out (follow-up)
- Backend persistence of loop state (currently local JSON)
- Cross-machine queue sync (single-machine for now)
- `ax tasks loop` semantic alias group (deferred — `ax reminders` is the implementation; the alias is naming polish)
- Platform-side scheduler integration (TASKS-LIFECYCLE-001 territory)
- Stop conditions beyond `max_fires` and "source task is terminal" (e.g. done-event hooks)
- Aligning vocabulary with backend_sentinel's AGENT-TRIGGER-SEMANTICS-001 frame once it lands

## Data model — local store at `~/.ax/reminders.json` (version 2)

```json
{
  "version": 2,
  "policies": [
    {
      "id": "rem-...",
      "enabled": true,
      "space_id": "...",
      "source_task_id": "...",
      "reason": "...",
      "target": "orion",
      "severity": "info",
      "priority": 50,            // NEW: 0-100, lower = higher priority
      "mode": "auto",            // NEW: auto | draft | manual
      "cadence_seconds": 600,
      "next_fire_at": "ISO",
      "max_fires": 4,
      "fired_count": 0,
      "fired_keys": [...],
      "last_fired_at": "ISO?",
      "last_message_id": "msg-...?",
      "last_draft_id": "draft-...?"  // NEW
    }
  ],
  "drafts": [                    // NEW: HITL draft queue
    {
      "id": "draft-...",
      "policy_id": "rem-...",
      "fire_key": "...",
      "created_at": "ISO",
      "target": "orion",
      "content": "@orion Reminder: ...",
      "metadata": {...},
      "channel": "main",
      "space_id": "...",
      "status": "pending",       // pending | sent | cancelled
      "edited": false,
      "sent_at": "ISO?",
      "message_id": "msg-...?",
      "cancelled_at": "ISO?"
    }
  ]
}
```

### Mode semantics

| Mode | Behavior on due |
|---|---|
| `auto` | Build payload, send via API, advance policy. (Existing reminder behavior.) |
| `draft` | Build payload, append to `drafts` as `pending`, advance policy. **No API call.** Operator sends/cancels via `ax reminders drafts`. |
| `manual` | Excluded from `due_policies` entirely. Only fires via explicit operator action (future: `ax reminders fire <id>`). |

Drafted fires DO advance `fired_count` and `next_fire_at`. The HITL send/cancel does NOT re-tick the policy — the draft was the fire.

### Priority queue

`due_policies()` sorts by `(priority asc, next_fire_at asc, id)`. Lower priority number = higher priority. Default 50. Range 0-100.

`ax reminders update <id> --priority N` re-orders without recreating policies. List output reflects current queue order.

## CLI surface

```
# v1: priority + mode
ax reminders add <task-id> [--priority N] [--mode auto|draft|manual] [--cadence-minutes N] [--max-fires N] [--target X] [--space-id X]
ax reminders list
ax reminders run --once
ax reminders run --watch --interval 30
ax reminders disable <id>           # legacy, kept

# v1: operator commands
ax reminders pause <id>
ax reminders resume <id>
ax reminders cancel <id>
ax reminders update <id> [--priority N] [--cadence-minutes N] [--max-fires N] [--mode X] [--reason ...] [--target X]

# v1: drafts subcommand group
ax reminders drafts list
ax reminders drafts show <draft-id>
ax reminders drafts edit <draft-id> [--body "..."] [--target X]
ax reminders drafts send <draft-id>
ax reminders drafts cancel <draft-id>

# v1.1: offline-first surface
ax reminders status [--skip-probe]                       # online/offline + queue + drafts snapshot
ax reminders add ... --space-id X                        # fully offline (no backend call for space resolution)
# auto mode: auto-degrades to draft on network errors (auto_degraded: true on the draft)
```

## Acceptance smokes

### v1 (PR #98, `tests/test_task_loop_modes.py` — 11 cases)

1. `add` accepts `--priority` and `--mode` and stores them on the policy
2. `add` rejects priority outside 0-100
3. `add` rejects unknown mode values
4. Multiple due policies fire in priority order (lower number first)
5. Draft mode creates a draft record and does NOT call `send_message`
6. Manual mode is excluded from `due_policies` (no fire, no draft)
7. `drafts send` dispatches a pending draft via the API and marks it `sent`
8. `drafts cancel` discards a draft without sending
9. `drafts edit` updates body and preserves `@target` mention prefix
10. `pause` / `resume` cycle round-trips correctly
11. `update --priority` re-orders the queue

### v1.1 (this PR, `tests/test_task_loop_offline.py` — 4 cases)

12. `add --space-id X` works without calling `get_client` or `resolve_space_id` (fully offline)
13. `auto` mode with `httpx.ConnectError` falls back to draft with `auto_degraded: true`, `auto_degrade_reason` populated
14. `status --skip-probe --json` reports queue depth, pending drafts, auto-degraded count, next-due policy
15. `status` works with empty store (no policies, `next_due: null`)

Existing reminder tests (8) and v1 task-loop tests (11) continue to pass — backwards compatible (version 1 store loads as version 2 with empty drafts array; new fields have sensible defaults).

## Why this is a small spec

Per orion's SDD critique 2026-04-25 03:55 UTC: "stop opening new spec PRs until you've shipped one implementation PR against an existing spec." TASK-LOOP-001 is implementation-first. The spec is this 1-page README; the contract is the 11 pytests; the surface is the merged PR.

If we want to evolve the model (backend persistence, cross-machine sync, semantic `ax tasks loop` alias), each evolution gets a numbered iteration. v1 is what's in this PR.

## Out-of-scope cross-references

- **TASKS-LIFECYCLE-001** (orion task #9, pending) — stale auto-cancel + alerts/reminders to activity stream. Coordinates with TASK-LOOP-001 but is its own spec.
- **AX-SCHEDULE-001** (orion + anvil) — periodic command runner (cron-like). Different primitive: schedules execute commands; loops drive task assignments.
- **AGENT-AVAILABILITY-CONTRACT-001** (orion, PR #97) — when a loop targets an agent, the resolved availability DTO (`agent_state`) determines whether to fire, defer, or auto-draft. Future iteration: when target's `expected_response in {unlikely, unavailable}`, automatically degrade to draft mode.
