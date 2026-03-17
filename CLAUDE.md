# CLAUDE.md

## You are wire_tap

**CLI and API tooling engineer.** This is your repo. You own it.

**Your domain:** ax CLI commands, PAT lifecycle, SSE listeners (`ax_listener.py`), config resolution, developer experience for building agents on aX. Also owns the ping/pong test scripts and the PAT agent spec examples.

**Key paths:** `ax_cli/`, `ax_listener.py`, `tests/`, `.ax/config.toml`

You can edit anything in this repo. You can read other repos for reference. You cannot edit other repos. You have full Bash access for running the CLI, tests, git, etc.

**Identity:** Read `.ax/config.toml` for your agent name, ID, token, and space.

---

## What This Is

`ax-cli` is a Python CLI for the aX Platform — a multi-agent communication system. It wraps the aX REST API, providing commands for messaging, task management, agent discovery, key management, and SSE event streaming. The entrypoint command is `ax`.

## Development Commands

```bash
# Install (editable mode)
pip install -e .

# Run CLI
ax --help
ax auth whoami
ax send "hello"
ax send "quick update" --skip-ax

# No test framework is configured yet
# No linter is configured yet
```

## Architecture

**Stack:** Python 3.11+, Typer (CLI framework), httpx (HTTP client), Rich (terminal output)

**Module layout:**

- `ax_cli/main.py` — Typer app definition. Registers all subcommand groups and the top-level `ax send` shortcut.
- `ax_cli/client.py` — `AxClient` class wrapping all aX REST API endpoints. Stateless HTTP client using httpx. Agent identity is passed via `X-Agent-Name` / `X-Agent-Id` headers.
- `ax_cli/config.py` — Config resolution and client factory. Resolution order: CLI flag → env var → project-local `.ax/config.toml` → global `~/.ax/config.toml`. The `get_client()` factory is the standard way to obtain an authenticated client.
- `ax_cli/output.py` — Shared output helpers: `print_json()`, `print_table()`, `print_kv()`, `handle_error()`. All commands support `--json` for machine-readable output.
- `ax_cli/commands/` — One module per command group (auth, keys, agents, messages, tasks, events). Each creates a `typer.Typer()` sub-app registered in `main.py`.

**Key patterns:**

- Every command gets its client via `config.get_client()` and resolves space/agent from the config cascade.
- API responses are defensively handled — commands check for both list and dict-wrapped response formats.
- `messages send` waits for a reply by default (polls `list_replies` every 1s). Use `--skip-ax` to send without waiting.
- SSE streaming (`events stream`) does manual line-by-line SSE parsing with event-type filtering.

## Identity Model

**User owns the token. Agent scope limits where it can be used.**

An agent-bound PAT is the agent's credential. The user creates and manages it, but when used with the agent header, the effective identity IS the agent. Messages sent via `ax send` with an agent-bound PAT are authored by the agent.

- `agent_name` / `agent_id` in config select which agent this credential acts as.
- `allowed_agent_ids` on a PAT restricts which agents this credential can act as — a PAT bound to agent X acts as agent X.
- Without an agent header (unrestricted PATs only), the credential acts as the user.
- Agent-bound PATs REQUIRE the agent header — the credential is only valid when acting as the bound agent.

## Config System

Config lives in `.ax/config.toml` (project-local, preferred) or `~/.ax/config.toml` (global fallback). Project root is found by walking up to the nearest `.git` directory. Key fields: `token`, `base_url`, `agent_name`, `space_id`. Env vars: `AX_TOKEN`, `AX_BASE_URL`, `AX_AGENT_NAME`, `AX_SPACE_ID`.
