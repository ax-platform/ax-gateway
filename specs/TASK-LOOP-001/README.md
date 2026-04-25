# TASK-LOOP-001: Task-Driven Loops with Priority Queue + HITL Drafts

**Status:** v1 — CLI-first implementation landing in this PR
**Owner:** @orion
**Date:** 2026-04-25
**Source directive:** @madtank 2026-04-25 03:46 UTC ("frequency and priority… queue a priority for each assignment, moved around like a song playing"); @madtank 2026-04-25 04:00 UTC ("loop should support a draft/review state where the HITL user can inspect and explicitly send")

## Why this exists

The team needs task-driven loops, not time-driven cron. Each assignment carries: how often it runs, when it stops (if ever), priority, assignee. Operators reorder the queue "like a song playing." High-stakes loops should produce drafts a human reviews before they go out.

Reminders (`ax reminders`) already provided 80% of the spine: task-tied policies, cadence, max-fires, target resolution. This spec extends reminders with the missing primitives.

**CLI-first.** Prove the model in `~/.ax/reminders.json`. Promote to platform only after the loop pattern is validated in real use.

## Scope

In:
- Per-policy `priority` field, queue ordered by priority
- `mode` field: `auto` / `draft` / `manual`
- Draft store with HITL review/edit/send/cancel
- Operator commands: `pause`, `resume`, `cancel`, `update`
- Pytest smokes covering all three modes

Out (follow-up):
- Backend persistence of loop state (currently local JSON)
- Cross-machine queue sync (single-machine for now)
- `ax tasks loop` semantic alias group (deferred — `ax reminders` is the implementation; the alias is naming polish)
- Platform-side scheduler integration (TASKS-LIFECYCLE-001 territory)
- Stop conditions beyond `max_fires` and "source task is terminal" (e.g. done-event hooks)

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
# Existing (now with --priority and --mode)
ax reminders add <task-id> [--priority N] [--mode auto|draft|manual] [--cadence-minutes N] [--max-fires N] [--target X]
ax reminders list
ax reminders run --once
ax reminders run --watch --interval 30
ax reminders disable <id>           # legacy, kept

# New operator commands
ax reminders pause <id>
ax reminders resume <id>
ax reminders cancel <id>
ax reminders update <id> [--priority N] [--cadence-minutes N] [--max-fires N] [--mode X] [--reason ...] [--target X]

# New drafts subcommand group
ax reminders drafts list
ax reminders drafts show <draft-id>
ax reminders drafts edit <draft-id> [--body "..."] [--target X]
ax reminders drafts send <draft-id>
ax reminders drafts cancel <draft-id>
```

## Acceptance smokes

All in `tests/test_task_loop_modes.py` — 11 pytest cases:

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

Existing reminder tests (8) continue to pass — backwards compatible (version 1 store loads as version 2 with empty drafts array; new fields have sensible defaults).

## Why this is a small spec

Per orion's SDD critique 2026-04-25 03:55 UTC: "stop opening new spec PRs until you've shipped one implementation PR against an existing spec." TASK-LOOP-001 is implementation-first. The spec is this 1-page README; the contract is the 11 pytests; the surface is the merged PR.

If we want to evolve the model (backend persistence, cross-machine sync, semantic `ax tasks loop` alias), each evolution gets a numbered iteration. v1 is what's in this PR.

## Out-of-scope cross-references

- **TASKS-LIFECYCLE-001** (orion task #9, pending) — stale auto-cancel + alerts/reminders to activity stream. Coordinates with TASK-LOOP-001 but is its own spec.
- **AX-SCHEDULE-001** (orion + anvil) — periodic command runner (cron-like). Different primitive: schedules execute commands; loops drive task assignments.
- **AGENT-AVAILABILITY-CONTRACT-001** (orion, PR #97) — when a loop targets an agent, the resolved availability DTO (`agent_state`) determines whether to fire, defer, or auto-draft. Future iteration: when target's `expected_response in {unlikely, unavailable}`, automatically degrade to draft mode.
