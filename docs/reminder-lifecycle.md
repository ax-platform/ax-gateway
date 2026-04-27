# Reminder Lifecycle Contract

**Spec source:** tasks `e032bc49` (urgent) + `f00e36ac` (high), 2026-04-16.
**Owner:** `ax_cli/commands/reminders.py::_fire_policy` + `ax_cli/commands/alerts.py::_task_lifecycle`.
**Composes:** ACTIVITY-TAXONOMY-001 (reminder = Alert type), SEND-RECEIPTS-001 (delivery receipts).

## Problem

The local reminder runner (`ax reminders run`) was firing policies based only on `next_fire_at` — with no check on the underlying task's state. That produced two specific regressions dogfooded on 2026-04-16:

1. **Completed tasks kept ringing.** Closing a task did not stop reminders authored against it; they kept flooding the Activity Stream until `max_fires` was exhausted.
2. **Pending-review pings woke the worker, not the reviewer.** When a task moved to a waiting-for-review state, the runner kept mentioning the assignee ("you're late"), when the person who actually needed to act was the reviewer.

## Contract

A reminder is a *directed Alert* (per ACTIVITY-TAXONOMY-001 §4.1). Its target and whether it fires at all depend on the **lifecycle state of the source task**, not on the local policy alone.

### Lifecycle states (as seen by the runner)

| State                 | Signal on `task`                                                                 | Runner behavior                                                                                     |
|-----------------------|-----------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------|
| **Active**            | `status ∈ {open, in_progress, ...}` — not terminal, not pending-review            | Fire to assignee (fall back to creator if no assignee). Default pre-2026-04-16 behavior.            |
| **Pending review**    | `status == "pending_review"` OR `tags` contains `pending_review` OR `requirements.pending_review` truthy OR `requirements.review_owner*` set | Fire with `[pending review]` prefix to `review_owner` (handle) → `review_owner_id` (resolved) → `creator_name`. Do **not** wake the worker/assignee. |
| **Terminal**          | `status ∈ {completed, closed, done, cancelled, canceled, archived, resolved}` or `completed_at` is set | **Do not fire.** Disable the policy (`enabled = false`, `disabled_reason = "source task <id> is <status>"`). Emit a skipped-result so the caller does NOT advance `fired_count` or reschedule. |

### What "Terminal" means

- Task status enum includes: `completed`, `closed`, `done`, `cancelled`, `canceled`, `archived`, `resolved`. Any of those → terminal.
- Fallback: `completed_at` is a non-empty string → terminal even if the status string is unusual.
- Terminal is a one-way transition. The runner never re-enables a policy it disabled for lifecycle reasons; the user can re-add one against a new task if needed.

### What "Pending review" means

The backend task schema does not (yet) have a first-class `pending_review` flag, so the runner reads from any of these sources, in priority order:

1. `task.status == "pending_review"`
2. `"pending_review"` present in `task.tags[]`
3. `task.requirements.pending_review` is truthy
4. `task.requirements.review_owner` or `task.requirements.review_owner_id` is set

If any matches, the reminder reroutes. Target selection order:

1. `task.requirements.review_owner` (string handle)
2. `task.requirements.review_owner_id` → resolved via `/api/v1/agents/{id}`
3. `task.creator_id` → resolved to handle (creator-as-fallback escalation)
4. No route available → runner falls back to default (assignee). This is a soft-fail; the reminder still fires against the worker rather than being silently dropped, but the operator should see `target_resolved_from=assignee` in the emitted metadata and treat it as a gap.

The reminder `reason` is prefixed with `[pending review]` so the receiver sees why they were pinged instead of the worker.

## Envelope changes (metadata.reminder_policy)

`_fire_policy` emits a `metadata.reminder_policy` block that now carries:

| Field                    | Values                                                                       |
|--------------------------|------------------------------------------------------------------------------|
| `target_resolved_from`   | `assignee` \| `creator` \| `review_owner` \| `creator_fallback` \| `manual`    |
| `policy_id`              | (unchanged)                                                                  |
| `fired_count`            | (unchanged — not advanced on lifecycle-skipped runs)                         |

Terminal-skip does NOT emit a message at all — there is no envelope. The run output surfaces it as `{"skipped": true, "reason": "source_task_terminal:<status>"}`.

## Backwards compatibility

- Policies that never had a `source_task_id` are unaffected (no lifecycle lookup runs).
- Policies where the task fetch fails (404, network) fall through to pre-lifecycle behavior: fire to the stored `target` (or `_resolve_target_from_task` fallback). Runner does **not** disable on fetch failure — that would cascade into quiet drops during backend outages.
- An existing policy whose task is already terminal will self-disable on the next pass. No manual cleanup needed.

## Test coverage

See `tests/test_reminders_commands.py`:

- `test_run_once_skips_and_disables_when_source_task_is_terminal` — completed task → no message, policy disabled, `fired_count` unchanged.
- `test_run_once_reroutes_pending_review_to_review_owner` — pending_review with `review_owner` → message to reviewer, `[pending review]` prefix, `target_resolved_from=review_owner`.
- `test_run_once_pending_review_falls_back_to_creator_when_no_owner` — pending_review flag only → routes to creator, `target_resolved_from=creator_fallback`.
- `test_run_once_without_task_snapshot_still_fires` — fetch failure fallback path unchanged.

## Non-goals (v1)

- No backend schema change. The runner reads from existing task fields / requirements dict / tags list, whichever the backend happens to expose.
- No supervisor/aX escalation beyond `creator_name` fallback. A future revision can add a configured escalation target (ENV `AX_REMINDER_ESCALATION_TARGET`) or a space-level default.
- No snooze/dismiss reply. The existing `ax alerts ack`/`snooze` commands remain the user-facing control; adding a `skip_review` auto-resolve is out of scope for this change.

## Change log

- 2026-04-16 — Initial contract (@demo-agent). Ships with tests + `_task_lifecycle` helper in `alerts.py`. Picks up source task status on every `_fire_policy` call; one extra GET per due policy (cost acceptable for local dogfood loop).


## Pause/resume and grooming workflow (2026-04-27)

Reminder policies now have a reversible pause state separate from permanent disable:

- `axctl reminders pause <policy-id> --reason "blocked/noisy" [--resume-at ISO | --minutes N]`
- `axctl reminders snooze <policy-id> --minutes N --reason "waiting for owner"`
- `axctl reminders resume <policy-id> [--fire-in-minutes N]`
- `axctl reminders groom [--apply]` reports stale/noisy/completed/orphaned policies and can disable completed/source-terminal reminders.

Pause metadata is stored on the local policy as `paused`, `paused_reason`, `paused_by`, `paused_at`, `resume_at`, and `snooze_until`. The runner skips paused policies. If `resume_at`/`snooze_until` has passed, the runner auto-resumes the policy before checking whether it is due.

`axctl reminders list --json` remains parse-safe and now includes:

- top-level `policies`: the sorted policy list for backwards-compatible automation
- `groups`: policies grouped into `due`, `active`, `paused`, `disabled`, `completed`, and `stale`
- `summary`: counts by group

### Agile reminder hygiene

1. **Continue useful work:** keep active reminders only for tasks that still need a response or check-in.
2. **Close/disable completed work:** terminal source tasks are skipped by the runner and should be disabled/removed during grooming.
3. **Pause blocked/noisy work:** use pause/snooze with a clear reason instead of permanently disabling when work may become actionable again.
4. **Resume when actionable:** set `resume_at`/`snooze_until` when possible; otherwise run `axctl reminders resume` when the blocker clears.
5. **Groom regularly:** review `due`, `paused`, `disabled`, `completed`, and `stale` groups so junk reminders do not crowd out current work.

`groom --apply` is intentionally conservative: it only disables reminders that are already completed by max-fires or whose source task resolves as terminal. Stale/orphaned/no-source reminders are reported for human review rather than silently deleted.
