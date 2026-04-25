# CLI-SURFACE-INVENTORY-001: axctl Verb Inventory + Gaps vs MCP

**Status:** v1 — gaps-vs-MCP diff filled in after `MCP-SURFACE-INVENTORY-001` landed (PR #212, mcp_sentinel)
**Owner:** @orion (absorbed from cli_sentinel due to silence on `653cae21`)
**Source task:** [`653cae21`](aX) — CLI surface inventory — every axctl verb, args, output, gaps vs MCP
**Sprint:** Gateway Sprint 1 (Trifecta Parity), umbrella [`d21e60ea`](aX)
**Date:** 2026-04-25
**Companion:** [MCP surface inventory `6699321c`](aX) → mcp_sentinel, landed at `ax-mcp-server/specs/MCP-SURFACE-INVENTORY-001.md`

## Method

For each verb-row: read `axctl <command> [<sub>] --help`, the relevant `ax_cli/commands/<group>.py`, and the underlying REST endpoint hit. Record:

- **Command path** — full `axctl ... <verb>` form
- **Required args** — positional + required options
- **Optional flags** — common flags only; full set in the source
- **Output shape** — default columns + `--json` keys
- **Auth scope** — user PAT / agent-bound PAT / either
- **REST endpoint** — what it actually hits
- **MCP equivalent** — does the MCP server expose the same verb? same params? same output? — **filled by cross-referencing mcp_sentinel's `6699321c` artifact when it lands**

Acceptance criteria for the merged inventory: every CLI row has a paired MCP row (or "MCP gap" note); every MCP row has a paired CLI row (or "CLI gap" note). The gap doc is the diff.

## Skeleton — populate Saturday AM

### Top-level shortcuts

| Command | Required | Optional flags | Output | Auth | REST | MCP equivalent |
|---|---|---|---|---|---|---|
| `axctl bootstrap-agent` | NAME | `--runtime`, `--workdir`, `--scope` | text + `--json` | user PAT | `/api/v1/agents` (POST) + `/api/v1/keys` | TBD |
| `axctl handoff` | TASK | `--to`, `--watch`, `--timeout` | text | user/agent | `/api/v1/tasks` + SSE | TBD |
| `axctl login` | — | `--token`, `--url`, `--env` | confirmation | bootstraps user PAT | `/auth/...` | n/a (CLI-only) |
| `axctl send` | CONTENT | `--skip-ax`, `--space-id`, `--wait`, `--file` | reply or none | agent | `/api/v1/messages` (POST) | `messages(action='send')` |

### `axctl auth`

| Subcommand | Args | Output | Auth | REST | MCP equivalent |
|---|---|---|---|---|---|
| `auth doctor` | — | report | any | various GET | TBD |
| `auth whoami` | — | identity | any | `/api/v1/auth/whoami` | `whoami(action='show')` |
| `auth init` | — | interactive setup | n/a | n/a | n/a (CLI-only) |
| `auth exchange` | — | JWT | PAT | `POST /auth/exchange` | n/a |
| `auth token` | (sub) | various | any | `POST /auth/...` | n/a |

### `axctl keys`

| Subcommand | Args | Output | Auth | REST |
|---|---|---|---|---|
| `keys create` | NAME | key info | user | `POST /api/v1/keys` |
| `keys list` | — | table | user | `GET /api/v1/keys` |
| `keys revoke` | KEY_ID | confirmation | user | `DELETE /api/v1/keys/{id}` |
| `keys rotate` | KEY_ID | new key | user | `POST /api/v1/keys/{id}/rotate` |

### `axctl credentials`

| Subcommand | Args | Output | Auth | REST |
|---|---|---|---|---|
| `credentials issue-agent-pat` | AGENT_ID | PAT | user | `POST /api/v1/credentials/issue/agent-pat` |
| `credentials issue-enrollment` | — | enrollment token | user | `POST /api/v1/credentials/issue/enrollment` |
| `credentials revoke` | CRED_ID | confirmation | user | `DELETE /api/v1/credentials/{id}` |
| `credentials audit` | — | log | user | `GET /api/v1/credentials/audit` |
| `credentials list` | — | table | user | `GET /api/v1/credentials` |

### `axctl agents`

| Subcommand | Args | Output | Auth | REST | MCP equivalent |
|---|---|---|---|---|---|
| `agents list` | — | table or JSON | any | `GET /api/v1/agents` | `agents(action='list')` |
| `agents ping` | NAME | round-trip | any | `POST /api/v1/messages` (probe) | n/a |
| `agents discover` | (filters) | matches | any | `GET /api/v1/agents?...` | TBD |
| `agents create` | NAME | created agent | user | `POST /api/v1/agents` | `agents(action='create')` (HITL) |
| `agents get` | NAME | full record | any | `GET /api/v1/agents/{id}` | `agents(action='get')` |
| `agents update` | NAME | updated record | user | `PATCH /api/v1/agents/{id}` | TBD |
| `agents delete` | NAME | confirmation | user | `DELETE /api/v1/agents/{id}` | TBD |
| `agents status` | — | bulk presence | any | `GET /api/v1/agents/presence` | partial — bulk shape matches MCP `agents(action='list')` availability map |
| `agents check` | NAME_OR_ID | resolved DTO | any | `GET /api/v1/agents/{id}/state` (fallback `/presence`) | **CLI-only today** — MCP needs `agents(action='check')` to close the gap (gap row 7 in MCP inventory) |
| `agents tools` | AGENT_ID | enabled tools | any | `GET /organizations/{space}/roster` | TBD |
| `agents avatar` | NAME, FILE | confirmation | user | `PATCH /api/v1/agents/{id}` | TBD |
| `agents tools` | NAME | tool list | any | `GET /api/v1/agents/{id}/tools` | TBD |
| `agents avatar` | NAME | avatar URL | any | `GET /api/v1/agents/{id}/avatar` | TBD |

### `axctl apps`

| Subcommand | Args | Output | REST |
|---|---|---|---|
| `apps list` | — | table | `GET /api/v1/apps` |
| `apps signal` | KIND | signal payload | `POST /api/v1/apps/signal` |

### `axctl messages`

| Subcommand | Args | Output | Auth | REST | MCP equivalent |
|---|---|---|---|---|---|
| `messages send` | CONTENT | reply or skip | any | `POST /api/v1/messages` | `messages(action='send')` |
| `messages list` | — | table | any | `GET /api/v1/messages` | `messages(action='list')` |
| `messages read` | (id?) | confirmation | any | `POST /api/v1/messages/read` | TBD |
| `messages get` | ID | full record | any | `GET /api/v1/messages/{id}` | `messages(action='get')` |
| `messages edit` | ID | updated | author | `PATCH /api/v1/messages/{id}` | TBD |
| `messages delete` | ID | confirmation | author | `DELETE /api/v1/messages/{id}` | TBD |
| `messages search` | QUERY | matches | any | `GET /api/v1/messages?q=...` | TBD |

### `axctl alerts`

| Subcommand | Args | Output | REST |
|---|---|---|---|
| `alerts send` | KIND | alert payload | `POST /api/v1/alerts` |
| `alerts reminder` | (subs) | reminder ops | `POST /api/v1/alerts/reminder/*` |
| `alerts ack` | ID | ack | `POST /api/v1/alerts/{id}/ack` |
| `alerts resolve` | ID | resolved | `POST /api/v1/alerts/{id}/resolve` |
| `alerts snooze` | ID | snoozed | `POST /api/v1/alerts/{id}/snooze` |
| `alerts state` | — | current alerts | `GET /api/v1/alerts` |

### `axctl reminders` (TASK-LOOP-001 v1 + v1.1, PRs #98/#99)

**Local-only loop runtime** — store at `~/.ax/reminders.json`, schema v2 (priority + mode + drafts).

| Subcommand | Args | Output | REST | MCP equivalent |
|---|---|---|---|---|
| `reminders add` | TASK_ID | reminder | local + `POST /api/v1/messages` (on fire if mode=auto) | **CLI-only** — MCP `tasks` lacks `mode=auto\|draft\|manual` and offline queue (gap row 2 in MCP inventory) |
| `reminders list` | — | table sorted by `(priority, next_fire)` | local | n/a |
| `reminders run` | `--once / --watch` | fire due policies | local + send | n/a — CLI loop runtime |
| `reminders status` | `--skip-probe` | online/offline + queue + drafts | local + cheap `/health` probe | **CLI-only** (gap row 5 in MCP inventory) |
| `reminders pause / resume / cancel` | ID | confirmation | local | n/a |
| `reminders update` | ID | updated | local | n/a |
| `reminders drafts list / show / edit / send / cancel` | (id) | HITL queue ops | local + `POST /api/v1/messages` (on send) | **CLI-only** (gap row 4 in MCP inventory) |
| `reminders disable` | ID | (legacy alias) | local | n/a |

### `axctl heartbeat` (HEARTBEAT-001, PR #100)

**Local-first connectedness primitive** — store at `~/.ax/heartbeats.json`, ring-buffer history.

| Subcommand | Args | Output | REST | MCP equivalent |
|---|---|---|---|---|
| `heartbeat send` | `--status / --note / --cadence / --skip-push` | record | `POST /api/v1/agents/heartbeat` (offline-safe) | **CLI-only today** — MCP needs `agents(action='heartbeat')` for cloud-agent self-pulse parity |
| `heartbeat list` | `--limit / --unpushed` | history | local | n/a |
| `heartbeat status` | `--skip-probe` | online + cadence + queued | local + `/health` probe | n/a |
| `heartbeat push` | — | drain queued | `POST /api/v1/agents/heartbeat` | n/a |
| `heartbeat watch` | `--interval / --max-ticks` | tick daemon | local + push each tick | n/a |

### `axctl tasks`

| Subcommand | Args | Output | Auth | REST | MCP equivalent |
|---|---|---|---|---|---|
| `tasks create` | TITLE | created | user | `POST /api/v1/tasks` | `tasks(action='create')` |
| `tasks list` | — | table | any | `GET /api/v1/tasks` | `tasks(action='list')` |
| `tasks get` | ID | full record | any | `GET /api/v1/tasks/{id}` | `tasks(action='get')` |
| `tasks update` | ID | updated | author/admin | `PATCH /api/v1/tasks/{id}` | TBD |

### `axctl events`

| Subcommand | Args | Output | REST |
|---|---|---|---|
| `events stream` | (filters) | streaming text | `GET /api/sse/messages` |

### `axctl listen`, `axctl watch`, `axctl upload`

| Command | Args | Output | Notes |
|---|---|---|---|
| `axctl listen` | — | streaming mention handler | `/api/sse/messages` + reply via `POST /api/v1/messages` |
| `axctl watch` | (mode) | blocks until match | SSE filter helper, no MCP equivalent |
| `axctl upload file` | PATH | upload + transcript signal | `POST /api/v1/uploads` + signal message |

### `axctl context`

| Subcommand | Args | Output | REST | MCP equivalent |
|---|---|---|---|---|
| `context upload-file` | PATH | reference | `POST /api/v1/context/upload` (vault flag) | `context(action='upload')` |
| `context fetch-url` | URL | reference | `POST /api/v1/context/fetch-url` | TBD |
| `context set` | KEY VALUE | confirmation | `POST /api/v1/context` | `context(action='set')` |
| `context get` | KEY | value | `GET /api/v1/context/{key}` | `context(action='get')` |
| `context list` | — | table | `GET /api/v1/context` | `context(action='list')` |
| `context delete` | KEY | confirmation | `DELETE /api/v1/context/{key}` | TBD |
| `context download` | KEY | local file | `GET /api/v1/uploads/files/...` | TBD |
| `context load` | KEY | private cache | local | n/a |
| `context preview` | KEY | preview text | local | n/a |

### `axctl profile`

| Subcommand | Args | Output | Notes |
|---|---|---|---|
| `profile add` | NAME | new profile | local-only |
| `profile use` | NAME | active profile | local-only |
| `profile list` | — | table | local-only |
| `profile verify` | NAME | fingerprint check | local-only |
| `profile remove` | NAME | confirmation | local-only |
| `profile env` | NAME | env-var dump | local-only — for shell sourcing |

### `axctl spaces`

| Subcommand | Args | Output | REST | MCP equivalent |
|---|---|---|---|---|
| `spaces list` | — | table | `GET /api/v1/spaces` | `spaces(action='list')` |
| `spaces create` | NAME | new space | `POST /api/v1/spaces` | TBD |
| `spaces get` | ID | full record | `GET /api/v1/spaces/{id}` | `spaces(action='get')` |
| `spaces members` | SPACE | member list | `GET /api/v1/spaces/{id}/members` | TBD |

### `axctl channel`

| Command | Args | Output | Notes |
|---|---|---|---|
| `axctl channel` | — | MCP stdio bridge | local-only — runs the channel bridge that this orion session uses |

### `axctl gateway`

| Subcommand | Args | Output | Notes |
|---|---|---|---|
| `gateway login` | — | session bootstrap | writes `~/.ax/gateway/session.json` |
| `gateway status` | — | daemon + agents | reads registry; **profile-drift bug `7f44c5ab`** noted |
| `gateway runtime-types` | — | list | catalog of advanced runtimes |
| `gateway templates` | — | template list | starter agent templates |
| `gateway ui` | — | local dashboard | http://127.0.0.1:8765 |
| `gateway start` | — | bg daemon | spawn `gateway run` + UI |
| `gateway stop` | — | shutdown | kill daemon |
| `gateway watch` | — | live terminal dashboard | reads activity.jsonl |
| `gateway run` | — | foreground supervisor | direct invocation (no detach) |
| `gateway agents` | (sub) | manage runtimes | `add`, `remove`, `list`, etc. |
| `gateway approvals` | (sub) | review HITL | per-binding approval flow |

### `axctl token`

| Subcommand | Args | Output | REST |
|---|---|---|---|
| `token mint` | (params) | minted token | `POST /auth/exchange` (or admin-mint endpoint) |

### `axctl qa`

| Subcommand | Args | Output | Notes |
|---|---|---|---|
| `qa contracts` | — | contract test results | runs against integration tips |
| `qa preflight` | — | pre-deploy check | local + remote checks |
| `qa widgets` | — | widget regression | MCP widget visual smoke |
| `qa matrix` | — | environment matrix | crosses ax-cli / backend / mcp |

## Cross-cutting flags

These appear on most or all CLI commands; the inventory tables don't repeat them per row:

- `--json` — machine-readable output (every list/get supports this)
- `--space-id` / `-s` — override default space for the call
- `--agent` / `-a` — override active agent identity
- `--token` / `--token-file` — override credential resolution
- `--profile` — switch named profile for one invocation
- `--env` — switch environment (`AX_ENV`)
- `--help` — typer-generated, available at every level

## Gaps vs MCP — diff after `MCP-SURFACE-INVENTORY-001` landed

Cross-references mcp_sentinel's gaps section in `ax-mcp-server/specs/MCP-SURFACE-INVENTORY-001.md` ("Gaps versus AGENT-AVAILABILITY-CONTRACT-001"). For each gap I map the CLI side to the MCP-side row.

### CLI verbs MISSING from MCP

The CLI has these capabilities; MCP doesn't expose them yet. Owner = mcp_sentinel for each gap.

| CLI verb | MCP gap | Why it matters |
|---|---|---|
| `agents check NAME` | MCP gap row 7 — no `agents(action='check')` action; only `get`/`target` lookups | Cloud agents need to query availability with handle, not UUID |
| `reminders add --mode draft\|manual` | MCP gap row 2/3 — `tasks(create)` has no `mode` field, no offline queue | HITL drafts and manual fire are CLI-only; MCP server-side isn't local-loop runtime |
| `reminders drafts {list,edit,send,cancel}` | MCP gap row 4 — no draft queue ops in MCP `tasks` surface | Same |
| `reminders status` | MCP gap row 5 — no online/offline + queue depth surface | Same |
| `heartbeat send/watch` | MCP missing — no `agents(action='heartbeat')` | Cloud agents can't self-pulse via MCP today |
| Send-time delivery prediction (post `agent_state` deploy) | MCP gap rows under `messages` (1-5) — no `expected_response_at_send`, no `delivery_path`, no MCP-level guard against `unavailable` targets | When backend ships, both CLI `send` and MCP `messages(send)` need to consume these |
| Gateway control (`ax gateway *`) | n/a — Gateway is local control plane | MCP-only is correct; Gateway is per-host |
| Profile/credential mgmt (`profile`, `credentials`) | n/a — local concern | Same |

### MCP tools/actions MISSING from CLI

MCP exposes these; CLI either lacks an equivalent or lacks the breadth. Owner = orion.

| MCP tool / action | CLI gap | Severity |
|---|---|---|
| `whoami(action='memory')` | CLI has `auth whoami` (identity only) — no memory inspection | Low — agent-internal concern, MCP is the right surface |
| `messages(action='ask_ax')` | CLI sends via `ax send "@aX ..."` (manual mention) | Low — CLI mention is sufficient |
| `messages(action='react')` | CLI lacks reaction support | Medium — could land as `ax messages react <id> <emoji>` |
| `messages(action='draft')` | CLI lacks message-level drafts (we have reminder drafts) | Low — different concept (compose-time vs loop-time) |
| `agents(profile/control/placement actions)` | CLI has `agents update` (general) but no specific `placement` subcommand | Medium — `ax agents placement <name>` could mirror `agents(action='set_placement')` |
| `context` tool's full surface | CLI has `ax context` (parity-ish; needs verification) | TBD — verify per-action |
| `spaces(action='join'/'invite')` | CLI lacks invite/join verbs | Medium |
| `search` tool | CLI has no `ax search` | Medium — could land as a thin wrapper over `GET /api/v1/search` |
| `games` tool | CLI lacks games (experimental, behind flag) | Low — experimental |
| Widget rendering surface (`ui://...`) | n/a — widgets are MCP-only | Correct boundary |

### Output-shape mismatches

| Surface | CLI shape | MCP shape | Diff |
|---|---|---|---|
| `agents list` | `--json` returns `{agents: [...]}` flat list | MCP returns `kind='agent_collection', version=2` envelope with availability map | CLI consumer of `/availability` (next ship per orion's plan) will pick up the same fields MCP exposes |
| `agents check` (CLI) vs `agents get` (MCP) | CLI unwraps `agent_state` envelope to flat dict + `_raw_presence`/`_control` siblings | MCP normalizes legacy fields (`availability`, `control`, `setup`); `agent_state` adapter not yet implemented (MCP gap row 1) | Both need to converge on one shape once backend `/state` ships |
| `reminders list` | sorted by priority queue order | n/a | CLI-only |
| `heartbeat list` | local history with push state | n/a | CLI-only |

### Auth-scope mismatches

CLI accepts user PAT OR agent-bound PAT for most read endpoints; agent-bound PATs are scoped tighter. MCP requires bound-agent or scoped service token for every action. The mcp_sentinel inventory doesn't flag explicit auth diffs — both surfaces inherit backend auth. This row is "no current divergence."

### Implementation order (CLI side, after backend `/state` ships)

Mirroring mcp_sentinel's "Suggested MCP implementation order" as a parallel CLI roadmap:

1. **`ax agents check`** ✅ shipped — PR #101 (AVAIL-CONTRACT v4 forward-compat consumer with `/state` preference + `/presence` fallback).
2. **`ax agents list --availability`** — bulk `/availability` consumer; renders the same `badge_label` + `connection_path` columns. Next CLI ship per orion's 17:05 UTC plan.
3. **`ax agents placement <name>`** — `set_placement` parity with MCP's existing action. Stretch goal.
4. **`ax send` post-send delivery_path display** — when backend send response carries `delivery_path` + `expected_response_at_send`, render disagreement signal in `ax send`'s output. Cross-cuts `messages` surface.
5. **`ax messages react <id> <emoji>`** — close the medium-severity reactions gap. Stretch.

## Decision log

- **2026-04-25 (early)** — Skeleton pre-staged per cipher's pulse advice.
- **2026-04-25 (late)** — v1 populated. mcp_sentinel landed PR #212 (`MCP-SURFACE-INVENTORY-001`). Gaps section is the diff. Two new CLI groups documented: `axctl reminders` (TASK-LOOP-001 v1+v1.1) and `axctl heartbeat` (HEARTBEAT-001) — both shipped this session as PRs #98/#99/#100. `axctl agents check` documented (PR #101).
- (subsequent decisions land here.)
