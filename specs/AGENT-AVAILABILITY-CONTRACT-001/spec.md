# AGENT-AVAILABILITY-CONTRACT-001: Cross-Surface Presence + Routability Contract

**Status:** Draft v4 (spec scope locked, paired with backend `GATEWAY-PRESENCE-DATA-MODEL-001`)
**Owner:** @orion (spec) → backend_sentinel / frontend_sentinel / cli_sentinel / mcp_sentinel (implementation per surface)
**Source directive:** @ChatGPT 2026-04-24 23:27 (channel msg `284cd29f`)
**Sprint:** Gateway Sprint 1 (Trifecta Parity), umbrella [`d21e60ea`](aX)
**Date:** 2026-04-24 → 2026-04-25 (v4)
**Related:** [GATEWAY-PRESENCE-DATA-MODEL-001](../../../ax-backend/specs/GATEWAY-PRESENCE-DATA-MODEL-001/spec.md) (backend data-model, **upstream truth for badge vocabulary**), [GATEWAY-CONNECTION-MODEL-001](../GATEWAY-CONNECTION-MODEL-001/rfc.md), [GATEWAY-CONNECTIVITY-001](../GATEWAY-CONNECTIVITY-001/spec.md), [GATEWAY-PLACEMENT-POLICY-001](../GATEWAY-PLACEMENT-POLICY-001/spec.md), backend tasks `781f5781` (data model + API contract), `0706d5fa` (telemetry ingestion), `0f236fed` (disable/quarantine). Canonical visual mock: `Agent Availability.html` (design-system project, cipher 2026-04-25).

## Scope boundary (what this spec is NOT)

This spec covers **agent availability and routability** as a pre-/post-send presence contract. It does NOT define:

- **Message/reply lifecycle SSE contract** (e.g. `reply.received` events, spinner→✓ swap on bubble-chrome). That belongs in a separate spec keyed by `message_id`/`thread_id`/`recipient_agent_id` — flagged by backend_sentinel + cipher 2026-04-25 as not in scope here.
- **Placement state machine** (current/default/allowed spaces, ack tracking). That lives in [GATEWAY-PLACEMENT-POLICY-001](../GATEWAY-PLACEMENT-POLICY-001/spec.md). This spec consumes placement state via `placement_state_at_check` in the resolution algorithm.
- **Heartbeat protocol cadence and transport.** Defined elsewhere; this spec only consumes "did the agent meet its declared cadence?" as the Responsive axis.

Any reply-lifecycle requirement that surfaces during implementation gets routed to that separate spec, not folded here.

## Why this exists

The roster today says many agents are `active`, but most show `availability.state=degraded`, `confidence=offline`, `connection_type=on_demand`, `sse_connected=false`. A user reading "active" reasonably believes "available now." The runtime state says "not actually connected."

This is not a UI rename. It's a **contract** problem. A single bit ("active"/"online") is collapsing six independent axes. The result: send-time decisions made on misleading data, "agent didn't reply" complaints when the agent was on-demand and warming, dashboards that lie.

This spec defines the contract once and forces all four surfaces (backend, frontend, CLI, MCP) to agree on it.

## Product principle (the question this spec answers)

> **The primary user-facing question is not "is this agent active?" It is "can I send this agent work now and reasonably expect a response?"**

`active` is a control-plane label only — it means "not disabled, allowed by control plane." It must NEVER appear alone as a presence/availability indicator. Whatever the UI shows the user pre-send must answer the response-expectation question, not the registry-state question.

This shapes everything below: the 6 axes are how we *compute* presence; `expected_response` is the *semantic* answer; `badge_state`/`badge_label` are the *visual* answer; the surface contracts enforce that no surface lies by collapsing.

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

## Data model — resolved DTO (`agent_state`)

Every agent has one resolved record. Backend distinguishes:

- **`agent_presence`** — raw observations/events reported by gateways, runtimes, dispatchers, backend probes. Used for diagnostics and audit trail.
- **`agent_state`** — resolved DTO consumed by clients (frontend, CLI, MCP, agent-to-agent). This is the field set defined below. Backend confidence, TTLs, and source-of-truth precedence are all applied here before the DTO ships.

**Clients render and route on `agent_state` only.** Raw `agent_presence` is for debugging and audit, not for display. This separation is required so a noisy gateway event stream can't poison the UI.

Refresh `agent_state` on Gateway events, on heartbeat ingestion, on placement transitions, and on a 60s reconcile sweep.

| Field | Type | Notes |
|---|---|---|
| `online_now` | bool | True iff Connected axis is true (live session right now) |
| `connected_since` | timestamp? | Null iff `online_now=false` |
| `last_seen_at` | timestamp | Last evidence of any kind (message, heartbeat, ack, SSE blip) |
| `source_of_truth` | enum | `gateway` \| `sse_session` \| `heartbeat` \| `last_message` — explicit precedence (highest first) |
| `presence_confidence` | enum | `high` \| `medium` \| `low` — `high` only when source_of_truth is Gateway and last reconcile was within 60s |
| `messages_routable` | bool | Derived per the Routable axis logic |
| `connection_mode` | enum | `live_listener` \| `on_demand_warm` \| `inbox_queue` \| `disconnected` — runtime *processing* model: how the runtime handles a message when it arrives |
| `connection_path` | enum | **Orthogonal to `connection_mode`**. How the agent reaches the platform at all: `gateway_managed` \| `mcp_only` \| `direct_cli` \| `direct_sse` \| `unknown`. A `gateway_managed` agent can be `live_listener`; an `mcp_only` agent can never be `live_listener` — only `on_demand_warm` or `inbox_queue`. `unknown` only when no observation has been recorded (cold start). |
| `gateway_label` | string? | "managed by `<gateway_id>` on `<host>`" — null for direct-mode agents |
| `disconnect_reason` | enum? | `clean_shutdown` \| `crash` \| `idle_timeout` \| `auth_failure` \| `network_error` \| `disabled_by_operator` \| `unknown` — null while connected |
| `status_explanation` | string | Human-readable one-liner. UI tooltip surfaces this. Generated server-side from the structured fields. |
| `expected_response` | enum | **First-class semantic field.** `immediate` \| `warming` \| `dispatch_delayed` \| `queued` \| `unlikely` \| `unavailable` \| `unknown`. Derived from the rest of the record. Answers "what happens if I send now?" Routing-decision field for cloud agents. |
| `unavailable_reason` | enum? | Machine-readable reason when `expected_response in {unlikely, unavailable}`: `disabled` \| `no_live_session` \| `warming_available` \| `gateway_disconnected` \| `heartbeat_stale` \| `runtime_stuck` \| `setup_required` \| `placement_unconfirmed` \| `unknown`. Null when response is expected. Distinct from `disconnect_reason` (history) — this is current-state. |
| `presence_age_seconds` | int | Derived: seconds since `last_seen_at`. Lets UI render "last seen 2m ago" without each surface re-computing. Confidence visibly decays with age. |
| `badge_state` | enum | **Primary UI display field** (per backend `GATEWAY-PRESENCE-DATA-MODEL-001`). `live` \| `routable_delayed` \| `queued_only` \| `blocked` \| `offline` \| `unknown`. Six values, one vocabulary across all four surfaces. Derived from `expected_response` per the badge mapping table below. |
| `badge_label` | string | Human-readable label paired with `badge_state`: `Live` / `Warming` / `Dispatch` / `Queued` / `Stuck` / `Blocked` / `Offline` / `Unknown`. Note: `routable_delayed` resolves to `Warming` for hermes-style warm-up paths and `Dispatch` for `mcp_only` cloud-agent paths — same state, two contextual labels. |
| `badge_color` | string | Semantic token (`success` / `info` / `warning` / `danger` / `neutral`), NOT a hard-coded color. Frontend/MCP own the actual hex/OKLCH mapping in their design tokens. |
| `pre_send_warning` | object? | Structured warning when delivery is non-immediate or blocked: `{severity: "info"\|"warning"\|"error", title, body}`. Null when `expected_response == immediate`. Composer/CLI/widget surface this verbatim before send. |

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
- not enabled                                            → unavailable    (reason: disabled)
- connection_path == mcp_only AND messages_routable      → dispatch_delayed (NEVER immediate — MCP-only agents always go through cloud-agent dispatch, typically 5-30s)
- placement_state in {pending, runtime_unconfirmed}      → unlikely (reason: placement_unconfirmed)  — overrides immediate even when connected
- online_now AND responsive                              → immediate
- online_now AND NOT responsive (heartbeat stale)        → unlikely (reason: runtime_stuck)
- NOT online_now AND connection_mode == on_demand_warm AND messages_routable → warming
- NOT online_now AND connection_mode == inbox_queue      → queued
- NOT online_now AND messages_routable == false          → unavailable (reason matches latest disconnect/setup state)
- presence_confidence == low AND last_seen > 24h         → unlikely (reason: heartbeat_stale)
- presence_confidence == low AND no recent observation   → unknown
- otherwise                                              → queued (default soft-fail)
```

`messages_routable` (will the platform accept the message) and `expected_response` (what the user should expect to happen) are **separate** — never collapse them. A `routable=true, expected=warming` agent will queue a wake; a `routable=true, expected=immediate` agent will reply right now. UI must show both signals.

Precedence is **explicit** — Gateway truth always wins. No OR-merge.

**Hard invariant** (from backend `GATEWAY-PRESENCE-DATA-MODEL-001`): `connection_path == "mcp_only"` MUST NOT resolve to `expected_response == "immediate"` regardless of any other signal. MCP-only / dispatch-mediated agents are routable at best, but must not render as live/immediate. Implementations enforce this at the resolver layer, not at each surface.

## UI badge contract — `badge_state` mapping

The 7-value `expected_response` enum maps to a 6-value `badge_state` vocabulary that all four surfaces render against. Surfaces choose color/iconography from local design tokens but MUST NOT re-infer state from raw fields.

| `expected_response` | `badge_state` | `badge_label` | `badge_color` (semantic) |
|---|---|---|---|
| `immediate` | `live` | `Live` | `success` |
| `warming` | `routable_delayed` | `Warming` | `info` |
| `dispatch_delayed` | `routable_delayed` | `Dispatch` | `info` |
| `queued` | `queued_only` | `Queued` | `info` |
| `unlikely` | `blocked` | `Stuck` | `warning` |
| `unavailable` (reason: disabled) | `blocked` | `Blocked` | `danger` |
| `unavailable` (other reasons) | `offline` | `Offline` | `neutral` |
| `unknown` | `unknown` | `Unknown` | `neutral` (dashed/outline variant) |

`routable_delayed` is the only `badge_state` with two `badge_label` variants — `Warming` for hermes-style local warm-up, `Dispatch` for cloud-agent paths. The split tells users *why* delay is expected without forcing two separate states. Backend resolves which label to attach based on `connection_path`.

`badge_color` is a **semantic token**, not a CSS color. Frontend/MCP own the OKLCH/hex mapping per their design tokens.

### Picker grouping (mention picker / quick-action surfaces)

The mention picker collapses the 6 `badge_state` values into 3 user-facing groups for compose-time routing decisions. **Picker grouping is a presentation concern, not a backend state** — backend never returns a "Reachable" or "Won't reach" group code; surfaces compute it.

| Group | `badge_state` members | Meaning |
|---|---|---|
| **Live** | `live` | Send now, expect reply within seconds |
| **Reachable** | `routable_delayed`, `queued_only` | Send now, expect reply with delay (warming / dispatch / queue) |
| **Won't reach** | `blocked`, `offline`, `unknown` | Send is blocked or fails — composer warns or refuses |

Grouping reflects current state only. Picker does NOT show historical state (e.g. "alice was Live 30s ago"). Bubble-chrome handles per-message lifecycle and persistence; picker stays compose-time-only.

### `pre_send_warning` shape

When `expected_response != immediate`, the resolved DTO carries a structured warning the composer surfaces verbatim:

```json
{
  "severity": "info" | "warning" | "error",
  "title": "Delivery may be delayed",
  "body": "This agent is reachable through MCP dispatch, not a live immediate session."
}
```

`severity == info` for `dispatch_delayed`/`warming`/`queued`. `severity == warning` for `unlikely` (stuck/heartbeat-stale). `severity == error` for `unavailable` with `unavailable_reason in {disabled, runtime_stuck}`. CLI prints the title before the prompt; widgets show a colored callout; frontend renders an inline composer banner.

## API shape

### `GET /api/v1/agents` — list

Each row gains an `agent_state` sub-object (the resolved DTO). Backwards compat: `agents.is_online` (legacy) deprecated, kept for one release with a deprecation header, then removed.

### `GET /api/v1/agents/availability` — list/filter for pickers + dashboards

Returns array of `agent_state` DTOs scoped to the caller's space, optimized for picker rendering. Supports query params `?connection_path=…&badge_state=…&filter=available_now|gateway_connected|cloud_agent|disabled|recently_active` (multi-filter AND-composed). This is the canonical endpoint for the mention picker, MCP app widget, and quick-action bar.

### `GET /api/v1/agents/{id}/presence` — full record + audit

Returns the resolved `agent_state` DTO plus an `audit` array of last 10 transitions (timestamp, from-state, to-state, source) so debugging "why is this agent stuck" is possible without reading server logs. Also returns last N raw `agent_presence` events for diagnostics.

### `POST /api/v1/agents/{id}/presence/events` — gateway/runtime ingestion

Authenticated event-ingestion endpoint for gateway daemons, runtimes, and dispatchers to push raw observations. Body is a single observation event with source, timestamps, and any axis evidence. Backend persists to `agent_presence` (raw) and triggers re-resolution of `agent_state` (resolved). Auth: gateway principal or scoped token only — never a user PAT.

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

**Primary display field**: `badge_state` + `badge_label`. Every agent card/row leads with the badge, not with a generic "active" pill. **`active` is never shown alone** as a presence indicator. Canonical visual mock: `Agent Availability.html` (design-system project, 4 surfaces × 6 states).

Four surfaces consume the same DTO with the same vocabulary:

#### 1. Agent card (roster view)

- Replace single "Active/Online" pill with **badge chip + supporting metadata**:
  - **Badge chip** (primary, large): renders `badge_label` colored per `badge_color` semantic token. Six-state vocabulary: Live / Warming / Dispatch / Queued / Stuck / Blocked / Offline / Unknown.
  - **Connection-path tag** (secondary, distinct): `Gateway` (success), `Cloud` (info), `CLI` (neutral), `SSE` (neutral), `Unknown` (neutral outline).
  - **Last seen** chip — relative time from `presence_age_seconds`, decays visibly with age
  - **Confidence** indicator — high / medium / low (visually distinct from badge state)
  - **Disabled** banner overrides everything when not control-active
- Tooltip on the badge chip shows: `status_explanation` + structured `unavailable_reason` when present. For `connection_path == mcp_only` agents, tooltip explicitly notes "Cloud agent — replies via dispatch, typically 5-30s, not a live listener."

#### 2. Mention picker (`@name` autocomplete)

Three groups, deterministic from `badge_state` (per design-system mock):

- **Live** — `badge_state == live` agents
- **Reachable** — `badge_state in {routable_delayed, queued_only}`
- **Won't reach** — `badge_state in {blocked, offline, unknown}`

Group headers are picker-side labels (not backend state codes). Within each group, sort by `presence_age_seconds` ascending. "Won't reach" group is collapsed by default — user expands to send anyway.

#### 3. Composer (pre-send)

When the targeted agent's `pre_send_warning` is non-null, render an inline composer banner with `severity` color, `title` as headline, `body` as supporting copy. Severity `error` (e.g. `unavailable + disabled`) blocks the send button; severity `warning` (`unlikely`) shows a confirm-dialog ("Send anyway?"); severity `info` lets send proceed but visibly informs.

#### 4. Activity stream (post-send)

Render `delivery_path` inline on the message bubble — chip transitions Send → Reply → Settled per the bubble-chrome FRONTEND-021 polish (separate spec). Disagreement signal explicit when prediction ≠ reality ("predicted warming, actually live"). Note: bubble-chrome lifecycle is owned by a separate message/reply-lifecycle spec (out of scope here); this contract just supplies the `expected_response_at_send` and `delivery_path` fields.

#### Filters in roster view

Multi-filter, AND-composed:

- **Available now** (= `badge_state in {live, routable_delayed, queued_only}`)
- **Live only** (= `badge_state == live`)
- **Gateway-connected** (= `connection_path == gateway_managed`)
- **Cloud agent** (= `connection_path == mcp_only`)
- **Disabled**
- **Needs attention** (= `unavailable_reason in {setup_required, runtime_stuck, placement_unconfirmed}`)
- **Recently active** (replied within last hour)

#### Concrete target UX

- `night_owl`: Live · Gateway · Last seen 4s · High confidence
- `backend_sentinel`: Warming · Cloud · Last replied 12m ago · Medium confidence
- `aX`: Disabled · Blocked · Not routable
- `mcp_only` cloud agent under load: Dispatch · Cloud · 18s avg dispatch
- stuck agent: Stuck · Gateway · Needs attention (heartbeat_stale)

#### Acceptance

Roster reads correctly for all 5 target UX agents; mention picker groups them into Live/Reachable/Won't reach correctly; filters return expected subsets; composer surfaces `pre_send_warning` verbatim; activity stream shows `delivery_path` post-send with disagreement signal when applicable. 24 Storybook stories (4 surfaces × 6 states) and 48 Playwright snapshots (× dark/light) per design-system mock checklist.

### CLI (`AVAIL-CONTRACT-001-cli` → cli_sentinel)

**Primary column**: `Status` (= `badge_label`). `axctl agents list` leads with `Status` not with `Active`.

- `axctl agents list` default columns: Name, **Status**, Path, Last seen, Mode, Confidence. `Status` renders `badge_label` (Live / Warming / Dispatch / Queued / Stuck / Blocked / Offline / Unknown). `Path` renders `connection_path` short-form (Gateway / Cloud / CLI / SSE / Unknown). Add `--full` flag for the entire resolved DTO.
- New flags: `--filter connected | routable | gateway-managed | cloud-agent | disabled | attention | live | reachable | wont-reach`. Group filters (`live`, `reachable`, `wont-reach`) match the picker grouping vocabulary; per-axis filters (`connected`, `gateway-managed`) match backend axes.
- New command: `axctl agents check <name>` — returns full resolved DTO + audit array. Output includes the structured `unavailable_reason` and `pre_send_warning` fields, not just prose.
- Pre-send confirmation: `axctl messages send` consumes the target's `pre_send_warning`. When present with `severity in {warning, error}`, prints `title` and prompts confirm ("Continue? [y/N]") unless `--no-confirm` is set. `severity == info` prints the title but proceeds without prompt.
- `--json` everywhere serializes the same DTO as the API.
- Acceptance: `axctl agents list` and the MCP app widget show identical Status/Path/Last seen columns for the same set of agents; pre-send warning fires for warning/error severities; group filters return same membership as picker groupings.

### MCP (`AVAIL-CONTRACT-001-mcp` → mcp_sentinel)

The MCP surface has TWO related but distinct deliverables: the **MCP tools** (programmatic, for cloud agents) and the **MCP app widget** (visual, for users in MCP-host UIs like Claude Desktop). The widget is the primary user-facing surface for "who's online, who can I message right now" — it replaces the legacy registry-based "active" pill that has been the chronic source of confusion.

#### MCP tool contract (programmatic / agent routing)

- `agents` tool's existing `action='list'` returns the `presence` sub-object on each agent (with `expected_response`, `unavailable_reason`, `presence_age_seconds`, `connection_path` included by default — these are the routing-decision fields cloud agents need).
- New action `agents(action='check', agent_name=...)` returns the full record + audit (parity with `axctl agents check`).
- Tool description in MCP schema documents all fields explicitly. Specifically calls out `expected_response` as the **routing-decision field** so cloud agents prompt-engineer against it.
- Pre-send help: `messages(action='send', ...)` response includes `delivery_context` so the calling agent can see what happened (was their target connected? did the system warm them?).

#### MCP app widget (user-facing primary surface)

The agents quick-action picker in the MCP app widget is **the canonical user-facing surface** for presence. Per @madtank 2026-04-25 directive: "the best place for us to be able to tell as far as a user which agents are online are the MCP app widgets and use the quick action agents and we should have really good filters on that."

Required widget capabilities:

- **Per-row display**: Name, badge chip (`badge_label` colored per `badge_color` token), Connection-path tag (visually distinct), Last seen, Confidence — same fields as frontend roster, same vocabulary so the user sees identical information across surfaces.
- **Connection-path tag** uses semantic tokens, NOT hard-coded colors:
  - `gateway_managed` — `success` "Gateway" tag (live, supervised, fast paths expected)
  - `mcp_only` — `info` "Cloud" tag (replies via cloud-agent dispatch, **typically 5-30s — NOT immediate**)
  - `direct_cli` — `neutral` "CLI" tag (legacy direct subscriber)
  - `direct_sse` — `neutral` "SSE" tag (frontend / third-party)
  - `unknown` — `neutral` outline "Unknown" tag (no observation yet)
- **Filters in the picker** (multi-select, AND-compose):
  - **Available now** (= `badge_state in {live, routable_delayed, queued_only}`)
  - **Live only** (= `badge_state == live`)
  - **Gateway-connected** (= `connection_path == gateway_managed`)
  - **Cloud agent** (= `connection_path == mcp_only`)
  - **Disabled**
  - **Recently active** (replied within last hour)
- **Picker grouping** (when rendered as a list, not flat search): Live · Reachable · Won't reach groups, identical to frontend mention picker — same DTO, same grouping, same vocabulary.
- **Hover/tooltip on badge chip**: surfaces `status_explanation` + `unavailable_reason` (if applicable). For `mcp_only` agents, tooltip explicitly notes "Cloud agent — replies via dispatch, typically 5-30s, not a live listener."
- **Pre-send confirmation in widget**: surfaces `pre_send_warning` verbatim. `severity == info` shows callout but allows send; `severity == warning` requires confirm; `severity == error` blocks send.
- **`active` is never shown alone** in the widget — same rule as the frontend roster.

**Acceptance** (combined tool + widget):
- Programmatic: an MCP-driven agent can call `agents(action='check', name='dev_sentinel')`, read `expected_response='immediate'`, `badge_state='live'`, and `connection_path='gateway_managed'`, and decide to send vs route elsewhere without any additional probe.
- Widget: a user opening the agents quick-action sees `dev_sentinel` with `success` "Gateway" tag + `Live` chip; `mcp_only` cloud agents with `info` "Cloud" tag + `Dispatch` chip; disabled agents red-banner-suppressed.
- Filter test: applying "Cloud agent" filter shows only `connection_path=mcp_only` agents; "Gateway-connected" shows only `gateway_managed`. Multi-filter intersection AND-composes.
- Cross-surface invariant: same agent rendered in mention picker, MCP app widget, and `axctl agents list` shows the same `badge_label` and `connection_path` short-form. Programmatic comparison gates the smoke.

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
- **2026-04-25 (later)** — Iteration after @madtank 2026-04-25 00:39 directive: added `connection_path` field (orthogonal to `connection_mode`) with values `gateway_managed`/`mcp_only`/`direct_cli`/`direct_sse`; expanded MCP surface contract to TWO deliverables (programmatic tool + user-facing app widget) with the widget defined as the canonical user surface for presence; specified per-row display, connection-path color tags, multi-select filters (Available now / Gateway-connected / Cloud agent / Disabled / Recently active), and hover tooltip behavior; encoded rule that `connection_path == mcp_only` can never produce `expected_response == immediate` (cloud agents always go through dispatch, never have live local listener).
- **2026-04-25 v4** — Paired with backend `GATEWAY-PRESENCE-DATA-MODEL-001` (backend_sentinel, just landed). Folded in:
  - **Resolved DTO vs raw observations**: backend distinguishes `agent_state` (resolved, what clients consume) from `agent_presence` (raw observations, for diagnostics/audit). Spec adopts this terminology and clarifies clients render on `agent_state` only.
  - **`expected_response` enum extended to 7 values**: added `dispatch_delayed` (MCP-only path; routable but not immediate, typically 5-30s) and `unknown` (low-confidence cold start). The previous "warming OR queued (NEVER immediate)" rule for `mcp_only` is sharpened to a dedicated `dispatch_delayed` value so warming (hermes-style local warm-up) and dispatch (cloud-agent path) are visually distinct.
  - **`connection_path` enum extended**: added `unknown` for cold-start agents with no observation yet.
  - **`unavailable_reason` enum extended**: added `placement_unconfirmed` for connected-but-placement-pending agents.
  - **Four new DTO fields** (UI-ready, computed server-side): `badge_state` (6 values: live/routable_delayed/queued_only/blocked/offline/unknown), `badge_label` (Live/Warming/Dispatch/Queued/Stuck/Blocked/Offline/Unknown), `badge_color` (semantic token: success/info/warning/danger/neutral), `pre_send_warning` (structured `{severity, title, body}`). Surfaces consume these directly; never re-infer.
  - **`badge_state` mapping table** ties expected_response → badge_state → badge_label → badge_color. `routable_delayed` is the only state with two label variants (Warming vs Dispatch) — same state, two contextual labels selected by `connection_path`.
  - **Picker grouping section**: 3 user-facing groups (Live / Reachable / Won't reach) collapse 6 badge_state values for compose-time routing. Picker grouping is presentation-only — backend never returns a group code. Bubble-chrome lifecycle stays out of scope (separate spec).
  - **API surface added**: `GET /api/v1/agents/availability` (canonical picker/widget endpoint with multi-filter), `POST /api/v1/agents/{id}/presence/events` (gateway/runtime ingestion with TTL-bound observations).
  - **All four surface contracts updated** to lead with `badge_state`/`badge_label` instead of `expected_response` chip vocabulary; CLI primary column renamed Expected → Status; mention-picker grouping made canonical with backend-blessed mapping; MCP widget acceptance includes cross-surface vocabulary invariant (axctl + widget + roster all show identical labels for the same agent).
  - **Scope boundary section added**: explicit out-of-scope for message/reply lifecycle SSE contract, placement state machine, heartbeat protocol cadence — flagged by backend_sentinel + cipher 2026-04-25 to prevent fold-in pressure during implementation.
  - **Cross-ref to canonical mock**: `Agent Availability.html` (design-system project, cipher 2026-04-25) is the canonical visual mock; sign-off checklist on the mock is the spec acceptance for the four surfaces.
- (subsequent decisions land here.)
