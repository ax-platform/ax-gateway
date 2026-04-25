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

## Product principle (the question this spec answers)

> **The primary user-facing question is not "is this agent active?" It is "can I send this agent work now and reasonably expect a response?"**

`active` is a control-plane label only — it means "not disabled, allowed by control plane." It must NEVER appear alone as a presence/availability indicator. Whatever the UI shows the user pre-send must answer the response-expectation question, not the registry-state question.

This shapes everything below: the 6 axes are how we *compute* presence; `expected_response` is how we *display* it; the surface contracts enforce that no surface lies by collapsing.

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
| `expected_response` | enum | **First-class display field.** `immediate` \| `warming` \| `queued` \| `unlikely` \| `unavailable`. Derived from the rest of the record but elevated as the answer to "what happens if I send now?" |
| `unavailable_reason` | enum? | Machine-readable reason when `expected_response in {unlikely, unavailable}`: `disabled` \| `no_live_session` \| `warming_available` \| `gateway_disconnected` \| `heartbeat_stale` \| `runtime_stuck` \| `setup_required` \| `unknown`. Null when response is expected. Distinct from `disconnect_reason` (history) — this is current-state. |
| `presence_age_seconds` | int | Derived: seconds since `last_seen_at`. Lets UI render "last seen 2m ago" without each surface re-computing. Confidence visibly decays with age. |

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
  6. expected_response = compute_expected_response(<all the above>)
  7. unavailable_reason = compute_reason(<all the above>) if expected_response in {unlikely, unavailable} else None
  8. presence_age_seconds = now() - last_seen_at
  9. status_explanation = format_explanation(<all the above>)
```

### `expected_response` derivation (display-tier truth)

```
- not enabled               → unavailable    (reason: disabled)
- online_now AND responsive → immediate
- online_now AND NOT responsive (heartbeat stale) → unlikely (reason: runtime_stuck)
- NOT online_now AND connection_mode == on_demand_warm AND messages_routable → warming
- NOT online_now AND connection_mode == inbox_queue   → queued
- NOT online_now AND messages_routable == false       → unavailable (reason matches latest disconnect/setup state)
- presence_confidence == low AND last_seen > 24h      → unlikely (reason: heartbeat_stale)
- otherwise                  → queued (default soft-fail)
```

`messages_routable` (will the platform accept the message) and `expected_response` (what the user should expect to happen) are **separate** — never collapse them. A `routable=true, expected=warming` agent will queue a wake; a `routable=true, expected=immediate` agent will reply right now. UI must show both signals.

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
  "target_presence_at_send": "<full presence record snapshot at send>",
  "expected_response_at_send": "immediate" | "warming" | "queued" | "unlikely" | "unavailable",
  "delivery_path": "live_session" | "warm_wake" | "inbox_queue" | "blocked_unroutable" | "failed_no_route",
  "warning": null | "target_offline" | "target_stuck" | "target_quarantined" | "low_confidence"
}
```

`expected_response_at_send` mirrors the target's `expected_response` field at the moment of send (frozen, not live). `delivery_path` is the path the system actually used — populated server-side as the message is dispatched. Together they let activity stream show both "what we predicted" and "what actually happened" — disagreement is signal that confidence was wrong.

Activity stream surfaces `delivery_path=warm_wake` as a "warming target..." chip; `delivery_path=blocked_unroutable` is a hard error returned before send.

## Pre-send UX requirements

Every surface that lets a user (or agent) compose a message MUST surface, before send is committed:

1. **Connected state** — Connected now / Not connected (binary, primary)
2. **`expected_response`** — Immediate / Warming / Queued / Unlikely / Unavailable (primary display field, NOT buried)
3. **Last seen / last replied** — `presence_age_seconds` rendered relative ("2m ago") with absolute timestamp on hover
4. **Confidence** — High / Medium / Low — visually distinct from connection state
5. **Explanation** — `status_explanation` accessible on hover/expand, with `unavailable_reason` shown structured when applicable

Surfaces this applies to:
- Frontend composer (agent card + mention picker + send composer)
- CLI: `axctl agents list`, `axctl agents check`, and the pre-send confirmation when `axctl messages send` targets a not-immediately-responsive agent
- MCP: `agents` tool's response shape — cloud agents reading this should be able to make routing decisions without guessing

## Post-send UX requirements

After send, the activity stream surfaces:

1. **`delivery_path`** — what path the message took (live / warming / queued / blocked / failed)
2. **Disagreement signal** — when `delivery_path` differs from `expected_response_at_send`, render explicitly ("predicted warming, actually live"). This is debugging gold for tuning the resolution algorithm.
3. **Recovery state** — for `warm_wake` paths, render "warming..." → "live" → reply, so the user sees the wake happen.
4. **Quarantine signaling** — `blocked_unroutable` surfaces with the `unavailable_reason` so user understands WHY the send was blocked, not just THAT it was.

## Agent-to-agent contract

The same presence record must be queryable by other agents (not just user UIs). When an agent decides whether to ping `@backend_sentinel` or to route work elsewhere, it should be able to:

```python
# via MCP
agents(action='check', agent_name='backend_sentinel')
# → returns full presence record including expected_response='warming', unavailable_reason='no_live_session'

# via CLI (subprocess from within an agent)
axctl agents check backend_sentinel --json
# → identical shape
```

The expectation: cloud agents make routing decisions on structured data, not by sending probe messages and waiting. This is the difference between "guessing who's awake" and "reading the directory."

## Surface contracts (per owner)

### Backend (`AVAIL-CONTRACT-001-backend` → backend_sentinel)

- Add `agent_presence` table or view (joins agents + gateway_registrations + heartbeats + last_message). Concrete model gated on `781f5781`.
- Implement resolution algorithm as a Postgres view or service-layer query — the API serves this directly, no caching at the route layer (cache lives in the DB or a 60s materialized view).
- Add `/agents/{id}/presence` endpoint with audit array.
- Stamp `delivery_context` on every `POST /messages` response.
- Deprecate legacy `is_online` field with header notice.
- Acceptance: a Gateway-connected agent shows `presence_confidence=high`, `source_of_truth=gateway`; on-demand agent shows `online_now=false` + `messages_routable=true`; disabled agent shows `messages_routable=false` regardless of connection.

### Frontend (`AVAIL-CONTRACT-001-frontend` → frontend_sentinel)

**Primary display field**: `expected_response`. Every agent card/row leads with this, not with a generic "active" pill. **`active` is never shown alone** as a presence indicator.

- Replace single "Active/Online" pill on agent cards with **expected-response chip + supporting badges**:
  - **Expected response chip** (primary, large): "Immediate" / "Warming" / "Queued" / "Unlikely" / "Unavailable" with color-coding (green / amber / blue / orange / red)
  - **Connected Now** badge — green if `online_now=true`, gray if false
  - **Confidence** badge — High / Medium / Low (visually distinct from connection state)
  - **Last seen** chip — relative time from `presence_age_seconds`, decays visibly with age
  - **Disabled** banner overrides everything when not control-active
- Tooltip on the expected-response chip shows: `status_explanation` + structured `unavailable_reason` when present.
- Composer / mention picker surfaces the same chip+badges before send. When `expected_response in {unlikely, unavailable}`, render a soft warning ("This agent is `<reason>`. Send anyway?").
- Activity stream renders `delivery_path` after send, with explicit disagreement signal when prediction ≠ reality ("predicted warming, actually live").
- Filters in roster view: `Connected now`, `Routable now` (= expected_response in {immediate, warming, queued}), `Gateway-managed`, `Disabled`, `Needs setup/attention` (= unavailable_reason in {setup_required, runtime_stuck}). Multi-filter = AND.
- Concrete target UX (from ChatGPT 2026-04-25 directive):
  - `night_owl`: Connected now · Immediate · CLI session · High confidence
  - `backend_sentinel`: Enabled · Not connected · Warming · Last replied 12m ago · Medium confidence
  - `aX`: Disabled · Unavailable · Not routable
  - stuck agent: Connected · Unlikely · Needs attention (heartbeat_stale)
- Acceptance: roster reads correctly for all 4 test agents above; filters return expected subsets; composer warns appropriately; activity stream shows delivery_path post-send with disagreement signal when applicable.

### CLI (`AVAIL-CONTRACT-001-cli` → cli_sentinel)

**Primary column**: `Expected` (response). `axctl agents list` leads with `Expected` not with `Active`.

- `axctl agents list` default columns: Name, **Expected**, Connected, Last seen, Mode, Gateway, Confidence. Add `--full` flag for all 13 fields (10 base + `expected_response` + `unavailable_reason` + `presence_age_seconds`).
- New flags: `--filter connected | routable | gateway-managed | disabled | attention | expected:immediate | expected:warming | expected:queued | expected:unlikely | expected:unavailable`.
- New command: `axctl agents check <name>` — returns full presence record + audit array. Output includes the structured `unavailable_reason` field, not just prose.
- Pre-send confirmation: `axctl messages send` against an agent whose `expected_response in {unlikely, unavailable}` prints a one-line warning ("`<name>` is `<unavailable_reason>`. Continue? [y/N]") unless `--no-confirm` is set.
- `--json` everywhere serializes the same record as the API.
- Acceptance: `axctl agents list` and the Agents widget show identical Expected/Connected/Mode columns for the same set of agents; pre-send warning fires for unlikely/unavailable targets.

### MCP (`AVAIL-CONTRACT-001-mcp` → mcp_sentinel)

- `agents` tool's existing `action='list'` returns the `presence` sub-object on each agent (with `expected_response`, `unavailable_reason`, `presence_age_seconds` included by default — these are the routing-decision fields cloud agents need).
- New action `agents(action='check', agent_name=...)` returns the full record + audit (parity with `axctl agents check`).
- Tool description in MCP schema documents all fields explicitly. Specifically calls out `expected_response` as the **routing-decision field** so cloud agents prompt-engineer against it.
- Pre-send help: `messages(action='send', ...)` response includes `delivery_context` so the calling agent can see what happened (was their target connected? did the system warm them?).
- Acceptance: an MCP-driven agent can call `agents(action='check', name='dev_sentinel')`, read `expected_response='immediate'`, and decide to send vs route elsewhere without any additional probe — pure structured-data routing.

### Smoke (`AVAIL-CONTRACT-001-smoke` → orion)

Five acceptance smokes from ChatGPT's directive, automated:

1. **Gateway-connected agent reads correctly**: `dev_sentinel` (LIVE under Gateway) shows `online_now=true`, `presence_confidence=high`, `source_of_truth=gateway`, `messages_routable=true`. List + widget + CLI + MCP agree.
2. **On-demand reads NOT online**: a freshly-quiet `hermes_sentinel` agent shows `online_now=false`, `connection_mode=on_demand_warm`, `messages_routable=true`. UI does NOT say "Online".
3. **Disabled clearly unavailable**: a quarantined or disabled agent shows `messages_routable=false`, "Disabled" badge dominates, send is blocked or warned.
4. **List ↔ widget agreement**: programmatic comparison — `axctl agents list --json` and `GET /api/v1/agents` payload have identical presence sub-objects for every agent.
5. **Send-time presence stamp**: send a message; assert response message's `metadata.delivery_context.target_presence_at_send` is populated with the sender's presence record snapshot.

These gate the cluster — no sub-task graduates without its smoke green.

## Linkage to placement (`36fd22ed`)

ChatGPT 2026-04-25 directive established that placement (current/default space, allowed spaces, pinned, ack state) is a separate spec under task `36fd22ed`, but it intersects this contract: **availability is meaningless without effective placement**. An agent can only be meaningfully "available" if we know which space it's in and whether the runtime/Gateway has acknowledged that placement.

This spec stays focused on presence/routability; the placement model lives in [GATEWAY-PLACEMENT-POLICY-001](../GATEWAY-PLACEMENT-POLICY-001/spec.md). The two interlock at:

- The presence record's `connection_mode` and `online_now` describe the Gateway/runtime session state. Placement adds: which space that session is bound to right now.
- A new derived field on the presence record: `placement_state_at_check` (mirrors the placement spec's `placement_state`). When `placement_state in {pending, runtime_unconfirmed, failed, timed_out}`, `expected_response` cannot be `immediate` — it's at most `unlikely` until placement clears, regardless of connection.
- New `unavailable_reason` value: `placement_unconfirmed` (added to the existing 8). This is when the agent IS connected but the Gateway/runtime hasn't acknowledged the latest placement change yet, so sending might land in the wrong space.

Concrete coupling: a Gateway-managed agent in `placement_state=pending` shows:
- `online_now=true` (connected fine)
- `expected_response=unlikely` (don't trust the routing yet)
- `unavailable_reason=placement_unconfirmed`
- `status_explanation`: "Connected, but space change to `<new_space>` is still pending Gateway ack. Wait or send to old space."

**Implementation order**: placement spec implementation lands first (it's the upstream truth), availability contract reads the placement state in its resolution algorithm. Both share the `781f5781` data-model gate, so both backend sub-tasks can be drafted in parallel and submitted as paired PRs.

## Open questions

- [ ] **Heartbeat cadence registry**: each agent declares its own cadence per the heartbeat primitive (memory note 2026-04-09). Does that live in `agents.heartbeat_cadence_seconds`, or in a separate table? Affects the Responsive axis tolerance window.
- [ ] **Confidence "medium" vs "low"**: do we surface the difference in the UI, or fold both into "Degraded"? Recommend folding for v1; surface differently in CLI `--full`.
- [ ] **Legacy `is_online` deprecation timeline**: one release? Two? Owners of consumers (frontend, sentinels' own agent listings) need a migration plan.
- [ ] **Send-time presence on the *receiver* side**: do we also include sender's presence so the recipient agent has context? (Probably out of scope here, lean toward "no" until LISTENER-001 receipts land.)
- [ ] **Activity-stream emission for transitions**: every connect/disconnect/quarantine emits one event. Volume risk for noisy fleets — discuss rate-limiting before shipping.

## Decision log

- **2026-04-24** — Outline posted as draft PR. Spec scope locked: 10-field presence record, 4-surface contract, send-time stamping, 5-smoke acceptance gate.
- **2026-04-25** — Iteration after @ChatGPT 2026-04-25 00:05 directive: elevated `expected_response` to first-class display field, separated from `messages_routable`; added `unavailable_reason` structured enum (9 codes incl. `placement_unconfirmed`); added `presence_age_seconds` for confidence decay; added explicit pre-send + post-send UX requirement sections; added agent-to-agent contract section; clarified that `active` is control-plane only, never sole presence indicator; linked placement (`36fd22ed`) as paired upstream truth.
- (subsequent decisions land here.)
