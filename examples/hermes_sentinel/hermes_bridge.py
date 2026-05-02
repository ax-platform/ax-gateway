#!/usr/bin/env python3
"""hermes_bridge.py — minimal aX agent powered by hermes-agent.

Receives a single @mention from `ax listen --exec`, routes it through
hermes's `AIAgent`, and prints the final response to stdout (which
`ax listen` posts back to aX as the agent's reply).

This is the simplest possible way to give an aX agent a capable brain
with tools. One process per mention, no cross-mention memory. For a
long-running agent with session memory and streaming SSE signals, see
the production sentinels in the aX platform repo.

Usage
-----

    ax listen \\
        --agent my_agent \\
        --exec "python examples/hermes_sentinel/hermes_bridge.py"

Prerequisites
-------------

1. The hermes-agent repo is cloned locally and installed in a venv.
   See https://github.com/alex/hermes-agent for install steps.

2. `HERMES_REPO_PATH` env var points at the hermes-agent checkout
   (or the default `~/hermes-agent` exists).

3. An LLM credential is available:
   - `ANTHROPIC_API_KEY` for `anthropic:claude-*` models
   - `CODEX_API_KEY` (or `~/.hermes/auth.json`) for `codex:gpt-*` models
   - `OPENROUTER_API_KEY` for `openrouter:*` models

4. aX agent is registered and `ax listen` has valid config (see README).

Configuration via env vars
--------------------------

- `HERMES_MODEL` — model id (default: `codex:gpt-5.4`)
- `HERMES_REPO_PATH` — path to hermes-agent checkout
- `HERMES_MAX_ITERATIONS` — max tool-calling turns (default: 30)
- `HERMES_SYSTEM_PROMPT` — system prompt for the agent
- `HERMES_WORKDIR` — working directory for file tools (default: cwd)

Design notes
------------

- `skip_memory=True` is deliberate. Hermes's built-in MemoryStore writes
  to a single shared path (`~/.hermes/memories/MEMORY.md`). If you run
  more than one hermes-backed agent on the same host, they would stomp
  each other's memory file. For per-agent persistence, write markdown
  notes to your agent's own directory using the normal file tools.

- `skip_context_files=True` stops hermes from auto-loading `CLAUDE.md`
  / `AGENTS.md`. Pass your system prompt explicitly via the
  `HERMES_SYSTEM_PROMPT` env var so behavior is predictable.

- No security wrapping. This example lets hermes's file/terminal tools
  operate freely within the working directory. For a multi-agent or
  multi-tenant setup, wrap the tools with path/command guards before
  passing them to the agent — see the production sentinels for a
  reference implementation.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

# ─── AX_GATEWAY_EVENT protocol ────────────────────────────────────────────
# Lines prefixed with `AX_GATEWAY_EVENT ` on stdout are parsed by the Gateway
# and forwarded as platform `agent_processing` / tool-call events so the UI
# sees per-mention phase text while the agent is still working. Unprefixed
# stdout lines accumulate into the final reply body.
EVENT_PREFIX = "AX_GATEWAY_EVENT "

# Preserve the *original* stdout before we suppress hermes's internal prints.
# AX_GATEWAY_EVENT lines and the final reply must reach Gateway, but hermes's
# chatter ("deliberating...", tool previews, etc.) must not — otherwise the
# Gateway captures it all as the reply body. `_real_stdout` is the escape hatch
# for our own prints.
_real_stdout = sys.stdout


def _emit_event(payload: dict) -> None:
    _real_stdout.write(f"{EVENT_PREFIX}{json.dumps(payload, sort_keys=True)}\n")
    _real_stdout.flush()


def _print_reply(text: str) -> None:
    _real_stdout.write(text + "\n")
    _real_stdout.flush()


# ─── Resolve hermes-agent location ─────────────────────────────────────────
HERMES_REPO = Path(os.environ.get("HERMES_REPO_PATH", str(Path.home() / "hermes-agent"))).expanduser()

if not HERMES_REPO.exists():
    print(
        f"ERROR: hermes-agent repo not found at {HERMES_REPO}.\n"
        f"Set HERMES_REPO_PATH or clone hermes-agent to ~/hermes-agent.",
        file=sys.stderr,
    )
    sys.exit(1)

sys.path.insert(0, str(HERMES_REPO))

# Make hermes's venv packages importable when running under the system python
_venv_lib = HERMES_REPO / ".venv" / "lib"
if _venv_lib.exists():
    for site_packages in _venv_lib.glob("python*/site-packages"):
        if str(site_packages) not in sys.path:
            sys.path.insert(0, str(site_packages))

try:
    from run_agent import AIAgent  # noqa: E402  (path set above)
except ImportError as e:
    print(
        f"ERROR: failed to import hermes AIAgent from {HERMES_REPO}: {e}\n"
        f"Did you run `pip install -e .` in the hermes-agent venv?",
        file=sys.stderr,
    )
    sys.exit(1)


# ─── Provider resolution ───────────────────────────────────────────────────
def _resolve_provider(model: str) -> dict:
    """Resolve provider config from a model string.

    Model format: "provider:model_name" or a bare model name.
    Examples:
        codex:gpt-5.4                 → Codex Responses API
        anthropic:claude-sonnet-4.6   → Anthropic Messages API
        openrouter:anthropic/claude-* → OpenRouter chat completions
    """
    if ":" in model:
        hint, name = model.split(":", 1)
    else:
        hint, name = "", model

    if hint == "anthropic" or (not hint and "claude" in name.lower()):
        return {
            "provider": "anthropic",
            "api_mode": "anthropic_messages",
            "base_url": os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
            "api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
            "model": name,
        }

    if hint == "openrouter":
        return {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": os.environ.get("OPENROUTER_API_KEY", ""),
            "model": name,
        }

    # Default: Codex (GPT-5.x) via ChatGPT Codex backend.
    codex_key = os.environ.get("CODEX_API_KEY", "").strip()
    if not codex_key:
        # Fall back to ~/.hermes/auth.json (the format hermes-cli maintains).
        import json

        auth_path = Path.home() / ".hermes" / "auth.json"
        if auth_path.exists():
            try:
                data = json.loads(auth_path.read_text())
                providers = data.get("providers") or {}
                active = data.get("active_provider") or next(iter(providers.keys()), None)
                tokens = (providers.get(active) or {}).get("tokens") or {}
                codex_key = tokens.get("access_token", "")
            except (OSError, json.JSONDecodeError):
                pass

    return {
        "provider": "openai-codex",
        "api_mode": "codex_responses",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_key": codex_key,
        "model": name or "gpt-5.4",
    }


def main() -> int:
    # ─── Read mention content ──────────────────────────────────────────────
    # ax listen passes the mention as both $1 and $AX_MENTION_CONTENT.
    content = ""
    if len(sys.argv) > 1:
        content = sys.argv[-1]
    if not content:
        content = os.environ.get("AX_MENTION_CONTENT", "")
    if not content:
        print("(no mention content received)", file=sys.stderr)
        return 1

    # ─── Resolve provider + auth ───────────────────────────────────────────
    model = os.environ.get("HERMES_MODEL", "codex:gpt-5.4")
    cfg = _resolve_provider(model)
    if not cfg["api_key"]:
        print(
            f"ERROR: no API key resolved for provider={cfg['provider']}.\n"
            f"Set the appropriate env var (ANTHROPIC_API_KEY, CODEX_API_KEY, "
            f"or OPENROUTER_API_KEY) or populate ~/.hermes/auth.json.",
            file=sys.stderr,
        )
        return 1

    # ─── Build the agent ───────────────────────────────────────────────────
    workdir = os.environ.get("HERMES_WORKDIR", os.getcwd())
    max_iterations = int(os.environ.get("HERMES_MAX_ITERATIONS", "30"))
    system_prompt = os.environ.get(
        "HERMES_SYSTEM_PROMPT",
        "You are a helpful assistant. Be concise — your response is "
        "posted to an aX chat channel, so keep it under 2000 characters "
        "unless the task genuinely requires more detail.",
    )

    # Change to workdir so relative file tool paths behave predictably.
    os.chdir(workdir)

    # ─── Phase events for Gateway/UI ───────────────────────────────────────
    # `tool_progress_callback` is an AIAgent constructor param (see
    # hermes_agent/run_agent.py:446). It fires before each tool call with
    # (name, args_preview, args_dict). We translate those into
    # AX_GATEWAY_EVENT tool_start/tool_result pairs so the UI chip
    # reflects what Hermes is actually doing.
    #
    # `activity` is the human-readable chip text (<40 chars). Extracting
    # a salient arg per tool makes the bubble read
    #   "@X: $ pwd"  or  "@X: reading AGENTS.md"
    # instead of the generic "@X is using terminal…".

    def _tool_activity(tool_name: str, args: dict | None) -> str:
        args = args if isinstance(args, dict) else {}
        name = (tool_name or "").lower()
        # Normalize synonyms across hermes's tool namespace
        if name in {"terminal", "bash", "shell", "run_command"}:
            cmd = args.get("command") or args.get("cmd") or ""
            cmd = str(cmd).splitlines()[0].strip() if cmd else ""
            return f"$ {cmd}"[:80] if cmd else "running shell"
        if name in {"read_file", "read", "view"}:
            path = args.get("path") or args.get("file") or args.get("filename") or ""
            return f"reading {path}"[:80] if path else "reading file"
        if name in {"write_file", "write", "create_file"}:
            path = args.get("path") or args.get("file") or args.get("filename") or ""
            return f"writing {path}"[:80] if path else "writing file"
        if name in {"edit_file", "edit", "str_replace"}:
            path = args.get("path") or args.get("file") or ""
            return f"editing {path}"[:80] if path else "editing file"
        if name in {"search", "grep", "find"}:
            pattern = args.get("pattern") or args.get("query") or args.get("q") or ""
            return f"searching {pattern}"[:80] if pattern else "searching"
        if name in {"web_search", "search_web"}:
            q = args.get("query") or args.get("q") or ""
            return f"web search: {q}"[:80] if q else "web search"
        if name in {"fetch", "http_get", "url"}:
            url = args.get("url") or args.get("target") or ""
            return f"fetching {url}"[:80] if url else "fetching"
        # Generic fallback: surface the first scalar arg value
        for k, v in args.items():
            if isinstance(v, (str, int, float, bool)):
                return f"{tool_name}: {str(v)}"[:80]
        return f"using {tool_name}"[:80]

    def _on_tool_progress(tool_name: str, args_preview: str, args_dict=None):
        tool_call_id = f"hermes-{uuid.uuid4()}"
        args = args_dict if isinstance(args_dict, dict) else {}
        activity = _tool_activity(tool_name, args)
        try:
            _emit_event(
                {
                    "kind": "tool_start",
                    "tool_name": tool_name,
                    "tool_action": tool_name,
                    "tool_call_id": tool_call_id,
                    "status": "tool_call",
                    "arguments": args,
                    "activity": activity,
                    "message": activity,
                }
            )
            _emit_event(
                {
                    "kind": "tool_result",
                    "tool_name": tool_name,
                    "tool_action": tool_name,
                    "tool_call_id": tool_call_id,
                    "status": "tool_complete",
                    "activity": activity,
                    "message": activity,
                }
            )
        except Exception:
            pass  # never let event emission break the agent run

    agent = AIAgent(
        base_url=cfg["base_url"],
        api_key=cfg["api_key"],
        provider=cfg["provider"],
        api_mode=cfg["api_mode"],
        model=cfg["model"],
        max_iterations=max_iterations,
        tool_delay=0.5,
        quiet_mode=True,
        skip_context_files=True,  # explicit prompt only — no auto CLAUDE.md
        skip_memory=True,  # see Design notes in module docstring
        disabled_toolsets=[
            "web",
            "browser",
            "image_generation",
            "tts",
            "vision",
            "cronjob",
            "rl_training",
            "homeassistant",
        ],
        tool_progress_callback=_on_tool_progress,
    )

    _emit_event({"kind": "status", "status": "started", "message": "Agent planning"})
    _emit_event({"kind": "status", "status": "thinking", "message": "Thinking"})

    # ─── Run a single conversation turn ────────────────────────────────────
    # Hermes's AIAgent prints progress chatter ("deliberating...", tool
    # previews, "🔧 ..." etc.) to stdout even with quiet_mode=True.  If we
    # don't suppress it, Gateway captures all of it as the reply body.
    # Redirect sys.stdout/sys.stderr to devnull during the run; our own
    # AX_GATEWAY_EVENT emissions write to `_real_stdout` directly and
    # still reach Gateway.
    saved_stdout = sys.stdout
    saved_stderr = sys.stderr
    devnull = open(os.devnull, "w")
    sys.stdout = devnull
    sys.stderr = devnull
    try:
        try:
            result = agent.run_conversation(
                user_message=content,
                system_message=system_prompt,
            )
        except Exception as run_err:
            sys.stdout = saved_stdout
            sys.stderr = saved_stderr
            _emit_event({"kind": "status", "status": "error", "message": f"Agent error: {run_err}"[:200]})
            print(f"Hermes bridge failed: {run_err}", file=saved_stderr)
            return 1
    finally:
        sys.stdout = saved_stdout
        sys.stderr = saved_stderr
        devnull.close()

    final_text = result.get("final_response", "").strip()
    if not final_text:
        _emit_event({"kind": "status", "status": "error", "message": "Agent produced no output"})
        print("(agent produced no output)", file=sys.stderr)
        return 1

    _emit_event({"kind": "status", "status": "completed", "message": "Reply ready"})

    # Gateway captures unprefixed stdout lines → posts to aX as the reply.
    _print_reply(final_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
