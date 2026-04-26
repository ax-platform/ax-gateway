# TASK-AUTONOMY-001 — Platform Architecture

**Status:** v1 — open for review
**Owner:** @orion
**Source task:** [`ba50f36e`](aX) — platform-native task reminder loops architecture
**Companion contract:** [`spec.md`](./spec.md) (data model, events, semantics — task `8e1a7ab3`)
**Implementation tasks:**
- [`3e665d6a`](aX) — backend_sentinel: durable scheduler + notification events
- [`3e68fd31`](aX) — backend_sentinel: stale lifecycle + auto-cancel
- [`94b0c91c`](aX) — backend_sentinel: infra audit (Redis/PR #45/#32 drift check)
- [`103605b2`](aX) — frontend_sentinel: activity-stream UX
**Date:** 2026-04-26

## Why this exists (separate from spec.md)

`spec.md` defines **what** the contract is — fields, events, state machine. This document defines **where the work lives** in the platform: scheduler ownership, persistence layer, dispatch path, infra primitives, rollout strategy, RC gates.

Per ChatGPT 17:37 UTC: "Do not treat this as a blind Gateway port. We need the platform design mapped before backend/infra execution." Reminders existed in the CLI/Gateway loop; we're now baking the right pieces into the platform with eyes open about which Gateway behaviors to inherit, which to redesign, and which to leave behind.

## Mapping the existing surface

### `ax_cli/commands/reminders.py` + [`TASK-LOOP-001`](../TASK-LOOP-001/README.md) — what we have today

CLI-side reminder loops at `~/.ax/reminders.json`. 1041 lines, real semantics:

- Per-policy `priority` field; queue is priority-ordered
- `mode`: `auto` / `draft` / `manual` — HITL drafts review before fire
- Offline-first: `add` works without `get_client`; `auto` mode auto-degrades to `draft` on network errors
- Operator commands: `pause`, `resume`, `cancel`, `update`
- Local pytest smokes covering all three modes

**What to inherit:**
- Priority queue model (P0 ordering rule from `_policy_sort_key`)
- Auto/draft/manual mode taxonomy
- Auto-degrade pattern (network failure → draft, with `auto_degraded: true`)
- Operator pause/resume/cancel verbs (already in TASK-AUTONOMY-001 spec)

**What to leave behind:**
- Local JSON persistence (machine-bound; doesn't survive box failure or cross-host)
- CLI-side scheduler tick (best-effort; no guarantees)
- Per-machine queue (no cross-host coordination)

**What's net-new in the platform port:**
- Server-authoritative scheduler with durable persistence
- Idempotency across retries
- Cross-host visibility (any agent's reminder fires regardless of which machine the agent runs on)
- Activity-stream events (the CLI loop has none today)
- Escalation chain (CLI loop has none)

The CLI `ax reminders` surface stays — but it's for ad-hoc cron-like loops not bound to tasks. The new `ax tasks ack/snooze/pause` from `spec.md` is the **task-bound autonomy** surface. Different use cases; both coexist.

## Architecture decisions

### Scheduler ownership: backend FastAPI worker thread + Postgres polling

**Decision:** in-process worker thread inside the existing FastAPI process, polling Postgres every 30s for `next_reminder_at <= now()`.

**Rationale:**
- Simplest viable. No new infra resources.
- Survives restart (state lives in Postgres, not memory).
- Postgres polling at 30s tick handles 1000s of reminders/min trivially with one indexed query (`WHERE next_reminder_at <= now() AND paused_at IS NULL AND status = 'in_progress' ORDER BY next_reminder_at LIMIT 100`).
- Failure mode is loud: thread dead → CloudWatch alarm on stale `last_scheduler_tick_at` heartbeat.

**Rejected alternatives:**

| Option | Why rejected for v1 |
|---|---|
| Separate ECS service | New infra surface; deploy lane, IAM role, security group — none of which buys anything at current scale. v2 candidate when reminders >10k/min. |
| AWS EventBridge | Cron + per-task scheduling — EventBridge isn't built for fine-grained per-task variability without overengineering rule lifecycle. |
| AWS Lambda triggered by SQS | Same as ECS plus cold-start latency. |
| Redis sorted set with `ZRANGEBYSCORE` | Faster than Postgres polling at scale, but adds a crash-recovery story we don't yet need. v2 candidate. |
| Pure Postgres `pg_notify` push | Tempting but per-row notify adds DB load + tight coupling; polling is more debuggable. |

**Single-process race:** if backend horizontally scales, multiple workers polling will race. Solution: distributed lock via Redis `SET NX EX` on key `task_autonomy:scheduler_lock`, lock held for tick duration. Only the lock-holder fires reminders. Existing infra has Redis already (per ax-backend CLAUDE.md). Lock TTL = 60s (so a dead holder releases automatically).

### Persistence layer

**Decision:** Postgres, two tables.

**`tasks` table extension** (per `spec.md` data model — 14 new columns, all default-null/safe):
- Migration adds columns; existing rows behave as today.
- Indexes:
  - `(next_reminder_at) WHERE paused_at IS NULL AND status IN ('open','in_progress')` — partial index for the scheduler hot path
  - `(stale_threshold_seconds, last_status_change_at) WHERE status = 'in_progress'` — partial index for stale detection
  - `(auto_cancel_at) WHERE auto_cancel_at IS NOT NULL` — partial index for auto-cancel sweep

**New `task_reminder_runs` table** (audit + idempotency):

```sql
CREATE TABLE task_reminder_runs (
  task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  scheduled_for TIMESTAMPTZ NOT NULL,
  fired_at TIMESTAMPTZ,
  event_id UUID NOT NULL UNIQUE,
  delivery_status TEXT NOT NULL CHECK (delivery_status IN ('pending','delivered','failed','dlq')),
  retry_count INT NOT NULL DEFAULT 0,
  last_error TEXT,
  PRIMARY KEY (task_id, scheduled_for)
);
CREATE INDEX idx_task_reminder_runs_pending ON task_reminder_runs (delivery_status, fired_at)
  WHERE delivery_status IN ('pending','failed');
```

**Idempotency:** `PRIMARY KEY (task_id, scheduled_for)` makes a re-poll of an already-fired reminder a no-op. The scheduler's INSERT-then-fire pattern is "ON CONFLICT DO NOTHING" — duplicate detection at insert time, dispatch never sees it.

### Dispatch path

**Decision:** existing `app/core/dispatch_queue.py` for direct agent nudges; existing `app/services/redis_sse_broker.py` for activity-stream events.

**Rationale:** ax-backend CLAUDE.md says "All async work goes through `app/core/dispatch_queue.py`." Direct agent nudges are async work. Activity-stream events go through the existing SSE broker.

**Flow per fire:**
1. Worker locks reminder via `INSERT INTO task_reminder_runs ... ON CONFLICT DO NOTHING` (returns row count = 1 if locked).
2. If locked, mark `delivery_status='pending'`, build event payload per `spec.md` event vocabulary.
3. Emit activity-stream event via SSE broker (frontend renders).
4. Emit direct-agent dispatch via dispatch_queue (assignee's runtime listener consumes).
5. On both succeed: update `delivery_status='delivered'`, update `tasks.last_nudged_at = scheduled_for`, compute `next_reminder_at`.

**Note:** `tasks.last_nudged_at` is set to `scheduled_for`, not `fired_at`. Otherwise drift in the scheduler tick accumulates over time.

### Retry + DLQ

**Decision:** bounded retry (3 attempts, exponential backoff 30s/2min/10min). DLQ is a `delivery_status='dlq'` flag on `task_reminder_runs`, not a separate table.

**Why same-table DLQ:** simpler to query (`WHERE delivery_status = 'dlq'`), preserves audit relationship with the source reminder. Separate DLQ table is overkill for expected volume.

**DLQ alarm:** CloudWatch on `count(delivery_status='dlq' AND fired_at > now() - 1h) > 0`.

**DLQ resolution:** ops can flip back to `'pending'` after fixing root cause. CLI surface for this is `ax tasks reminder-runs --status dlq` + `ax tasks reminder-replay <run_id>` (in scope for the orion-side CLI consumer once backend ships).

### Idempotency model — full picture

| Layer | Mechanism |
|---|---|
| Scheduler tick | Distributed Redis lock prevents double-tick |
| Reminder claim | `INSERT INTO task_reminder_runs ... ON CONFLICT DO NOTHING` — row uniqueness |
| Event delivery | `event_id` UUID is stable per fire; consumers (frontend SSE, listener) dedupe by it |
| Ack/snooze | Idempotent in effect — reset cadence, audit-logged, multiple acks don't break the contract |

### Observability

**CloudWatch alarms** (per ChatGPT's explicit ask):

| Alarm | Threshold | Severity |
|---|---|---|
| Scheduler tick stale | `last_scheduler_tick_at > now() - 90s` | Page |
| Reminder backlog depth | `count(WHERE next_reminder_at < now() - 5min) > 50` | Warn → Page at 200 |
| Fire→delivery latency p95 | > 60s for 5min sustained | Warn |
| DLQ depth | `count(delivery_status='dlq' AND fired_at > now() - 1h) > 0` | Warn |
| Stale-task growth rate | rate of `task.stale` events > 2x 24h baseline | Info |
| Auto-cancel rate spike | rate of `task.auto_cancel.fired` > 2x 7d baseline | Warn |

**Dashboards:**
- Scheduler tick latency, ticks/min, fires/min
- Reminder cadence distribution (min/p50/p95 between fires per task)
- Escalation tier distribution (how often does work climb the chain?)
- Snooze rate per agent (signal: assignee overloaded?)

**Audit trail:** every mutation in `tasks` autonomy fields and every entry in `task_reminder_runs` is queryable and joined to `task_autonomy_audit` per `spec.md` §Auth.

## Dev-first principle (ChatGPT 17:39 UTC)

> "Jacob wants this shipped and validated in dev first. The architecture must explain how reminder loops run in local Docker and dev.paxai.app, then how the same behavior translates to production. Production may use different infrastructure, and EventBridge may make sense, but the dev/staging path must be concrete and fast."

**The contract from `spec.md` does NOT change between environments.** Events, idempotency, state machine, audit trail are platform-wide. What CAN change is **where the scheduler runs**.

### Local Docker (developer laptop)

- `docker-compose up` brings up the API container (existing); scheduler thread starts inside it under `ENABLE_TASK_AUTONOMY=1`.
- Postgres + Redis already in the compose file (per ax-backend CLAUDE.md).
- **Zero additional containers needed.** No separate worker, no cron sidecar.
- Tick cadence: 30s in dev (matches prod) — but configurable via `TASK_AUTONOMY_TICK_SECONDS` env for fast iteration during testing (e.g. set to `2` to run smokes in seconds).
- Pytest smokes run against the same in-process scheduler; no special test harness.
- **Failure mode for laptops:** dev box closes lid → scheduler stops → reminders pause silently. That's acceptable for dev. Prod-grade liveness is a prod concern.

### `dev.paxai.app` (shared dev/staging)

- Identical code path to local Docker.
- Single API container instance (per current dev architecture); single scheduler holds the Redis lock trivially.
- 24h soak test on dev validates everything before prod consideration.

### Production (`paxai.app`)

**Decision deferred to backend_sentinel's deployment-plan task** (per ChatGPT 17:39 UTC: "smallest reliable v1 that works locally/dev and does not block production hardening"). Plausible options the deployment plan should evaluate:

| Option | Pros | Cons |
|---|---|---|
| **Same in-process worker thread** | Zero new infra; identical to dev | Tied to API instance lifecycle; long ticks block API event loop briefly |
| **ECS long-running worker service** | Isolated from API; scales independently | New service surface, IAM, log lane |
| **ECS scheduled task (cron)** | Simpler than long-running | 30s minimum granularity from ECS scheduling; can't react to per-task cadence variability |
| **EventBridge Scheduler** | AWS-native, scales effortlessly | Per-task rule lifecycle is heavyweight; better for fixed cron, less for variable per-task |
| **EventBridge + SQS + Lambda consumer** | Decoupled, retryable, DLQ-native | More infra surface; cold-start latency on Lambda |
| **Redis sorted set + Lambda or worker** | Sub-second precision | Crash recovery + persistence story is more involved |

**Architectural invariant the deployment plan must preserve:**
- Same Postgres tables (`tasks` extensions + `task_reminder_runs`)
- Same idempotency model (`(task_id, scheduled_for)` row uniqueness; `event_id` UUID per fire)
- Same event vocabulary (the 11 SSE event types from `spec.md`)
- Same dispatch path (existing `dispatch_queue.py` for direct nudges, existing `redis_sse_broker.py` for activity stream)

If the prod scheduler swaps from in-process to EventBridge, no change is needed in the spec, the frontend, the CLI consumer, or the listener. Only the trigger source changes.

**Recommendation for v1:** ship the in-process worker thread to dev/staging AND prod initially. Migrate to EventBridge or ECS-scheduled-worker when scheduler load > current API instance can absorb (likely months out at current scale; the dev-first path is right for now).

backend_sentinel's deployment plan task (per ChatGPT 17:39) owns the prod decision. This architecture doc gates on the **invariants above** being preserved across whatever they pick.

## Dev/staging rollout

### Phase 1 — `dev/staging` only, feature-flagged

1. Backend PR (`3e665d6a`):
   - Alembic migration: `tasks` columns + `task_reminder_runs` table
   - Worker thread starts under `ENABLE_TASK_AUTONOMY=1` env flag
   - All 4 smokes from `spec.md` pass in CI before merge
2. Frontend PR (`103605b2`):
   - Activity-stream renderers for the 11 event types
   - Behind same `ENABLE_TASK_AUTONOMY` flag check on backend response shape
3. Orion PR (CLI consumer):
   - `ax tasks ack/snooze/pause/set/status` thin wrappers around new API
   - Lands in same week as backend PR
4. Continuous 24h soak on `dev.paxai.app`:
   - At least 5 test tasks with varied urgency
   - Confirm: every fire has a matching `task_reminder_runs` row; no duplicates; no missed fires
   - DLQ stays empty
   - p95 fire→delivery < 60s

### Phase 2 — `main` / `paxai.app` opt-in

5. Per-space opt-in: feature flag is space-attribute, not global. Pilot space gets it first; expand after 7d clean.

### Phase 3 — full rollout

6. Default-on for new spaces.
7. Backfill option for existing tasks (set `urgency='normal'`, `reminder_policy='default'` on opt-in by space owner).

## RC gates for `main` / `paxai.app`

Per ChatGPT's "main remains the guarded RC path." Promotion criteria:

1. **All 4 spec smokes pass continuously for 24h on `dev.paxai.app`** (basic cadence, snooze cap, due_at crossing, auto-cancel after escalation).
2. **Scheduler reliability:** zero stale-tick alarms in 24h soak; backlog p95 < 50.
3. **Delivery latency:** fire→delivery p95 < 60s; p99 < 5min.
4. **DLQ clean:** 0 entries in 24h.
5. **Audit integrity:** every fire in 24h has matching `task_reminder_runs` row + dispatch record + SSE event. No orphans.
6. **Frontend coherence:** `103605b2` consumes real backend state; no mocked widgets; all 11 event types render.
7. **CLI consumer green:** orion-side `ax tasks ack/snooze/pause/set/status` round-trips against the live API on dev.
8. **Observability live:** all 6 CloudWatch alarms armed; dashboards published; oncall runbook for DLQ + escalation-chain-exhaust scenarios.
9. **Rollback path:** `ENABLE_TASK_AUTONOMY=0` cleanly suppresses scheduler, returns task fields to no-op defaults; tested.
10. **@madtank signoff** per ax-cli + ax-backend CLAUDE.md.

## Infra needed — explicit answers

Per ChatGPT's explicit ask, here are the yes/no/why answers:

| Resource | Need it? | Why |
|---|---|---|
| **Backend worker thread** | ✅ YES | In-process scheduler; one thread per backend instance, distributed lock for cross-instance dedup |
| **Separate ECS service** | ❌ NO | No additional service; scheduler runs in existing API process. v2 if scale demands. |
| **AWS EventBridge** | ❌ NO | Per-task variability doesn't fit EventBridge's rule model without overengineering |
| **Redis stream** | ⚠️ EXISTING | Scheduler doesn't add a new stream; activity-stream events use existing `redis_sse_broker` |
| **Redis sorted set (scheduler)** | ❌ NO for v1 | Postgres polling sufficient. v2 candidate at >10k reminders/min. |
| **Redis distributed lock** | ✅ YES | `task_autonomy:scheduler_lock` key, NX EX 60s, ensures one lock-holder polls per tick across replicas |
| **Postgres polling** | ✅ YES | 30s tick, indexed `WHERE next_reminder_at <= now()` query |
| **Queue/DLQ** | ✅ YES | DLQ is a `delivery_status='dlq'` flag on `task_reminder_runs`; not a separate table |
| **CloudWatch alarms** | ✅ YES (6) | Scheduler stale, backlog depth, latency p95, DLQ depth, stale growth, auto-cancel spike |
| **New IAM roles** | ❌ NO | Existing API role has Postgres + Redis access |
| **New security groups** | ❌ NO | No new network surfaces |
| **Migrations** | ✅ YES | One Alembic migration for `tasks` columns + `task_reminder_runs` table |

Net: **one Alembic migration, one Redis lock key, six CloudWatch alarms, zero new services.**

## Mapping to upstream tasks

| Architecture concern | Owner task |
|---|---|
| Migration + scheduler thread + dispatch wiring | `3e665d6a` (backend_sentinel) |
| Stale detection + auto-cancel sweep | `3e68fd31` (backend_sentinel) |
| Frontend SSE event rendering | `103605b2` (frontend_sentinel) |
| Pre-impl Redis/infra audit (PR #45 / #32 drift) | `94b0c91c` (backend_sentinel) — per ChatGPT 17:37 |
| CLI consumer `ax tasks ack/snooze/pause/set/status` | (TBD orion task once backend slices) |
| End-to-end smoke validation | falls into `2598129a`-shape pattern when ready |

## Open questions / TODOs

- [ ] **Multi-region:** if backend deploys to >1 region, distributed lock needs to be cross-region or each region runs independently. Single-region for v1.
- [ ] **Tenancy:** scheduler currently global; per-space opt-in flag implies per-space scheduler scope. Filter the polling query by space-list of opted-in spaces? Performance acceptable up to ~100 spaces; rethink at 1000+.
- [ ] **Pause-during-quiet-hours:** should the platform offer "no nudges between 22:00 and 06:00 local time"? Useful for human assignees, less so for agents. Defer to post-v1.
- [ ] **Heartbeat interaction:** if `HEARTBEAT-001` ships, agent status of `sleeping` should suppress reminder fires until next wake. Need a join between `task.assigned_to` and `agent_state` at fire time.
- [ ] **Calendar integration:** none planned. Scope creep.

## Decision log

- **2026-04-26 17:37 UTC** — v1 platform architecture per ChatGPT `ba50f36e`. Companion to `spec.md` (`8e1a7ab3`). Maps existing CLI reminders surface, names what's inherited vs left behind, makes infra decisions with rejected-alternatives reasoning, defines 3-phase dev/staging/prod rollout, and answers ChatGPT's explicit yes/no/why infra checklist. Net infra footprint: 1 migration, 1 Redis lock key, 6 CloudWatch alarms, 0 new services.
