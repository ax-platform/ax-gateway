# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Agent Role (pulse)

This repo is maintained by the **pulse** agent. Pulse's job is to make ax-cli production-ready:
- Test and validate every command against the local API (`http://localhost:8001`)
- Find and fix bugs
- Keep docs and README current
- Prepare for eventual public release (repo is private for now)

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

A PAT remains a user credential, but it may be restricted (bound) to a specific agent. In that case, the token is only valid for interactions with that agent. Messages sent via `ax send` are authored by the user, not the agent.

- `agent_name` / `agent_id` in config select which agent surface the user interacts with.
- `allowed_agent_ids` on a PAT restricts which agents this credential can be used with — a PAT bound to agent X is only valid for interactions with agent X.
- For agent-principal actions (agent sending as itself), agents use backend-issued RS256 JWTs, not PATs. This happens during dispatch, not via the CLI.

## Config System

Config lives in `.ax/config.toml` (project-local, preferred) or `~/.ax/config.toml` (global fallback). Project root is found by walking up to the nearest `.git` directory. Key fields: `token`, `base_url`, `agent_name`, `space_id`. Env vars: `AX_TOKEN`, `AX_BASE_URL`, `AX_AGENT_NAME`, `AX_SPACE_ID`.
