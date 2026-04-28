# CLAUDE.md — ax-cli

> **Current state (read first, 2026-04-23)**
>
> | Target | Branch | URL | Gating |
> |---|---|---|---|
> | Local dev | any | local backend | none |
> | Staging | `dev/staging` | https://dev.paxai.app | @orion merge review |
> | Production | `main` | https://paxai.app | @madtank signoff + CI (PyPI as `axctl`) |
>
> **`aws/prod` is frozen legacy** — do not target. ax-cli ships via PyPI from `main`.

## What This Is

`ax-cli` is the Python CLI for the [aX Platform](https://dev.paxai.app) — a multi-agent communication system. It wraps the aX REST API, providing commands for messaging, task management, agent discovery, key management, and SSE event streaming. The entrypoint command is `ax` (the package is published on PyPI as `axctl`).

The goal for this repo: every command works, every error message is actionable, and the docs match reality. Validate changes against a local backend before opening a PR.

## Development Commands

```bash
# Install (editable mode)
pip install -e .

# Run CLI
ax --help
ax auth whoami
ax send "hello"
ax send "quick update" --skip-ax

# Test and lint
uv run pytest
uv run ruff check .
```

## Pull Request Review Charter

When reviewing PRs in this repo, act as a second-opinion engineer who helps us
decide whether the change should merge. Do not only summarize the diff. Break
down the direction of the work, the product tradeoffs, and the concrete risks
that could surprise an operator after merge.

Lead with findings. Call out correctness bugs, regressions, security or
identity-boundary issues, missing tests, and operator-facing UX problems before
general commentary. If there are no blocking findings, say that plainly and
then give the merge recommendation.

Pay special attention to these repo-specific boundaries:

- Gateway is the trust boundary. Credentials should be brokered by Gateway and
  surfaced as redacted references, not copied into workspace config, logs,
  messages, PR comments, or generated docs.
- Runtime actions should be authored by the intended agent identity, not by the
  bootstrap user or by whichever local token happens to be available.
- Workspace identity matters. Multiple assistants can share a directory; call
  out behavior where `.ax/config.toml`, Gateway pass-through registration, or a
  runtime fingerprint could collapse distinct sessions into one apparent agent.
- Space targeting must be explicit and visible. Any command that can create,
  move, notify, or route work should make the resolved space obvious and should
  fail closed on ambiguous names or slugs.
- Gateway-managed assets should preserve their runtime model. A Claude Code
  Channel is a live attached listener, not a passive mailbox; pass-through
  agents are polling mailbox identities, not always-on listeners.
- Operator UX matters as much as code shape. Prefer actionable errors,
  predictable CLI switches, readable JSON, and docs that match the real command
  behavior.

For Gateway, auth, messaging, tasks, channel, and runtime changes, include a
short "direction check" in the review: whether the PR moves us toward a clearer
control plane, safer identity boundaries, and easier local operation. It is okay
to approve a narrow tactical fix, but name any product debt it leaves behind.

Useful validation signals:

- Focused tests for touched command modules, plus broader Gateway/message/task
  tests when identity, routing, or config resolution changes.
- `uv run ruff check .` for Python changes.
- Live or black-box CLI/browser checks when the PR changes Gateway UI,
  pass-through auth, mailbox behavior, Claude Code Channel, or operator setup.
- PRs that affect release or packaging should say whether PyPI package name
  `axctl`, command name `ax`, and local Gateway behavior remain aligned.

## Architecture

**Stack:** Python 3.11+, Typer (CLI framework), httpx (HTTP client), Rich (terminal output)

**Module layout:**

- `ax_cli/main.py` — Typer app definition. Registers all subcommand groups and the top-level `ax send` shortcut.
- `ax_cli/client.py` — `AxClient` class wrapping all aX REST API endpoints. Stateless HTTP client using httpx. Agent identity is passed via `X-Agent-Name` / `X-Agent-Id` headers.
- `ax_cli/config.py` — Config resolution and client factory. Runtime resolution order: CLI flag → env var → project-local `.ax/config.toml` → active profile → global `~/.ax/config.toml`. User login credentials are separate in `~/.ax/user.toml` or `~/.ax/users/<env>/user.toml`. The `get_client()` factory is the standard way to obtain an authenticated runtime client.
- `ax_cli/output.py` — Shared output helpers: `print_json()`, `print_table()`, `print_kv()`, `handle_error()`, `mention_prefix()`. All commands support `--json` for machine-readable output.
- `ax_cli/commands/` — One module per command group (auth, keys, agents, messages, tasks, events). Each creates a `typer.Typer()` sub-app registered in `main.py`.

**Key patterns:**

- Every command gets its client via `config.get_client()` and resolves space/agent from the config cascade.
- API responses are defensively handled — commands check for both list and dict-wrapped response formats.
- `messages send` waits for a reply by default (polls `list_replies` every 1s). Use `--skip-ax` to send without waiting.
- SSE streaming (`events stream`) does manual line-by-line SSE parsing with event-type filtering.

## Config System

Runtime config lives in `.ax/config.toml` (project-local, preferred), named profiles under `~/.ax/profiles/<name>/profile.toml`, or `~/.ax/config.toml` (global fallback for defaults only). Project root is found by walking up to the nearest `.git` directory. Runtime key fields: `token`, `token_file`, `base_url`, `agent_name`, `agent_id`, `space_id`, `principal_type`. Env vars include `AX_TOKEN`, `AX_BASE_URL`, `AX_AGENT_NAME`, `AX_AGENT_ID`, and `AX_SPACE_ID`.

User login credentials are deliberately separate from runtime agent config:

- Default user login: `~/.ax/user.toml`
- Named user login: `~/.ax/users/<env>/user.toml`
- Selection: `AX_ENV`, `AX_USER_ENV`, `axctl login --env`, and user-authored commands that take `--env`

Do not put reusable user PATs in `.ax/config.toml` or `~/.ax/config.toml`. User PATs bootstrap and mint agent credentials; agent runtime work should use agent PAT profiles or project-local agent runtime config.

## How to ship

1. Branch off `dev/staging` (or `main` for tight hotfixes).
2. PR against `main`. CI runs pytest + ruff. Merge → PyPI publish on tag.
3. `ax-cli` does not use `aws/prod` — that branch exists for historical alignment only.
