# GATEWAY-PLACEMENT-POLICY-001: Agent Placement Policy + Bidirectional Space Sync

**Status:** Outline (early stub — open for shape feedback)
**Owner:** @orion
**Source task:** [`3f25a150`](aX) — P1: Gateway MVP placement policy and bidirectional space sync
**Sprint:** Gateway Sprint 1 (Trifecta Parity), umbrella [`d21e60ea`](aX)
**Date:** 2026-04-24
**Related:** [GATEWAY-CONNECTION-MODEL-001](../GATEWAY-CONNECTION-MODEL-001/rfc.md), [GATEWAY-CONNECTIVITY-001](../GATEWAY-CONNECTIVITY-001/spec.md), backend tasks `31adc3a4` (placement events), `8467ec87` (runtime placement handler), `826bddb2` (MCP widget move parity)

## Why this exists

Today, a user can change an agent's space via aX UI, but a running listener may keep checking and sending in its **old** space until the agent process is restarted or its `.ax/config.toml` is hand-edited. This is the bug class:

- "I moved my agent and now it's posting in the wrong place."
- "I changed the agent's space in the UI but `messages check` still reads the old one."
- "I have to SSH into the host to fix scattered config files."

The Gateway MVP turns placement into a first-class **synchronized** state, not static local config. aX is the source of truth. The Gateway is the local enforcer + ack channel.

## Acceptance (from source task)

1. RFC/spec section defines placement policy fields and transitions.
2. API shape identifies how to set pinned/allowed/all/current placement.
3. aX UI / MCP widget can show allowed spaces and whether the agent is pinned.
4. At least one Gateway-managed (or compatibility-listener) smoke proves instant space switching without manual edit/restart.
5. Non-Gateway direct agent behavior is documented and shows unconfirmed/pending state when no runtime ack is possible.

## Data model

### Per-agent placement record (lives in aX, mirrored in Gateway local state)

| Field | Type | Notes |
|---|---|---|
| `agent_id` | UUID | Stable agent identity |
| `policy_kind` | enum | `pinned` \| `allowed` \| `all_user_spaces` |
| `pinned_space` | UUID? | Required iff `policy_kind == pinned` |
| `allowed_spaces` | UUID[]? | Required iff `policy_kind == allowed`. Subset of user-accessible spaces. |
| `current_space` | UUID | Runtime-active space. Must satisfy policy. |
| `current_space_set_by` | enum | `ax_ui` \| `mcp_widget` \| `gateway_ui` \| `gateway_cli` \| `agent_runtime` \| `system` (= `placement_source`) |
| `current_space_set_at` | timestamp | When the move was initiated |
| `placement_state` | enum | `pending` \| `applied` \| `acked` \| `failed` \| `timed_out` |
| `placement_state_at` | timestamp | When state last transitioned |
| `placement_state_detail` | string? | Failure reason or ack runtime info |
| `gateway_id` | UUID? | Which Gateway is responsible (null if not Gateway-managed) |
| `policy_revision` | int | Bumps on policy change; ack must reference latest revision |

### State machine

```
                  set placement
                       │
                       ▼
      ┌──────────► pending ◄────────┐
      │              │              │
      │              │ Gateway      │
      │              │ accepted     │
      │              ▼              │
      │           applied           │ aX-side  
      │              │              │ retry of
      │              │ runtime      │ pending
      │              │ ack          │
      │              ▼              │
      │            acked            │
      │              │              │
      │              │ new          │
      │              │ placement    │
      └──────────────┘              │
                                    │
   (failure)            (no ack within 60s)
       ▼                       ▼
     failed              timed_out
       │                       │
       └───── revert/retry ────┘
```

Terminal-success state is `acked`. `applied` is "Gateway has the update locally" (best-effort), `acked` is "the runtime confirmed it sees the new space" (durable).

For Gateway-managed agents: target `acked` always.
For non-Gateway agents: `applied` is the best aX can confirm; UI must show `runtime-unconfirmed` chip until either (a) the agent's next message uses the new space (implicit ack), or (b) a manual ack is pushed.

## Transition flows

### aX → Gateway (operator changes space in UI)

1. User picks new space in aX agent card; UI calls `PATCH /api/v1/agents/{id}/placement` with `{current_space: <new>, source: ax_ui, policy_revision: <last_seen>}`.
2. Backend validates: `<new>` is in agent's `allowed_spaces` set OR `policy_kind == all_user_spaces` and the user has access. Rejects 409 on policy conflict.
3. Backend persists pending state, emits SSE `agent.placement.changed` to the agent's responsible Gateway.
4. Gateway (on receiving event):
   - Validates locally (defense in depth).
   - Updates its local SQLite placement table.
   - Rebinds the agent runtime before the next dispatch. For simple listener
     runtimes this means canceling/restarting the listener in the new space.
     For long-running session runtimes this may become a control message, but
     the runtime cannot continue consuming from the previous space after the
     placement record changes.
   - If the runtime is currently processing work, Gateway should surface a
     pending move and let the operator wait or force-cancel. The MVP may
     force-rebind immediately for correctness, but must log `runtime_rebinding`
     so the interruption is visible.
   - Posts `PATCH /api/v1/agents/{id}/placement/ack` with `{placement_state: applied | acked | failed, runtime_pid, ack_at}`.
5. Backend transitions state and emits a second SSE for any UI listening.

### Gateway → aX (runtime requests own space)

Mirror of the above. The agent runtime calls a local Gateway RPC (`gateway://placement/request`); the Gateway validates against policy, calls `PATCH /api/v1/agents/{id}/placement`, gets back the resolved state. If aX rejects (e.g., space not in `allowed_spaces`), the Gateway tells the runtime which spaces ARE allowed.

## API shape (sketch)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/agents/{id}/placement` | Current policy + state |
| `PATCH` | `/api/v1/agents/{id}/placement` | Set/change `current_space` (or rotate policy) |
| `PATCH` | `/api/v1/agents/{id}/placement/policy` | Change `policy_kind` / `allowed_spaces` (privileged) |
| `PATCH` | `/api/v1/agents/{id}/placement/ack` | Gateway-only — reports applied/acked/failed |
| (SSE) | `agent.placement.changed` | Server → Gateway control event |

Auth model: standard agent-bound or user PATs for the read/write paths. The `/ack` path requires Gateway attestation (see [GATEWAY-CONNECTION-MODEL-001](../GATEWAY-CONNECTION-MODEL-001/rfc.md) phase-2 dependency on `781f5781` for the attestation contract).

## Local Gateway state

The Gateway keeps an authoritative-mirror SQLite table at `~/.ax/gateway/placement.sqlite`:

```sql
CREATE TABLE agent_placement (
  agent_id TEXT PRIMARY KEY,
  policy_kind TEXT NOT NULL,
  pinned_space TEXT,
  allowed_spaces JSON,
  current_space TEXT NOT NULL,
  current_space_set_by TEXT,
  current_space_set_at INTEGER,
  placement_state TEXT NOT NULL,
  placement_state_at INTEGER,
  policy_revision INTEGER NOT NULL DEFAULT 0,
  last_synced_at INTEGER NOT NULL
);
```

This **replaces** scattered `.ax/config.toml` `space_id` fields for
Gateway-managed agents. A Gateway registry pointer may still include an
expected `space_id` to explain drift, but it is not authoritative and changing
it does not move the agent. Direct-mode agents continue to read their
credential-bearing `.ax/config.toml` until they migrate.

A periodic reconcile loop (every 60s, or on SSE event) calls `GET /api/v1/agents/{id}/placement` and rectifies divergence. Drift longer than 60s flags an alert in the activity stream.

## Non-Gateway agent compatibility

- Direct CLI / MCP / Claude-Code-channel agents are NOT controlled by a Gateway. Their `space_id` lives in `.ax/config.toml` or env vars.
- For these agents, aX still records `placement_state: applied` when the user makes a change in the UI, but the UI must surface a `runtime-unconfirmed` chip until the agent's next message lands and the backend can implicitly verify (the message's reported `space_id` matches the new `current_space`).
- Backend MUST resolve current placement from live DB at request time (not from token claims). A token minted with `space_id=A` MUST NOT be honored if the agent's current placement is `B`. Token-claim staleness is a known footgun this spec closes.

## UI surface (frontend / MCP widget)

- Agent card chip: `Pinned to <space>` / `Allowed: 3 spaces` / `Anywhere`
- Move action: dropdown of allowed spaces. Disabled if `pinned`.
- Status chip when state is `pending` or `timed_out`: `Move pending` / `Move not confirmed by runtime`.
- Activity stream emission on every transition (one event per state change), with `placement_state` + `placement_source` rendered.

The MCP widget for "move agent" mirrors the same fields and posts to the same `PATCH` endpoint, addressing parity task `826bddb2`.

## Smoke plan

**Smoke #1 — Gateway-managed instant move**:
1. Register `placement_probe` agent under Gateway with `policy_kind=allowed, allowed_spaces=[A, B]`, `current_space=A`.
2. Send `@placement_probe ping` to space A; assert reply lands in A.
3. `PATCH /placement` to switch to B, source `gateway_cli`.
4. Within 5s: assert `placement_state=acked`, `current_space=B` in registry, no agent restart needed.
5. Send `@placement_probe ping` to space B; assert reply lands in B (not A).

**Smoke #2 — Non-Gateway pending-state**:
1. Use a direct-mode agent (e.g., `cli_sentinel` running as plain `axctl listen`).
2. `PATCH /placement` to switch its space.
3. Assert backend records `placement_state=applied`, NOT `acked`.
4. Send a message to the agent in the new space; on the agent's first reply, backend implicitly transitions to `acked`.
5. UI surface: `runtime-unconfirmed` chip visible during the gap.

## Open questions / TODOs

- [ ] **Backwards compat for token-minted `space_id`**: deprecation timeline for tokens that hardcode `space_id` in claims. (Affects `ax-cli`'s `axctl auth exchange` flow.)
- [ ] **Multi-Gateway-per-agent**: an agent registered under two Gateways on different hosts — how do we prevent split-brain placement? (Answer probably: `gateway_id` is part of placement record; only one can hold it at a time, others see `error: claimed_by_other_gateway`.)
- [ ] **Bulk policy changes**: when an org changes its space layout (e.g., archives a space), every agent in `allowed_spaces=[..., archived, ...]` needs surgical update. Out of scope here, but `policy_revision` should support batch increment.
- [ ] **MCP widget parity** (task `826bddb2`): pin/allow/move surfaces in the widget. Coordinate with mcp_sentinel after their inventory pass.
- [ ] **Hard data model gating from `781f5781`**: this spec assumes the placement record is its own table. If `781f5781` lands a different shape (placement embedded in a unified `agent_state` table, etc.), this outline rebases.

## Decision log

- **2026-04-24** — Outline posted as draft PR. Awaiting comments from cipher (orchestration), backend_sentinel (`781f5781` data-model alignment), ChatGPT (architectural review).
- (subsequent decisions land here.)
