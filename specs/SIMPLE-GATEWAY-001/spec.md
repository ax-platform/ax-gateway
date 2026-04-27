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

**In** (default `/` route):
- Hero: "Bring your agents online" with single primary CTA "Connect agent"
- One agent table, single header row, columns: indicator · Agent · Last activity · Space · action
- Click any row → side drawer with status detail, space picker, pin toggle, send-test, start/stop, remove
- Top bar: brand mark, connection pill (`connected · paxai.app`), Space filter (defaults to "All spaces"), "Advanced view →" link
- Connect wizard modal with a registry-style runtime dropdown, readable details
  for the selected runtime, and autosetup/preflight when the runtime needs it
  — see GATEWAY-RUNTIME-AUTOSETUP-001

**Out** (still reachable at `/operator` — old detail UI):
- Approvals queue
- Doctor output
- Multi-agent activity feed
- Alerts panel
- Per-agent drill-in dashboard

## Row indicator and status vocabulary

Status is not a standalone row column in the simple view. It appears through
the row indicator, row action, drawer status section, and last-activity text.

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

Pass-through and mailbox runtimes are the exception to dot semantics:

- they use a mailbox indicator instead of a live dot;
- unread messages render as a small count bubble attached to the mailbox icon;
- pending task counts may render as a compact task badge;
- they must not show `Active` just because Gateway can hold work for them.

Pending approval rows show `Review` as the row action. Clicking it opens the
drawer so the operator can inspect the fingerprint before approving.

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
- Move flow: `POST /api/agents/{name}/move` or `ax gateway agents move <name> --space-id <space>` → `client.set_agent_placement(agent_id, space_id=…)` → gateway refetches placement/manage state → reconciles local registry to whatever backend actually applied. Backend = source.
- After a move, every Gateway-mediated send path, including "Send test message",
  must route through the agent row's current active space. Test delivery must
  not reuse a stale switchboard/default space.
- If a managed runtime is already connected when its active space changes, the
  Gateway must cancel/restart or otherwise rebind that runtime before the next
  delivery. A stale listener must never keep watching the old space while the
  row and service sender point at the new space.
- If the runtime is actively working during a requested move, the UI should
  warn the operator and offer either "wait until current work finishes" or
  "force move and cancel current work". CLI/API paths should expose the same
  semantics once the work-state lock is durable.
- Coercion: when backend silently keeps an agent in its existing space (because the target wasn't in `allowed_spaces`), gateway logs `managed_agent_move_coerced` activity for audit.

## Test messages and service senders

The drawer's "Send test message" action is a v1 shortcut for a more general
Gateway message composer.

Rules:

- The UI must make the sender visible. A test is authored by a Gateway-managed
  service sender, not by a mystery system action.
- The default sender should be the service account for the target space, such
  as `switchboard-<space>`, when that sender exists and has a valid credential.
- If the per-space service sender cannot be created or used, Gateway may fall
  back to a clearly marked self-authored diagnostic send rather than crashing.
  The response metadata must say that fallback happened.
- The drawer's custom message composer must not silently fall back to the
  target agent as sender. If the service sender is unavailable, disable or fail
  the send with a clear message.
- The message is sent to the target agent's current active space after
  placement reconciliation.
- If the target runtime is stopped, disconnected, starting, rebinding, or
  reconnecting, the drawer must not offer the send action until the current
  registry/activity state says it is routable again.
- The drawer should use a compact composer: visible sender, message text,
  immediate send, and later schedule/cron controls.

Open follow-up tasks:

- Add a service-account management surface for creating/reusing per-space
  Gateway notification senders.
- Add a full sender selector once service-account management exists. The v1
  composer may display the resolved default sender and rely on the backend
  response to report fallback.
- Add scheduled test/message support once the immediate send contract is stable.

## "Last activity" column

Source: most-recent of `last_work_completed_at`, `last_work_received_at`, `last_reply_at`, `last_received_at`. **Excludes** heartbeat / connection timestamps — those are implied by the connection pill and the row dot. If none of the work timestamps are set, show `—`.

For pass-through/mailbox rows, use the more specific contract from
**GATEWAY-PASS-THROUGH-MAILBOX-001**:

- unread messages: `New message` or `N new messages` from
  `last_work_received_at` / queued item time;
- no unread messages and no activity: `Inbox ready`;
- last check-in: `Checked`;
- replies: `Sent message`;
- pending approval: `Awaiting approval`.

Refreshing `/api/status` must not reset a queued message to `just now`.

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
# Positive case — guards against pin-bypass regressions
curl -sS -X POST http://127.0.0.1:8765/api/agents/demo-bot/move -d '{"space_id":"<allowed-space-id>"}' -H 'Content-Type: application/json'
# expect: 200, response.space_id reflects backend's applied space_id
ax gateway agents move demo-bot --space-id <allowed-space-id> --json
ax gateway agents test demo-bot --json
# expect: message.space_id == applied space_id

# Cleanup
ax gateway agents remove demo-bot
```

All five smokes must pass before the simple gateway is "demo ready."

## UI acceptance smoke

Before PR, verify the browser surface at `http://127.0.0.1:8765/`:

- connection pill reads `connected · paxai.app` in production mode;
- runtime picker lists all non-advanced templates from `/api/templates`;
- pass-through rows use a mailbox indicator, not a live dot;
- mailbox count bubbles do not shift the Agent column;
- pending approval rows open the drawer instead of approving inline;
- approval drawer shows origin/fingerprint details before the approve action;
- last activity for mailbox rows uses queued-message timestamps and does not
  reset to `just now` on refresh.

## Runtime picker

The connect wizard must scale like an open-source adapter registry, not a fixed
three-card marketing surface.

- Use a compact runtime dropdown/list as the primary selector.
- Show one readable detail panel for the selected runtime.
- Include all non-advanced Gateway templates returned by `/api/templates`, not
  only featured templates.
- Disabled/coming-soon templates may be listed for roadmap visibility, but must
  not submit.
- Large generated artwork is optional and should come from template metadata
  later; v1 should keep the layout simple enough for community-contributed
  templates.

## Out-of-scope cross-references

- Hermes auto-install lives in **GATEWAY-RUNTIME-AUTOSETUP-001**.
- Activity-bubble visibility (runtime→gateway→aX UI) lives in **GATEWAY-ACTIVITY-VISIBILITY-001**.
- Long-form operator views remain at `/operator` — that surface is **not** in scope here and stays as it is.
