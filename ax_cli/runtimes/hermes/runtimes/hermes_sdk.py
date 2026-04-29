# Vendored from ax-agents on 2026-04-25 — see ax_cli/runtimes/hermes/README.md
"""Hermes SDK runtime — wraps hermes-agent's AIAgent for aX sentinels.

Supports multiple backends:
  - codex_responses: GPT-5.4 via ChatGPT Codex endpoint (sentinel workers)
  - anthropic_messages: Claude via Anthropic API or Bedrock-compatible proxy
  - chat_completions: Any OpenAI-compatible endpoint (OpenRouter, local, etc.)

Security:
  - Credentials read from files with restricted permissions, never logged
  - AWS auth via IAM role / instance profile (no hardcoded keys)
  - Token sources validated before use
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

from . import BaseRuntime, RuntimeResult, StreamCallback, register

log = logging.getLogger("runtime.hermes_sdk")

# Hermes repo path — must be on sys.path for AIAgent import
HERMES_REPO = Path(os.environ.get(
    "HERMES_REPO_PATH",
    "/home/ax-agent/shared/repos/hermes-agent",
))
# Hermes venv python — used to verify the environment
HERMES_VENV = HERMES_REPO / ".venv"

# Codex auth — multiple sources, resolved in priority order.
# ~/.hermes/auth.json is the canonical source (auto-refreshed by hermes-cli).
# ~/.codex/auth.json is the Codex CLI native store (same structure).
# ~/.ax/codex-token is a legacy plain-text fallback.
HERMES_AUTH_PATH = Path.home() / ".hermes" / "auth.json"
CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"
CODEX_SHARED_TOKEN_PATH = Path.home() / ".ax" / "codex-token"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"


def _read_token_file(path: Path) -> str:
    """Read a token file securely. Returns empty string on failure."""
    try:
        stat = path.stat()
        # Warn if token file is world-readable
        if stat.st_mode & 0o077:
            log.warning("Token file %s has loose permissions (mode %o). "
                        "Run: chmod 600 %s", path, stat.st_mode & 0o777, path)
        return path.read_text().strip()
    except OSError:
        return ""


def _extract_access_token_from_auth_json(path: Path) -> str:
    """Extract the access_token from a hermes/codex auth.json file.

    Both ~/.hermes/auth.json and ~/.codex/auth.json use the same token
    structure: tokens.access_token is the OAuth bearer token.

    Hermes format:
        {"providers": {"openai-codex": {"tokens": {"access_token": "..."}}}}
    Codex CLI format:
        {"tokens": {"access_token": "..."}}
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return ""

    # Hermes format: providers.<provider>.tokens.access_token
    providers = data.get("providers") or {}
    if providers:
        active = data.get("active_provider") or next(iter(providers.keys()), None)
        provider = providers.get(active) or {}
        tokens = provider.get("tokens") or {}
        token = tokens.get("access_token", "")
        if token:
            return token

    # Codex CLI format: tokens.access_token
    tokens = data.get("tokens") or {}
    token = tokens.get("access_token", "")
    if token:
        return token

    # Last resort: top-level "token" key (legacy format)
    return data.get("token", "")


def _resolve_codex_token() -> str:
    """Resolve Codex OAuth token from available sources.

    Priority:
      1. CODEX_API_KEY env var (explicit override)
      2. ~/.hermes/auth.json (canonical, auto-refreshed)
      3. ~/.codex/auth.json (Codex CLI native)
      4. ~/.ax/codex-token (legacy plain-text fallback)

    Note: the legacy ~/.ax/codex-token file has historically contained an
    aX Platform PAT (axp_u_*) which is NOT a valid Codex bearer token.
    We explicitly reject tokens with that prefix to prevent regression.
    """
    # 1. Environment override
    env_token = os.environ.get("CODEX_API_KEY", "").strip()
    if env_token and not env_token.startswith("axp_"):
        return env_token

    # 2. Hermes auth store (canonical)
    token = _extract_access_token_from_auth_json(HERMES_AUTH_PATH)
    if token and not token.startswith("axp_"):
        return token

    # 3. Codex CLI auth store
    token = _extract_access_token_from_auth_json(CODEX_AUTH_PATH)
    if token and not token.startswith("axp_"):
        return token

    # 4. Legacy plain-text file (reject aX PATs)
    token = _read_token_file(CODEX_SHARED_TOKEN_PATH)
    if token and not token.startswith("axp_"):
        return token
    if token.startswith("axp_"):
        log.error(
            "Legacy %s contains an aX Platform PAT (axp_*), not a Codex token. "
            "Sentinels will fail authentication. Fix: copy the access_token from "
            "~/.hermes/auth.json, or run `hermes login`.",
            CODEX_SHARED_TOKEN_PATH,
        )

    return ""


def _resolve_provider_config(model: str | None) -> dict:
    """Resolve provider, base_url, api_key, and api_mode from model string.

    Model format: "provider:model_name" or just "model_name"
    Examples:
      "codex:gpt-5.4"           → Codex Responses API
      "anthropic:claude-sonnet-4.6" → Anthropic Messages API
      "openrouter:anthropic/claude-sonnet-4.6" → OpenRouter chat completions
      "gpt-5.4"                 → auto-detect Codex
    """
    model = model or "gpt-5.4"

    if ":" in model and not model.startswith("us."):
        provider_hint, model_name = model.split(":", 1)
    else:
        provider_hint = None
        model_name = model

    if provider_hint == "codex" or (not provider_hint and "gpt" in model_name.lower()):
        return {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": CODEX_BASE_URL,
            "api_key": _resolve_codex_token(),
            "model": model_name,
        }
    elif provider_hint == "anthropic":
        return {
            "provider": "anthropic",
            "api_mode": "anthropic_messages",
            "base_url": os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
            "api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
            "model": model_name,
        }
    elif provider_hint == "bedrock":
        # Bedrock via Anthropic SDK (Claude models only).
        # Auth: IAM role (instance profile) — no API key needed.
        # Model names: claude-sonnet-4.6, claude-haiku-4.5, etc.
        # Region from AWS_REGION env var (default us-west-2).
        region = os.environ.get("AWS_REGION", "us-west-2")
        return {
            "provider": "anthropic",
            "api_mode": "anthropic_messages",
            "base_url": f"https://bedrock-runtime.{region}.amazonaws.com",
            "api_key": "bedrock-iam-role",  # placeholder; AnthropicBedrock uses IAM
            "model": model_name,
            "_bedrock": True,
            "_bedrock_region": region,
        }
    elif provider_hint == "openrouter":
        return {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": os.environ.get("OPENROUTER_API_KEY", ""),
            "model": model_name,
        }
    else:
        # Default: auto-detect based on model name
        if "claude" in model_name.lower():
            return {
                "provider": "anthropic",
                "api_mode": "anthropic_messages",
                "base_url": os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
                "api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
                "model": model_name,
            }
        return {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": CODEX_BASE_URL,
            "api_key": _resolve_codex_token(),
            "model": model_name,
        }


def _ensure_hermes_importable():
    """Add hermes repo to sys.path if needed."""
    repo_str = str(HERMES_REPO)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)


def _secure_hermes_tools(workdir: str):
    """Replace hermes's unrestricted tools with bounded versions.

    Wraps terminal, read_file, write_file, patch, and execute_code
    with path/command guards from agents/tools/__init__.py.
    Agents can only write to their own worktrees/workspace/tmp.
    Agents cannot read token files or credential directories.
    """
    from tools.registry import registry
    from tools import (
        _check_read_path, _check_write_path, _check_bash_command,
        BLOCKED_READ_PATTERNS,
    )
    import json as _json

    log.info("hermes_sdk: securing tools for workdir=%s", workdir)

    def _wrap(tool_name, check_fn):
        """Wrap a registered tool handler with a security check."""
        if tool_name not in registry._tools:
            return
        original = registry._tools[tool_name].handler

        def secured(args, **kwargs):
            err = check_fn(args)
            if err:
                return _json.dumps({"error": err})
            return original(args, **kwargs)

        registry._tools[tool_name].handler = secured
        log.info("hermes_sdk: secured %s", tool_name)

    # Terminal / bash — block token exfil and destructive commands
    _wrap("terminal", lambda a: _check_bash_command(a.get("command", "")))

    # File reads — block token/secret paths
    _wrap("read_file", lambda a: _check_read_path(a.get("path", "")))
    _wrap("search_files", lambda a: _check_read_path(a.get("path", "")))

    # File writes — only allow agent workspace, worktrees, /tmp
    _wrap("write_file", lambda a: _check_write_path(a.get("path", ""), workdir))
    _wrap("patch", lambda a: _check_write_path(
        a.get("file_path", a.get("path", "")), workdir))

    # Code execution — block any code referencing secret paths
    def _check_code(args):
        code = args.get("code", "")
        for pat in BLOCKED_READ_PATTERNS:
            if pat in code:
                return f"Code blocked: references {pat}"
        return None
    _wrap("execute_code", _check_code)


@register("hermes_sdk")
class HermesSDKRuntime(BaseRuntime):
    """Runs hermes-agent's AIAgent with full agentic loop.

    Advantages over the basic openai_sdk runtime:
      - 90-turn agentic loop (vs 25)
      - Parallel tool execution for independent tools
      - Context compression for long sessions
      - Subagent delegation for large tasks
      - Rich callback pipeline for SSE signals
      - Codex auth refresh built-in
    """

    def execute(
        self,
        message: str,
        *,
        workdir: str,
        model: str | None = None,
        system_prompt: str | None = None,
        session_id: str | None = None,
        stream_cb: StreamCallback | None = None,
        timeout: int = 300,
        extra_args: dict | None = None,
    ) -> RuntimeResult:
        _ensure_hermes_importable()
        from run_agent import AIAgent

        cb = stream_cb or StreamCallback()
        extra = extra_args or {}
        start_time = time.time()
        tool_count = 0
        files_written: list[str] = []

        # Resolve provider config from model string
        provider_cfg = _resolve_provider_config(model)
        if not provider_cfg["api_key"]:
            log.error("hermes_sdk: no API key for provider=%s", provider_cfg["provider"])
            return RuntimeResult(
                text="Agent could not authenticate — no API key available.",
                exit_reason="crashed",
                elapsed_seconds=int(time.time() - start_time),
            )

        # Mask key in logs
        key_preview = provider_cfg["api_key"][:8] + "..." if len(provider_cfg["api_key"]) > 8 else "***"
        log.info("hermes_sdk: provider=%s model=%s key=%s",
                 provider_cfg["provider"], provider_cfg["model"], key_preview)

        # ── Callbacks bridge: hermes → aX SSE signals ──
        # Hermes passes (name, preview, args_dict) — accept all three
        def _on_tool_progress(tool_name: str, args_preview: str, args_dict=None):
            nonlocal tool_count
            tool_count += 1
            cb.on_tool_start(tool_name, args_preview)

        def _on_status(status_msg: str):
            if not status_msg:
                return
            lower = status_msg.lower()
            if "think" in lower or "reason" in lower:
                cb.on_status("thinking")
            elif "tool" in lower:
                cb.on_status("tool_call")
            else:
                cb.on_status("processing")

        def _on_step(iteration: int, prev_tools: list[str]):
            cb.on_status("processing")

        def _on_stream_delta(text: str):
            cb.on_text_delta(text)

        # ── Agent profile ──
        # Profiles define tools, access, budget per agent type.
        agent_name = os.environ.get("AX_AGENT_NAME", "")
        try:
            from profiles import get_profile_for_agent, get_disabled_toolsets, \
                get_enabled_toolsets, get_max_iterations
            profile = get_profile_for_agent(agent_name)
            disabled = extra.get("disabled_toolsets", get_disabled_toolsets(profile))
            enabled = extra.get("enabled_toolsets", get_enabled_toolsets(profile))
            max_iters = int(os.environ.get("HERMES_MAX_ITERATIONS",
                                           str(get_max_iterations(profile))))
            log.info("hermes_sdk: profile=%s agent=%s iters=%d",
                     profile.get("name", "?"), agent_name, max_iters)
        except Exception as e:
            log.warning("hermes_sdk: profile load failed (%s), using defaults", e)
            enabled = extra.get("enabled_toolsets")
            disabled = extra.get("disabled_toolsets", [
                "web", "browser", "image_generation", "tts", "vision",
                "cronjob", "rl_training", "homeassistant",
            ])
            max_iters = int(os.environ.get("HERMES_MAX_ITERATIONS", "60"))

        # ── Build the agent ──
        try:
            agent = AIAgent(
                base_url=provider_cfg["base_url"],
                api_key=provider_cfg["api_key"],
                provider=provider_cfg["provider"],
                api_mode=provider_cfg["api_mode"],
                model=provider_cfg["model"],
                max_iterations=max_iters,
                tool_delay=0.5,
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                disabled_toolsets=disabled,
                enabled_toolsets=enabled,
                tool_progress_callback=_on_tool_progress,
                status_callback=_on_status,
                step_callback=_on_step,
                stream_delta_callback=_on_stream_delta,
            )
        except Exception as e:
            log.error("hermes_sdk: failed to create agent: %s", e)
            return RuntimeResult(
                text=f"Agent initialization failed: {e}",
                exit_reason="crashed",
                elapsed_seconds=int(time.time() - start_time),
            )

        # ── Secure tools: wrap hermes tools with path/command guards ──
        # Agents can only read shared repos + own dir, write to worktrees only.
        try:
            _secure_hermes_tools(workdir)
        except Exception as e:
            log.warning("hermes_sdk: tool security setup failed: %s", e)

        # ── Build conversation history ──
        history = list(extra.get("history", []))

        # ── Execute ──
        cb.on_status("accepted")
        try:
            result = agent.run_conversation(
                user_message=message,
                system_message=system_prompt,
                conversation_history=history if history else None,
            )
        except KeyboardInterrupt:
            return RuntimeResult(
                text="Agent interrupted.",
                history=history,
                tool_count=tool_count,
                exit_reason="timeout",
                elapsed_seconds=int(time.time() - start_time),
            )
        except Exception as e:
            log.error("hermes_sdk: agent crashed: %s", e, exc_info=True)
            error_str = str(e).lower()
            if "429" in error_str or "rate" in error_str:
                return RuntimeResult(
                    text="",
                    history=history,
                    tool_count=tool_count,
                    exit_reason="rate_limited",
                    elapsed_seconds=int(time.time() - start_time),
                )
            return RuntimeResult(
                text=f"Agent error: {e}",
                history=history,
                tool_count=tool_count,
                exit_reason="crashed",
                elapsed_seconds=int(time.time() - start_time),
            )

        # ── Extract results ──
        final_text = result.get("final_response", "")
        output_history = result.get("messages", history)
        api_calls = result.get("api_calls", 0)
        total_tokens = result.get("total_tokens", 0)

        if final_text:
            cb.on_text_complete(final_text)

        elapsed = int(time.time() - start_time)
        api_calls = result.get("api_calls", 0)
        exit_reason = "done"
        if result.get("interrupted"):
            exit_reason = "timeout"
        elif not result.get("completed", True):
            # Distinguish iteration limit from actual crash
            if api_calls >= max_iters:
                exit_reason = "iteration_limit"
            else:
                exit_reason = "crashed"

        log.info("hermes_sdk: %s in %ds, %d tools, %d api_calls, %d tokens, %d chars",
                 exit_reason, elapsed, tool_count, api_calls, total_tokens, len(final_text))

        return RuntimeResult(
            text=final_text,
            session_id=None,
            history=output_history,
            tool_count=tool_count,
            files_written=files_written,
            exit_reason=exit_reason,
            elapsed_seconds=elapsed,
        )
