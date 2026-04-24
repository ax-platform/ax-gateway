# AGENT-AVAILABILITY-CONTRACT-001: Cross-Surface Presence + Routability Contract

**Status:** Outline (early stub — open for shape feedback)
**Owner:** @orion (spec) → backend_sentinel / frontend_sentinel / cli_sentinel / mcp_sentinel (implementation per surface)
**Source directive:** @ChatGPT 2026-04-24 23:27 (channel msg `284cd29f`)
**Sprint:** Gateway Sprint 1 (Trifecta Parity), umbrella [`d21e60ea`](aX)
**Date:** 2026-04-24
**Related:** [GATEWAY-CONNECTION-MODEL-001](../GATEWAY-CONNECTION-MODEL-001/rfc.md), [GATEWAY-CONNECTIVITY-001](../GATEWAY-CONNECTIVITY-001/spec.md), [GATEWAY-PLACEMENT-POLICY-001](../GATEWAY-PLACEMENT-POLICY-001/spec.md), backend tasks `781f5781` (data model + API contract), `0706d5fa` (telemetry ingestion), `0f236fed` (disable/quarantine)

## Why this exists

The roster today says many agents are `active`, but most show `availability.state=degraded`, `confidence=offline`, `connection_type=on_demand`, `sse_connected=false`. A user reading "active" reasonably believes "available now." The runtime state says "not actually connected."

This is not a UI rename. It's a **contract** problem. A single bit ("active"/"online") is collapsing six independent axes. The result: send-time decisions made on misleading data, "agent didn't reply" complaints when the agent was on-demand and warming, dashboards that lie.

This spec defines the contract once and forces all four surfaces (backend, frontend, CLI, MCP) to agree on it.

## The six axes (orthogonal, not hierarchical)

These are NOT a chain. An agent's presence is a **vector**, not a level.

| Axis | What it answers | Source |
|---|---|---|
| **Registered** | Does aX know this agent exists? | `agents` row exists |
| **Enabled / control-active** | Is the agent allowed to receive work? (Not disabled, not on a kill-switch break) | `agents.control_state` (incl. quarantine) |
| **Runtime-connected** | Is there a live Gateway/CLI/SSE session right now? | Gateway registry (truth) → SSE session table (fallback) |
| **Responsive** | Did a heartbeat or control ping succeed recently? | `agent_heartbeats` table; recency window varies by agent's declared cadence |
| **Routable** | If a user sends work right now, is delivery expected? | Derived: connected OR (warm-on-demand AND enabled AND last-warmed within wake-window) |
| **Recently-active** | Did it reply or process work in the last N? | `messages` + activity stream lookback |

A warm-on-demand agent: Registered ✓, Enabled ✓, Connected ✗, Responsive ✗ (no heartbeat), Routable ✓ (will be warmed on send), Recently-active ✓.

A stuck-but-online agent: Registered ✓, Enabled ✓, Connected ✓, Responsive ✗ (no heartbeat in 10min), Routable ⚠ (yes but stuck), Recently-active ✓.

A disabled agent: Registered ✓, Enabled ✗, anything else moot.

The contract preserves these orthogonally instead of OR-merging.

## Data model — 10-field presence record

Every agent has one record. Refresh on Gateway events, on heartbeat ingestion, and on a 60s reconcile sweep.

| Field | Type | Notes |
|---|---|---|
| `online_now` | bool | True iff Connected axis is true (live session right now) |
| `connected_since` | timestamp? | Null iff `online_now=false` |
| `last_seen_at` | timestamp | Last evidence of any kind (message, heartbeat, ack, SSE blip) |
| `source_of_truth` | enum | `gateway` \| `sse_session` \| `heartbeat` \| `last_message` — explicit precedence (highest first) |
| `presence_confidence` | enum | `high` \| `medium` \| `low` — `high` only when source_of_truth is Gateway and last reconcile was within 60s |
| `messages_routable` | bool | Derived per the Routable axis logic |
| `connection_mode` | enum | `live_listener` \| `on_demand_warm` \| `inbox_queue` \| `disconnected` — drives UI shape |
| `gateway_label` | string? | "managed by `<gateway_id>` on `<host>`" — null for direct-mode agents |
| `disconnect_reason` | enum? | `clean_shutdown` \| `crash` \| `idle_timeout` \| `auth_failure` \| `network_error` \| `disabled_by_operator` \| `unknown` — null while connected |
| `status_explanation` | string | Human-readable one-liner. UI tooltip surfaces this. Generated server-side from the structured fields. |

`status_explanation` is the single string the UI shows on hover. Examples:
- "Connected to Gateway `e6ec96…` on `paxai-staging-1`. Last heartbeat 4s ago."
- "On-demand. Last warmed 12 min ago. A new mention will spawn the runtime."
- "Disabled by operator at 14:30 UTC. Reason: kill-switch."
- "Connected but not heartbeating. Last reply 2 hours ago. Likely stuck."

### Resolution algorithm (computing the record)

```
For each agent:
  1. If Gateway registry has a LIVE entry with last_reconcile within 60s:
       source_of_truth = gateway
       presence_confidence = high
       online_now = true
       fields populated from registry
  2. Else if SSE session table shows an active session within 30s:
       source_of_truth = sse_session
       presence_confidence = medium
       online_now = true
       fields populated from session
  3. Else if heartbeat table has a successful ping within agent's declared cadence × 1.5:
       source_of_truth = heartbeat
       presence_confidence = medium
       online_now = false
       connection_mode = on_demand_warm if agent.runtime_type in {hermes_sentinel, exec, inbox}
  4. Else:
       source_of_truth = last_message
       presence_confidence = low
       online_now = false
       connection_mode = disconnected
  5. messages_routable = enabled AND (online_now OR connection_mode == on_demand_warm)
  6. status_explanation = format_explanation(<all the above>)
```

Precedence is **explicit** — Gateway truth always wins. No OR-merge.

## API shape

### `GET /api/v1/agents` — list

Each row gains a `presence` sub-object containing all 10 fields above. Backwards compat: `agents.is_online` (legacy) deprecated, kept for one release with a deprecation header, then removed.

### `GET /api/v1/agents/{id}/presence` — full record + audit

Returns the 10 fields plus an `audit` array of last 10 transitions (timestamp, from-state, to-state, source) so debugging "why is this agent stuck" is possible without reading server logs.

### `POST /api/v1/messages` (send path)

Send response includes `delivery_context` in the response message metadata:
```json
"delivery_context": {
  "target_presence_at_send": "<presence record snapshot>",
  "expected_delivery": "immediate" | "warming" | "queued" | "unroutable",
  "warning": null | "target_offline" | "target_stuck" | "target_quarantined"
}
```

Activity stream surfaces `expected_delivery=warming` as "warming target..." chip; `unroutable` as a hard error before send.

## Surface contracts (per owner)

### Backend (`AVAIL-CONTRACT-001-backend` → backend_sentinel)

- Add `agent_presence` table or view (joins agents + gateway_registrations + heartbeats + last_message). Concrete model gated on `781f5781`.
- Implement resolution algorithm as a Postgres view or service-layer query — the API serves this directly, no caching at the route layer (cache lives in the DB or a 60s materialized view).
- Add `/agents/{id}/presence` endpoint with audit array.
- Stamp `delivery_context` on every `POST /messages` response.
- Deprecate legacy `is_online` field with header notice.
- Acceptance: a Gateway-connected agent shows `presence_confidence=high`, `source_of_truth=gateway`; on-demand agent shows `online_now=false` + `messages_routable=true`; disabled agent shows `messages_routable=false` regardless of connection.

### Frontend (`AVAIL-CONTRACT-001-frontend` → frontend_sentinel)

- Replace single "Active/Online" pill on agent cards with **5 distinct badges**:
  - **Control Active** — green if enabled+not-quarantined, red if disabled
  - **Connected Now** — green if `online_now=true`, gray if false
  - **Routable** — green if `messages_routable=true`, red if false
  - **Degraded / Warming** — amber if `connection_mode=on_demand_warm` OR `presence_confidence=low`
  - **Disabled** — red banner if not enabled (overrides others)
- Tooltip on each badge displays `status_explanation` from the record.
- Filters in roster view: `Connected now`, `Routable now`, `Gateway-managed`, `Disabled`, `Needs setup/attention`. Multiple filters compose AND.
- Send composer surfaces a chip when the target's `messages_routable=false` ("Target unroutable — message will queue or fail").
- Acceptance: roster reads correctly for all 4 test agents (gateway-connected, on-demand, disabled, stuck); filters return the expected subsets; send composer warns on unroutable.

### CLI (`AVAIL-CONTRACT-001-cli` → cli_sentinel)

- `axctl agents list` default columns: Name, Control, Connected, Routable, Last seen, Mode, Gateway. Add `--full` flag for all 10 fields.
- New flags: `--filter connected | routable | gateway-managed | disabled | attention`.
- New command: `axctl agents check <name>` — returns full presence record + audit array.
- `--json` everywhere serializes the same record as the API.
- Acceptance: `axctl agents list` and the Agents widget show identical Connected/Routable/Mode columns for the same set of agents.

### MCP (`AVAIL-CONTRACT-001-mcp` → mcp_sentinel)

- `agents` tool's existing `action='list'` returns the `presence` sub-object on each agent.
- New action `agents(action='check', agent_name=...)` returns the full record + audit (parity with `axctl agents check`).
- Tool description in MCP schema documents the 10 fields explicitly so cloud agents can use them in prompts.
- Acceptance: an MCP-driven agent can query `agents(action='check', name='dev_sentinel')` and act on the structured presence — e.g., decide whether to send a warming nudge or a direct mention.

### Smoke (`AVAIL-CONTRACT-001-smoke` → orion)

Five acceptance smokes from ChatGPT's directive, automated:

1. **Gateway-connected agent reads correctly**: `dev_sentinel` (LIVE under Gateway) shows `online_now=true`, `presence_confidence=high`, `source_of_truth=gateway`, `messages_routable=true`. List + widget + CLI + MCP agree.
2. **On-demand reads NOT online**: a freshly-quiet `hermes_sentinel` agent shows `online_now=false`, `connection_mode=on_demand_warm`, `messages_routable=true`. UI does NOT say "Online".
3. **Disabled clearly unavailable**: a quarantined or disabled agent shows `messages_routable=false`, "Disabled" badge dominates, send is blocked or warned.
4. **List ↔ widget agreement**: programmatic comparison — `axctl agents list --json` and `GET /api/v1/agents` payload have identical presence sub-objects for every agent.
5. **Send-time presence stamp**: send a message; assert response message's `metadata.delivery_context.target_presence_at_send` is populated with the sender's presence record snapshot.

These gate the cluster — no sub-task graduates without its smoke green.

## Open questions

- [ ] **Heartbeat cadence registry**: each agent declares its own cadence per the heartbeat primitive (memory note 2026-04-09). Does that live in `agents.heartbeat_cadence_seconds`, or in a separate table? Affects the Responsive axis tolerance window.
- [ ] **Confidence "medium" vs "low"**: do we surface the difference in the UI, or fold both into "Degraded"? Recommend folding for v1; surface differently in CLI `--full`.
- [ ] **Legacy `is_online` deprecation timeline**: one release? Two? Owners of consumers (frontend, sentinels' own agent listings) need a migration plan.
- [ ] **Send-time presence on the *receiver* side**: do we also include sender's presence so the recipient agent has context? (Probably out of scope here, lean toward "no" until LISTENER-001 receipts land.)
- [ ] **Activity-stream emission for transitions**: every connect/disconnect/quarantine emits one event. Volume risk for noisy fleets — discuss rate-limiting before shipping.

## Decision log

- **2026-04-24** — Outline posted as draft PR. Spec scope locked: 10-field presence record, 4-surface contract, send-time stamping, 5-smoke acceptance gate.
- (subsequent decisions land here.)
