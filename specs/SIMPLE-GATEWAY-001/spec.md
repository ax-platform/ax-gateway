# SIMPLE-GATEWAY-001: Simple Gateway — Default Onboarding View

**Status:** v1 draft
**Owner:** @pulse (ax-cli QA), reviewer @orion
**Date:** 2026-04-25
**Source directives:**
- @madtank 2026-04-25, investor demo deadline 2026-04-27
- "Just have it show all of the agents that you have connected from this gateway"
- "Use this and have an advanced setup. I actually like this view it's simple it's clean."
- "Stop them, that should make it so that they don't send or receive messages."

## Why this exists

The Gateway has many internal surfaces — approvals, doctor, drill-ins, runtime config. For most users (and especially investor demos), the right entry point is opinionated, calm, and onboarding-shaped: "you are connected, here are your agents, click to add another, click a row to inspect or act."

Naming: this view is **not** "the demo." It is the gateway's default. The detailed operator surface lives behind an "Advanced view" link.

## Route migration (current state — already shipped)

`/` and `/demo` both serve the simple gateway page (rendered from `ax_cli/static/demo.html`). `/operator` serves the previous detail-heavy UI. No further redirect or migration step is required. We should still decide whether to drop `/demo` as an alias post-Monday so the canonical URL is just `/`. Removing `/demo` is a one-line change and breaks no consumers other than the topbar link this spec already replaces with `/operator`.

## Scope

**In** (default `/` route — to be renamed from `/demo`):
- Hero: "Bring your agents online" with single primary CTA "Connect agent"
- One agent table, single header row, columns: dot · Agent · Type · Status · Last activity · Space
- Click any row → side drawer with status detail, space picker, pin toggle, send-test, start/stop, remove
- Top bar: brand mark, connection pill (`connected · paxai.app`), Space filter (defaults to "All spaces"), "Advanced view →" link
- Connect wizard modal (Ollama featured, others available, autosetup triggered when the runtime needs it — see GATEWAY-RUNTIME-AUTOSETUP-001)

**Out** (still reachable at `/operator` — old detail UI):
- Approvals queue
- Doctor output
- Multi-agent activity feed
- Alerts panel
- Per-agent drill-in dashboard

## Status vocabulary (single column on the row)

Map raw `presence` × `confidence` × `desired_state` → one human label:

| Operator intent / observed state         | Label           | Tone     |
|------------------------------------------|-----------------|----------|
| `desired_state=stopped`                  | Stopped         | muted    |
| `presence=ERROR` or `confidence_reason=setup_blocked` | Setup error | error |
| `confidence=BLOCKED, reason=binding_drift` | Needs approval| warning  |
| `confidence=BLOCKED` (other)             | Blocked         | warning  |
| `presence=STALE`                         | Stale           | warning  |
| `presence=ACTIVE/LIVE` or `connected=true` | Active        | ok       |
| `confidence=MEDIUM, reason=launch_available` | Ready       | ok       |
| `confidence=HIGH`                        | Ready           | ok       |
| `presence=IDLE`                          | Idle            | muted    |

Operator intent overrides observed presence: pressing Stop reads "Stopped" immediately, even if a stale heartbeat is still draining.

## Lifecycle (the kill switch)

- "Stop" sets `desired_state=stopped` on the gateway registry entry.
- While stopped:
  - `POST /api/agents/{name}/test` returns 400 with "is stopped. Start it before sending a test."
  - `POST /api/agents/{name}/send` returns 400.
  - Reconcile loop tears down any live listener at next tick.
- "Start" sets `desired_state=running`. Reconcile picks up the runtime on next pass.

## Space binding & pinning

- Backend `agent_space_access` table is the canonical record (per AGENT-SPACE-ACCESS rules in `ax-backend/CLAUDE.md` §Architectural rules #6).
- The gateway exposes a per-agent `pinned: bool` in **local registry only** — refuses move client-side when pinned. No backend schema change.
- Move flow: `POST /api/agents/{name}/move` → `client.update_agent(name, space_id=…)` → gateway refetches `GET /api/v1/agents/manage/{id}` → reconciles local registry to whatever backend actually applied. Backend = source.
- Coercion: when backend silently keeps an agent in its existing space (because the target wasn't in `allowed_spaces`), gateway logs `managed_agent_move_coerced` activity for audit.

## "Last activity" column

Source: most-recent of `last_work_completed_at`, `last_work_received_at`, `last_reply_at`, `last_received_at`. **Excludes** heartbeat / connection timestamps — those are implied by the connection pill and the row dot. If none of the work timestamps are set, show `—`.

## System-agent visibility

- `switchboard-*` agents are gateway internals (per-space inboxes), hidden by default.
- "Show system agents (N)" toggle in the section header reveals them with a `[system]` chip.
- localStorage-persisted choice.

## Acceptance smokes (CLI-driven, no UI required)

```bash
# Connect, status, list
ax gateway start
ax gateway status                   # connected, agents=N, daemon running
ax gateway agents list

# Add via wizard equivalent
ax gateway agents add demo-bot --template ollama
curl -sS http://127.0.0.1:8765/api/status | jq '.agents | map({name, desired_state, space_id, pinned})'

# Kill switch
ax gateway agents stop demo-bot
curl -sS -X POST http://127.0.0.1:8765/api/agents/demo-bot/test -d '{}' -H 'Content-Type: application/json'
# expect: {"error":"@demo-bot is stopped. Start it before sending a test."}, http 400

ax gateway agents start demo-bot
curl -sS -X POST http://127.0.0.1:8765/api/agents/demo-bot/test -d '{}' -H 'Content-Type: application/json'
# expect: 201

# Pin / move
curl -sS -X POST http://127.0.0.1:8765/api/agents/demo-bot/pin -d '{"pinned":true}' -H 'Content-Type: application/json'
curl -sS -X POST http://127.0.0.1:8765/api/agents/demo-bot/move -d '{"space_id":"<other>"}' -H 'Content-Type: application/json'
# expect: 400, "is pinned to its current space."

curl -sS -X POST http://127.0.0.1:8765/api/agents/demo-bot/pin -d '{"pinned":false}' -H 'Content-Type: application/json'

# Cleanup
ax gateway agents remove demo-bot
```

All five smokes must pass before the simple gateway is "demo ready."

## Out-of-scope cross-references

- Hermes auto-install lives in **GATEWAY-RUNTIME-AUTOSETUP-001**.
- Activity-bubble visibility (runtime→gateway→aX UI) lives in **GATEWAY-ACTIVITY-VISIBILITY-001**.
- Long-form operator views remain at `/operator` — that surface is **not** in scope here and stays as it is.
