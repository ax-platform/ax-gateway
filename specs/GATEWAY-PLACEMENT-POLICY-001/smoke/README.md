# Placement-policy smoke test (task `2598129a`)

Validates that backend task `9e0286c1` (placement state + `/placement/ack` API + `agent.placement.changed` SSE) implements the contract in [`../spec.md`](../spec.md) lines 81–93.

**Boundary:** these are platform-only checks. They do not start, restart, or
otherwise touch the Gateway daemon. If the deployed Gateway lacks PR #110's
listener, the SSE event will fire but no listener will consume it — that's a
separate "missing redeploy" blocker, not a backend bug.

## Prerequisites

```bash
# A user-scope JWT (exchange a PAT first)
export AX_BASE_URL=https://dev.paxai.app
export AX_USER_PAT=axp_u_...                  # bootstrap PAT — exchanged below
export PROBE_AGENT_NAME=placement_probe       # any agent you own
export SPACE_A=12d6eafd-0316-4f3e-be33-fd8a3fd90f67
export SPACE_B=49afd277-78d2-4a32-9858-3594cda684af  # must be in your allowed_spaces
```

## What it checks

Per `GATEWAY-PLACEMENT-POLICY-001/spec.md` API table (lines 101–107) + state machine (lines 47–74):

1. **Read placement** — `GET /api/v1/agents/{id}/placement` returns the current record (or backfills from agent.space_id if backend hasn't shipped the dedicated endpoint yet).
2. **PATCH placement** — `PATCH /api/v1/agents/{id}/placement` with `{current_space, source, policy_revision}` transitions to `pending`.
3. **SSE emit** — subscribe to `/api/sse/messages`; expect an `event: agent.placement.changed` with payload matching the placement record (`agent_id`, `current_space`, `placement_state`, `policy_revision`).
4. **Ack endpoint shape** — `PATCH /api/v1/agents/{id}/placement/ack` accepts `{placement_state, runtime_pid?, ack_at}` and returns 2xx (or 403 if Gateway-attestation is enforced — that's expected; document the failure mode without flagging it as red).
5. **Round-trip timing** — placement state should reach `applied` within 5s of the PATCH, per spec smoke #1 (line 156).

## Running

```bash
python3 smoke_placement.py            # prints PASS/FAIL per check
python3 smoke_placement.py --verbose  # dumps response bodies
```

Exit codes:

- `0` — all checks pass
- `1` — at least one check failed; see stderr for diff vs. spec
- `2` — environment/auth setup wrong (missing token, can't exchange JWT, etc.)

## Expected output (happy path)

```
[1/5] Exchange PAT → JWT                                            ✓
[2/5] Resolve agent placement record                                ✓
[3/5] PATCH /placement → space-B (transition to pending)            ✓
[4/5] SSE: receive agent.placement.changed event                    ✓
[5/5] PATCH /placement/ack → 2xx                                    ✓ (or 403 expected)

PASS — backend contract for 9e0286c1 matches spec.
```

## When backend's shape diverges

If a check fails, the script emits the actual shape vs. the spec-expected shape:

```
[3/5] PATCH /placement → space-B (transition to pending)            ✗
  expected response field: placement_state="pending"
  actual response field:   "status"="ok"  (no placement_state field)
```

That's the right kind of failure mode — actionable for backend_sentinel, doesn't
require interpretation, doesn't gate on my listener PR (#110) being deployed.
