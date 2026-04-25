# Vendored from ax-agents on 2026-04-25 — see ax_cli/runtimes/hermes/README.md
#!/usr/bin/env python3
"""CLI agent v2 for aX — SSE listener with session continuity,
message queuing, and processing signals.

Improvements over v1:
- Session continuity: tracks session IDs per conversation thread, uses --resume
- Message queue: processes mentions in a background worker thread (no dropped SSE)
- Processing signal: fires agent_processing SSE events so the frontend shows status
- Codex support: --runtime codex uses `codex exec` instead of `claude -p`

Usage:
    python3 claude_agent_v2.py                        # Live mode (Claude Code)
    python3 claude_agent_v2.py --runtime codex        # Use Codex CLI
    python3 claude_agent_v2.py --dry-run              # Watch only
    python3 claude_agent_v2.py --agent relay           # Override agent name
"""

import argparse
import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import httpx
except ImportError:
    print("Error: httpx required. pip install httpx")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("claude_agent")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    for p in [Path(".ax/config.toml"), Path.home() / ".ax" / "config.toml"]:
        if p.exists():
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib
            return tomllib.loads(p.read_text())
    return {}


def parse_args():
    parser = argparse.ArgumentParser(description="CLI agent v2 for aX")
    parser.add_argument("--dry-run", action="store_true", help="Watch only")
    parser.add_argument("--agent", type=str, help="Override agent name")
    parser.add_argument("--workdir", type=str, default=None,
                        help="Working directory (default: agents/<agent_name>/)")
    parser.add_argument("--model", type=str, default=None,
                        help="Model to use (default: CLI default)")
    parser.add_argument("--timeout", type=int, default=300,
                        help="Max seconds per invocation")
    parser.add_argument("--update-interval", type=float, default=2.0,
                        help="Seconds between reply edits for streaming")
    parser.add_argument("--allowed-tools", type=str, default=None,
                        help="Comma-separated tools to allow (default: all)")
    parser.add_argument("--system-prompt", type=str, default=None,
                        help="Additional system prompt")
    parser.add_argument("--runtime",
                        choices=["claude", "codex", "claude_cli", "codex_cli", "openai_sdk", "hermes_sdk"],
                        default="claude",
                        help="Runtime plugin: claude/claude_cli (subprocess), "
                             "codex/codex_cli (subprocess), openai_sdk (SDK)")
    parser.add_argument("--disable-codex-mcp", action="store_true",
                        help="Disable inherited Codex MCP servers for listener runs")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Session Store — maps thread (parent_id) to session_id for continuity
# ---------------------------------------------------------------------------

class SessionStore:
    """Thread-safe mapping of conversation threads to CLI session IDs."""

    def __init__(self, max_sessions: int = 100):
        self._store: dict[str, str] = {}  # parent_id -> session_id
        self._lock = threading.Lock()
        self._max = max_sessions

    def get(self, thread_id: str) -> str | None:
        with self._lock:
            return self._store.get(thread_id)

    def set(self, thread_id: str, session_id: str):
        with self._lock:
            self._store[thread_id] = session_id
            # Evict oldest if too many
            if len(self._store) > self._max:
                oldest = next(iter(self._store))
                del self._store[oldest]

    def delete(self, thread_id: str):
        with self._lock:
            self._store.pop(thread_id, None)

    def count(self) -> int:
        with self._lock:
            return len(self._store)


class HistoryStore:
    """Thread-scoped working memory for SDK-style runtimes."""

    def __init__(self, max_threads: int = 100, max_messages: int = 12):
        self._store: dict[str, list[dict]] = {}
        self._lock = threading.Lock()
        self._max_threads = max_threads
        self._max_messages = max_messages

    def get(self, thread_id: str) -> list[dict]:
        with self._lock:
            history = self._store.get(thread_id, [])
            return [dict(item) for item in history]

    def set(self, thread_id: str, history: list[dict]):
        trimmed = [dict(item) for item in history[-self._max_messages:]]
        with self._lock:
            self._store[thread_id] = trimmed
            if len(self._store) > self._max_threads:
                oldest = next(iter(self._store))
                del self._store[oldest]

    def delete(self, thread_id: str):
        with self._lock:
            self._store.pop(thread_id, None)


# ---------------------------------------------------------------------------
# API Client
# ---------------------------------------------------------------------------

class AxAPI:
    """Thin wrapper for the aX REST API.

    Uses the ax-cli TokenExchanger for seamless PAT→JWT exchange with
    auto-refresh and retry on 401. Falls back to raw token if the CLI
    auth module isn't available (e.g. on prod without auth-spec-001).
    """

    def __init__(self, base_url: str, token: str, agent_name: str,
                 agent_id: str, internal_api_key: str = "", space_id: str = ""):
        self.base_url = base_url.rstrip("/")
        self._raw_token = token
        self.token = token  # may be replaced by JWT below
        self.agent_name = agent_name
        self.agent_id = agent_id
        self.space_id = space_id
        self.internal_api_key = internal_api_key
        self._processing_signals_enabled = bool(internal_api_key)
        self._exchanger = None

        # Try to use the CLI's TokenExchanger for seamless auth
        if token.startswith("axp_"):
            try:
                from ax_cli.token_cache import TokenExchanger
                exchanger = TokenExchanger(base_url, token)
                # Warm the cache — exchange once at startup.
                # Sentinels always use agent_access when they have an agent_id.
                token_class = "agent_access" if agent_id else "user_access"
                jwt = exchanger.get_token(
                    token_class,
                    agent_id=agent_id or None,
                    scope="messages tasks context agents spaces search",
                )
                # Validate the JWT is real (not empty or malformed)
                if jwt and jwt.startswith("eyJ"):
                    self._exchanger = exchanger
                    self.token = jwt
                    log.info("PAT→JWT exchange active (auto-refresh enabled)")
                else:
                    log.warning("Exchange returned invalid JWT — using PAT directly")
            except ImportError:
                log.debug("ax_cli.token_cache not available — using PAT directly")
            except Exception as e:
                log.warning("Token exchange init failed (%s) — using PAT directly", e)

        # Build httpx client with 401 retry if exchanger is available
        inner = httpx.Client(timeout=30.0)
        if self._exchanger:
            try:
                from ax_cli.client import _RetryOnAuthClient
                self._client = _RetryOnAuthClient(
                    inner, lambda: self._get_fresh_jwt(force=True)
                )
                log.debug("401 auto-retry enabled")
            except ImportError:
                self._client = inner
        else:
            self._client = inner

    def _get_fresh_jwt(self, force: bool = False) -> str:
        """Get a JWT from the exchanger, with caching."""
        # Sentinels always use agent_access when agent_id is set — works for
        # both axp_a_ (agent-bound) and axp_u_ (user PATs used by sentinels).
        if self.agent_id:
            jwt = self._exchanger.get_token(
                "agent_access", agent_id=self.agent_id,
                scope="messages tasks context agents spaces search",
                force_refresh=force,
            )
        else:
            jwt = self._exchanger.get_token(
                "user_access",
                scope="messages tasks context agents spaces search",
                force_refresh=force,
            )
        self.token = jwt  # keep self.token current for SSE etc
        return jwt

    def _headers(self) -> dict:
        # If exchanger is active, get a fresh JWT for each call
        token = self._get_fresh_jwt() if self._exchanger else self.token
        h = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        # No X-Agent-Id with exchange auth (AUTH-SPEC-001 §13)
        if not self._exchanger and self.agent_id:
            h["X-Agent-Id"] = self.agent_id
        return h

    def send_message(
        self,
        space_id: str,
        content: str,
        parent_id: str | None = None,
        *,
        message_type: str = "text",
        metadata: dict | None = None,
    ) -> dict | None:
        body = {
            "content": content,
            "space_id": space_id,
            "channel": "main",
            "message_type": message_type,
        }
        if parent_id:
            body["parent_id"] = parent_id
        if metadata is not None:
            body["metadata"] = metadata
        try:
            resp = self._client.post(
                f"{self.base_url}/api/v1/messages",
                json=body,
                headers=self._headers(),
            )
            if resp.status_code == 200 and resp.text:
                payload = resp.json()
                if isinstance(payload, dict):
                    if isinstance(payload.get("message"), dict):
                        return payload["message"]
                    if payload.get("id"):
                        return payload
            else:
                log.warning(f"send_message: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            log.error(f"send_message error: {e}")
        return None

    def edit_message(self, message_id: str, content: str, metadata: dict | None = None) -> bool:
        try:
            body = {"content": content}
            if metadata is not None:
                body["metadata"] = metadata
            resp = self._client.patch(
                f"{self.base_url}/api/v1/messages/{message_id}",
                json=body,
                headers=self._headers(),
            )
            return resp.status_code == 200
        except Exception as e:
            log.error(f"edit_message error: {e}")
            return False

    def request_summary(self, message_id: str):
        """Clear stale summary then request fresh one. Non-blocking, best-effort.

        The background summarizer skips messages that already have ai_summary,
        so we need to clear it first (the initial '...' content may have gotten
        a garbage summary). We clear via direct DB update through an internal
        endpoint, then trigger re-summarization.
        """
        try:
            # Clear stale summary by editing the message (the PATCH handler
            # doesn't touch ai_summary, but we can use the internal status
            # endpoint to signal the backend to re-summarize)
            self._client.post(
                f"{self.base_url}/api/v1/messages/{message_id}/summarize",
                headers={
                    **self._headers(),
                    "X-Force-Resummarize": "true",
                },
            )
        except Exception as e:
            log.debug(f"request_summary error (non-fatal): {e}")

    def signal_processing(
        self,
        message_id: str,
        status: str = "started",
        space_id: str = "",
        *,
        tool_name: str | None = None,
    ):
        """Fire an agent_processing event so the frontend shows a status indicator."""
        if not self._processing_signals_enabled or not self.internal_api_key:
            return
        try:
            body = {
                "agent_name": self.agent_name,
                "agent_id": self.agent_id,
                "status": status,
                "message_id": message_id,
                "space_id": space_id,
            }
            if tool_name:
                body["tool_name"] = tool_name
            resp = self._client.post(
                f"{self.base_url}/auth/internal/agent-status",
                json=body,
                headers={
                    "X-API-Key": self.internal_api_key,
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code in (401, 403):
                self._processing_signals_enabled = False
                log.warning(
                    "Disabling agent_processing signals after %s from %s/auth/internal/agent-status; "
                    "check INTERNAL_DISPATCH_API_KEY or AGENT_RUNNER_API_KEY",
                    resp.status_code,
                    self.base_url,
                )
        except Exception as e:
            log.debug(f"signal_processing error (non-fatal): {e}")

    def connect_sse(self) -> httpx.Response:
        # Get a fresh JWT for SSE if exchange is available, otherwise use raw token
        try:
            sse_token = self._get_fresh_jwt() if self._exchanger else self.token
        except Exception:
            sse_token = self._raw_token  # Fall back to PAT (works on prod)
        sse_client = httpx.Client(timeout=httpx.Timeout(
            connect=10.0, read=90.0, write=10.0, pool=10.0,
        ))
        return sse_client.stream(
            "GET",
            f"{self.base_url}/api/v1/sse/messages",
            params={"token": sse_token, "space_id": self.space_id},
        )

    def close(self):
        self._client.close()


# ---------------------------------------------------------------------------
# SSE Parser
# ---------------------------------------------------------------------------

def iter_sse(response: httpx.Response):
    event_type = None
    data_lines = []

    for line in response.iter_lines():
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif line == "":
            if event_type and data_lines:
                raw = "\n".join(data_lines)
                try:
                    parsed = json.loads(raw) if raw.startswith("{") else raw
                except json.JSONDecodeError:
                    parsed = raw
                yield event_type, parsed
            event_type = None
            data_lines = []


# ---------------------------------------------------------------------------
# CLI Runners
# ---------------------------------------------------------------------------

def _build_claude_cmd(message: str, workdir: str, args,
                      session_id: str | None = None) -> list[str]:
    cmd = [
        "claude", "-p",
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
        "--add-dir", "/home/ax-agent/shared/repos",
    ]
    if session_id:
        cmd.extend(["--resume", session_id])
    if args.model:
        cmd.extend(["--model", args.model])
    if args.allowed_tools:
        cmd.extend(["--allowedTools", args.allowed_tools])
    if args.system_prompt:
        cmd.extend(["--append-system-prompt", args.system_prompt])
    return cmd


def _build_codex_cmd(message: str, workdir: str, args,
                     session_id: str | None = None) -> list[str]:
    # Per-agent sandbox override. Default (unset) preserves legacy behavior
    # (full bypass) for agents that legitimately need cross-repo access like
    # sentinels. Set CODEX_SANDBOX=workspace-write in the agent launcher to
    # opt into Codex's workspace-write sandbox: read anywhere, write only
    # under workdir, autonomous execution without approvals.
    sandbox_mode = os.environ.get("CODEX_SANDBOX", "").strip()
    if sandbox_mode == "workspace-write":
        sandbox_flags = [
            "--sandbox", "workspace-write",
            "--ask-for-approval", "never",
        ]
    else:
        sandbox_flags = ["--dangerously-bypass-approvals-and-sandbox"]

    cmd = [
        "codex", "exec",
        "--json",
        *sandbox_flags,
        "--skip-git-repo-check",
        "-C", workdir,
    ]
    if session_id:
        # Codex uses `codex exec resume --last` or by session ID
        cmd = ["codex", "exec", "resume", session_id, "--json",
               *sandbox_flags]
    if args.disable_codex_mcp:
        cmd.extend(["-c", "mcp_servers.ax-platform.enabled=false"])
    if args.model:
        cmd.extend(["-m", args.model])
    return cmd


def _summarize_codex_command(command: str) -> str:
    short = " ".join(command.split())
    if len(short) > 90:
        short = short[:87] + "..."

    lowered = short.lower()
    if "apply_patch" in lowered:
        return "Applying patch..."
    if any(token in lowered for token in (" rg ", "grep ", "find ", "fd ", "glob ")):
        return "Searching codebase..."
    if any(token in lowered for token in ("sed -n", "cat ", "head ", "tail ", "ls ", "pwd", "git status", "git diff")):
        return "Reading files..."
    if any(token in lowered for token in ("pytest", "npm test", "pnpm test", "uv run", "cargo test")):
        return "Running tests..."
    return f"Running: {short}..."


def _parse_claude_stream(proc) -> tuple[str, str | None]:
    """Parse Claude Code stream-json output. Returns (text, session_id)."""
    accumulated = ""
    session_id = None

    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = event.get("type", "")

        if etype == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    accumulated = block["text"]

        elif etype == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                accumulated += delta.get("text", "")

        elif etype == "result":
            result_text = event.get("result", "")
            if result_text:
                accumulated = result_text
            sid = event.get("session_id", "")
            if sid:
                session_id = sid

    return accumulated, session_id


def _parse_codex_stream(proc) -> tuple[str, str | None]:
    """Parse Codex JSONL output. Returns (text, session_id)."""
    accumulated = ""
    session_id = None

    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = event.get("type", "")

        # Codex emits message events with content
        if etype == "message" and event.get("role") == "assistant":
            for block in event.get("content", []):
                if block.get("type") == "output_text":
                    accumulated += block.get("text", "")

        elif etype == "response.completed":
            # Final response
            pass

        # Capture session
        sid = event.get("id", "")
        if sid and sid.startswith("sess_"):
            session_id = sid

    return accumulated, session_id


# ---------------------------------------------------------------------------
# Runtime plugin bridge — connects agnostic runtimes to aX message plumbing
# ---------------------------------------------------------------------------

def _run_via_runtime_plugin(
    runtime_name: str,
    message: str,
    workdir: str,
    args,
    api: AxAPI,
    parent_id: str,
    space_id: str,
    sessions: SessionStore,
    histories: HistoryStore,
    thread_id: str | None = None,
) -> str:
    """Execute via a runtime plugin, bridging its StreamCallback to aX messages."""
    import sys
    # Ensure the agents directory is on sys.path so runtimes/ and tools/ can import
    agents_dir = str(Path(__file__).parent)
    if agents_dir not in sys.path:
        sys.path.insert(0, agents_dir)

    from runtimes import get_runtime, StreamCallback

    runtime = get_runtime(runtime_name)
    history_thread_id = thread_id or parent_id or "default"
    existing_session = sessions.get(history_thread_id)

    log.info(f"Runtime: {runtime_name} in {workdir}"
             + (f" (session {existing_session[:12]})" if existing_session else " (new)")
             + f" reply={parent_id[:12]} history={history_thread_id[:24]}")

    stream_edits = os.environ.get("AX_SENTINEL_STREAM_EDITS", "0").lower() in {
        "1", "true", "yes",
    }

    # Signal: processing started (SSE-only, may fail on prod without internal API key)
    api.signal_processing(parent_id, "started", space_id=space_id)
    api.signal_processing(parent_id, "thinking", space_id=space_id)

    # ── Immediate progress reply ──
    # Create a non-final reply right away so the user sees the agent is working.
    # The stream updater edits this message in-place with tool progress,
    # and the final result overwrites it when done.
    reply_id = None
    progress_metadata = {
        "top_level_ingress": False,
        "routing": {
            "mode": "direct_mention",
            "source": "sse_agent",
        },
        "streaming_reply": {
            "enabled": True,
            "final": False,
            "runtime": runtime_name,
        },
    }
    progress_msg = api.send_message(
        space_id=space_id,
        content=f"Working\u2026",
        parent_id=parent_id,
        message_type="reply",
        metadata=progress_metadata,
    )
    if progress_msg:
        reply_id = progress_msg.get("id", "")
        log.info(f"Progress reply created: {reply_id[:12]}")

    accumulated_text = ""
    tool_count = 0
    edit_lock = threading.Lock()
    finished = threading.Event()
    last_streamed_text = ""
    stream_interval = max(0.5, float(getattr(args, "update_interval", 2.0)))

    def _reply_metadata(final: bool = False) -> dict:
        return {
            "top_level_ingress": False,
            "routing": {
                "mode": "direct_mention",
                "source": "sse_agent",
            },
            "streaming_reply": {
                "enabled": True,
                "final": final,
                "runtime": runtime_name,
            },
        }

    def _display_text(text: str) -> str:
        if len(text) > 15000:
            return "...(truncated)...\n\n" + text[-15000:]
        return text

    def _create_reply(content: str) -> str | None:
        nonlocal reply_id
        if reply_id or not content.strip():
            return reply_id
        msg = api.send_message(
            space_id=space_id,
            content=content,
            parent_id=parent_id,
            message_type="reply",
            metadata=_reply_metadata(final=False),
        )
        if msg:
            reply_id = msg.get("id", "")
            log.info(f"Reply created for streaming: {reply_id[:12]}")
        return reply_id

    last_progress_update = ""

    def _stream_updater():
        nonlocal last_streamed_text, last_progress_update
        while not finished.wait(timeout=stream_interval):
            with edit_lock:
                current_text = accumulated_text

            # If we have real text from the LLM, stream that
            if current_text.strip() and stream_edits:
                if current_text != last_streamed_text:
                    display = _display_text(current_text)
                    if reply_id is None:
                        if _create_reply(display):
                            last_streamed_text = current_text
                        continue
                    if api.edit_message(reply_id, display):
                        last_streamed_text = current_text
                continue

            # No LLM text yet — show tool progress on the progress reply
            if reply_id and tool_activity:
                progress = _progress_text()
                if progress != last_progress_update:
                    if api.edit_message(reply_id, progress):
                        last_progress_update = progress

    updater = threading.Thread(target=_stream_updater, daemon=True)
    updater.start()

    # StreamCallback — accumulates text, signals status via SSE, and optionally
    # streams in-place message edits for runtimes that share this callback API.
    # Also tracks tool activity for the progress reply.
    tool_activity: list[str] = []  # recent tool names for progress display

    def _progress_text() -> str:
        """Build progress display text showing what the agent is doing."""
        lines = [f"Working\u2026 ({tool_count} tool{'s' if tool_count != 1 else ''})"]
        # Show last 3 tool activities
        for activity in tool_activity[-3:]:
            lines.append(f"  \u203a {activity}")
        return "\n".join(lines)

    class AxStreamCallback(StreamCallback):
        def on_text_delta(self, text: str):
            nonlocal accumulated_text
            with edit_lock:
                accumulated_text += text

        def on_text_complete(self, text: str):
            nonlocal accumulated_text
            with edit_lock:
                accumulated_text = text

        def on_tool_start(self, tool_name: str, summary: str):
            nonlocal tool_count
            tool_count += 1
            tool_activity.append(summary or tool_name)
            # Update progress reply with tool activity
            if reply_id and not accumulated_text.strip():
                api.edit_message(reply_id, _progress_text())
            api.signal_processing(
                parent_id,
                "tool_call",
                space_id=space_id,
                tool_name=tool_name,
            )

        def on_tool_end(self, tool_name: str, summary: str):
            api.signal_processing(
                parent_id,
                "processing",
                space_id=space_id,
                tool_name=tool_name,
            )

        def on_status(self, status: str):
            api.signal_processing(parent_id, status, space_id=space_id)

    # Load system prompt from agent instruction file
    # AGENTS.md is the standard for non-Claude runtimes; CLAUDE.md is fallback
    agents_md = Path(workdir) / "AGENTS.md"
    claude_md = Path(workdir) / "CLAUDE.md"
    if agents_md.exists():
        system_prompt = agents_md.read_text()
    elif claude_md.exists():
        system_prompt = claude_md.read_text()
    else:
        system_prompt = None

    # Build extra args
    extra = {
        "add_dir": "/home/ax-agent/shared/repos",
        "history": histories.get(history_thread_id),
    }
    if hasattr(args, "disable_codex_mcp") and args.disable_codex_mcp:
        extra["disable_mcp"] = True
    if hasattr(args, "allowed_tools") and args.allowed_tools:
        extra["allowed_tools"] = args.allowed_tools

    def _execute_runtime(session_id: str | None):
        return runtime.execute(
            message,
            workdir=workdir,
            model=args.model,
            system_prompt=system_prompt,
            session_id=session_id,
            stream_cb=AxStreamCallback(),
            timeout=args.timeout,
            extra_args=extra,
        )

    def _looks_like_claude_auth_expiry(result) -> bool:
        text = (result.text or "").lower()
        if "oauth token has expired" in text:
            return True
        if "failed to authenticate" in text and "authentication_error" in text:
            return True
        return False

    def _refresh_claude_auth() -> bool:
        refresh_script = Path("/home/ax-agent/fetch-claude-token.sh")
        if not refresh_script.exists():
            log.warning("Claude refresh helper missing: %s", refresh_script)
            return False
        try:
            proc = subprocess.run(
                [str(refresh_script)],
                cwd="/home/ax-agent",
                text=True,
                capture_output=True,
                timeout=90,
                check=False,
            )
        except Exception as exc:
            log.warning("Claude auth refresh failed to start: %s", exc)
            return False

        if proc.returncode == 0:
            log.info("Claude auth refresh helper completed successfully")
            return True

        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = stderr or stdout or f"exit {proc.returncode}"
        log.warning("Claude auth refresh helper failed: %s", detail[:300])
        return False

    result = _execute_runtime(existing_session)

    # Claude auth expiry should not permanently poison the listener. If a
    # resumed session hits auth failure, clear continuity, refresh auth, and
    # retry once with a clean Claude session before surfacing the blocker.
    if runtime_name == "claude_cli" and _looks_like_claude_auth_expiry(result):
        log.warning("Claude auth expired for %s; retrying with fresh session", workdir)
        sessions.delete(history_thread_id)
        histories.delete(history_thread_id)
        with edit_lock:
            accumulated_text = ""
        _refresh_claude_auth()
        result = _execute_runtime(None)

    finished.set()

    # Save session
    if result.session_id:
        sessions.set(history_thread_id, result.session_id)
        log.info(f"Session saved: {result.session_id[:12]}")
    if result.history is not None:
        histories.set(history_thread_id, result.history)

    # Build final content
    final = result.text
    if result.files_written:
        names = [p.split("/")[-1] for p in result.files_written]
        final += "\n\n📄 Wrote: " + ", ".join(names)

    if result.exit_reason == "rate_limited":
        # Rate limited — don't post ANYTHING to chat. Silent backoff.
        log.warning(f"Rate limited — suppressing chat message, cooling down 60s")
        api.signal_processing(parent_id, "completed", space_id=space_id)
        time.sleep(60)  # Cool down before processing next message
        return ""

    if result.exit_reason == "crashed":
        final += f"\n\n---\n⚠️ Agent ended unexpectedly ({result.elapsed_seconds}s)."
    elif result.exit_reason == "timeout":
        final += f"\n\n---\n⏱️ Timed out ({result.elapsed_seconds}s)."
    elif result.exit_reason == "iteration_limit":
        final += (f"\n\n---\n🔄 Reached iteration limit "
                  f"({result.tool_count} tools, {result.elapsed_seconds}s). "
                  f"Reply to continue where I left off.")

    if not final:
        final = f"Completed ({result.elapsed_seconds}s) — no text output."

    # Final message update
    if reply_id:
        api.edit_message(reply_id, final, metadata=_reply_metadata(final=True))
    else:
        api.send_message(
            space_id=space_id,
            content=final,
            parent_id=parent_id,
            message_type="reply",
            metadata=_reply_metadata(final=True) if stream_edits else {
                "top_level_ingress": False,
                "routing": {"mode": "direct_mention", "source": "sse_agent"},
            },
        )

    api.signal_processing(parent_id, "completed", space_id=space_id)

    if reply_id and len(final) > 50:
        api.request_summary(reply_id)

    log.info(f"Runtime {runtime_name}: {result.exit_reason} "
             f"({len(final)} chars, {result.tool_count} tools, {result.elapsed_seconds}s)")
    return final


def run_cli(message: str, workdir: str, args, api: AxAPI,
            parent_id: str, space_id: str,
            sessions: SessionStore,
            histories: HistoryStore,
            thread_id: str | None = None) -> str:
    """Run an agent runtime and stream output back to aX.

    Delegates to the configured runtime plugin. Runtimes are agnostic —
    they produce text/tool events via StreamCallback; this function handles
    the aX-specific message create/edit/signal logic.
    """
    # ── Plugin runtime dispatch ─────────────────────────────────────
    # Normalize legacy names to plugin names
    runtime_name = args.runtime
    if runtime_name == "claude":
        runtime_name = "claude_cli"
    elif runtime_name == "codex":
        runtime_name = "codex_cli"

    # Check if this is a plugin runtime (not the legacy subprocess path)
    if runtime_name in ("claude_cli", "codex_cli", "openai_sdk", "hermes_sdk"):
        return _run_via_runtime_plugin(
            runtime_name, message, workdir, args, api,
            parent_id, space_id, sessions, histories, thread_id=thread_id,
        )

    # ── Legacy subprocess path (fallback, should not be reached) ────
    history_thread_id = thread_id or parent_id or "default"
    existing_session = sessions.get(history_thread_id)

    if args.runtime == "codex":
        cmd = _build_codex_cmd(message, workdir, args, existing_session)
    else:
        cmd = _build_claude_cmd(message, workdir, args, existing_session)

    log.info(f"Running {args.runtime} in {workdir}"
             + (f" (resuming session {existing_session[:12]})" if existing_session else " (new session)"))

    # Signal: we're processing
    api.signal_processing(parent_id, "started", space_id=space_id)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=workdir,
        text=True,
    )

    proc.stdin.write(message)
    proc.stdin.close()

    # Streaming state
    accumulated_text = ""
    update_interval = args.update_interval
    edit_lock = threading.Lock()
    finished = threading.Event()

    # Defer reply creation until real content arrives.
    # Creating a placeholder message (e.g. "thinking...") triggers the backend's
    # routing pipeline, which dispatches it to the concierge — wasting tokens.
    # The signal_processing() call above fires an SSE-only event (no DB message,
    # no dispatch) so the frontend can still show a status indicator.
    reply_id = None
    api.signal_processing(parent_id, "thinking", space_id=space_id)

    def _create_reply_if_needed(content: str) -> str | None:
        """Lazily create the reply message on first real content. Returns reply_id."""
        nonlocal reply_id
        if reply_id is not None:
            return reply_id
        msg = api.send_message(
            space_id=space_id,
            content=content,
            parent_id=parent_id,
        )
        if msg:
            reply_id = msg.get("id", "")
            log.info(f"Reply created on first content: {reply_id[:12]}")
        return reply_id

    def periodic_updater():
        """Updates the reply with either tool status or streaming text."""
        last_sent = ""
        while not finished.wait(timeout=update_interval):
            with edit_lock:
                current_text = accumulated_text
                current_tool = last_tool_status

            # If we have real text, show that
            if current_text and current_text != last_sent:
                display = current_text
                if len(current_text) > 15000:
                    display = "...(truncated)...\n\n" + current_text[-15000:]
                _create_reply_if_needed(display)
                if reply_id:
                    api.edit_message(reply_id, display)
                last_sent = current_text

            # If no text yet but tools are running, show tool activity
            elif not current_text and current_tool:
                elapsed = int(time.time() - start_time)
                status_display = f"▸ *{current_tool}*\n_{tool_count} steps · {elapsed}s_"
                if status_display != last_sent:
                    _create_reply_if_needed(status_display)
                    if reply_id:
                        api.edit_message(reply_id, status_display)
                    last_sent = status_display

    updater = threading.Thread(target=periodic_updater, daemon=True)
    updater.start()

    # Track tool use for status updates
    last_tool_status = ""
    tool_count = 0
    files_written = []
    start_time = time.time()
    last_activity_time = time.time()  # Tracks last output/tool event
    exit_reason = "done"  # done | crashed | timeout

    # Activity-based timeout watchdog.
    # No fixed wall-clock timeout. Only kills if output goes silent.
    SILENCE_KILL_SECS = max(30, args.timeout)
    SILENCE_WARN_SECS = max(10, min(120, SILENCE_KILL_SECS // 2))
    silence_warned = False

    def timeout_watchdog():
        nonlocal exit_reason, silence_warned
        while not finished.wait(timeout=10.0):
            silence = time.time() - last_activity_time
            if silence > SILENCE_KILL_SECS:
                exit_reason = "timeout"
                elapsed = int(time.time() - start_time)
                log.warning(f"No activity for {int(silence)}s — killing CLI (total {elapsed}s)")
                proc.kill()
                return
            elif silence > SILENCE_WARN_SECS and not silence_warned:
                silence_warned = True
                log.warning(f"No activity for {int(silence)}s — agent may be hung")

    watchdog = threading.Thread(target=timeout_watchdog, daemon=True)
    watchdog.start()

    # Parse the stream — update accumulated_text in real time for the updater thread
    new_session_id = None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")
            last_activity_time = time.time()  # Any output = agent is alive
            silence_warned = False  # Reset warning on activity

            if args.runtime == "codex":
                if etype == "thread.started":
                    tid = event.get("thread_id", "")
                    if tid:
                        new_session_id = tid

                elif etype == "item.started":
                    item = event.get("item", {}) or {}
                    if item.get("type") == "command_execution":
                        tool_count += 1
                        last_tool_status = _summarize_codex_command(item.get("command", ""))
                        elapsed = int(time.time() - start_time)
                        if reply_id and not accumulated_text:
                            status_msg = f"▸ {last_tool_status}\n_{tool_count} tools used · {elapsed}s_"
                            api.edit_message(reply_id, status_msg)
                        api.signal_processing(
                            parent_id,
                            "tool_call",
                            space_id=space_id,
                            tool_name=last_tool_status,
                        )

                elif etype == "item.completed":
                    item = event.get("item", {}) or {}
                    item_type = item.get("type", "")
                    if item_type == "agent_message":
                        text = item.get("text", "")
                        if text:
                            with edit_lock:
                                accumulated_text = text
                    elif item_type == "command_execution":
                        command = item.get("command", "")
                        if command:
                            last_tool_status = _summarize_codex_command(command)

            else:
                if etype == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            with edit_lock:
                                accumulated_text = block["text"]
                        # Detect tool use for status updates
                        if block.get("type") == "tool_use":
                            tool_name = block.get("name", "")
                            tool_input = block.get("input", {})
                            tool_count += 1

                            # Build human-readable status
                            if tool_name in ("Read", "read"):
                                path = tool_input.get("file_path", "")
                                short = path.split("/")[-1] if "/" in path else path
                                last_tool_status = f"Reading {short}..."
                            elif tool_name in ("Write", "write"):
                                path = tool_input.get("file_path", "")
                                short = path.split("/")[-1] if "/" in path else path
                                last_tool_status = f"Writing {short}..."
                                files_written.append(path)
                            elif tool_name in ("Edit", "edit"):
                                path = tool_input.get("file_path", "")
                                short = path.split("/")[-1] if "/" in path else path
                                last_tool_status = f"Editing {short}..."
                            elif tool_name in ("Bash", "bash"):
                                cmd = str(tool_input.get("command", ""))[:60]
                                last_tool_status = f"Running: {cmd}..."
                            elif tool_name in ("Grep", "grep"):
                                pattern = tool_input.get("pattern", "")
                                last_tool_status = f"Searching: {pattern}..."
                            elif tool_name in ("Glob", "glob"):
                                pattern = tool_input.get("pattern", "")
                                last_tool_status = f"Finding files: {pattern}..."
                            else:
                                last_tool_status = f"Using {tool_name}..."

                            # Update reply with tool status if no text yet
                            elapsed = int(time.time() - start_time)
                            if reply_id and not accumulated_text:
                                status_msg = f"▸ {last_tool_status}\n_{tool_count} tools used · {elapsed}s_"
                                api.edit_message(reply_id, status_msg)

                            # Fire tool_call processing event
                            api.signal_processing(
                                parent_id,
                                "tool_call",
                                space_id=space_id,
                                tool_name=tool_name,
                            )

                elif etype == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        with edit_lock:
                            accumulated_text += delta.get("text", "")

                elif etype == "result":
                    result_text = event.get("result", "")
                    if result_text:
                        with edit_lock:
                            accumulated_text = result_text
                    sid = event.get("session_id", "")
                    if sid:
                        new_session_id = sid

    except Exception as e:
        log.error(f"Error reading output: {e}")
        exit_reason = "crashed"
    finally:
        finished.set()

    # Wait for process
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    # Detect crash from return code
    stderr = proc.stderr.read()
    if proc.returncode != 0 and exit_reason == "done":
        exit_reason = "crashed"
    if proc.returncode != 0 and not accumulated_text:
        log.error(f"CLI failed (exit {proc.returncode}): {stderr[:500]}")

    # Save session for thread continuity (even on crash — partial session is fine)
    if new_session_id:
        sessions.set(history_thread_id, new_session_id)
        log.info(f"Session saved: {new_session_id[:12]} for thread {history_thread_id[:12]}")

    # Build final message content based on exit reason
    elapsed = int(time.time() - start_time)

    # Append file artifacts if any were written
    artifacts_line = ""
    if files_written:
        artifact_names = [p.split("/")[-1] for p in files_written]
        artifacts_line = "\n\n📄 Wrote: " + ", ".join(artifact_names)

    if exit_reason == "crashed":
        suffix = f"\n\n---\n⚠️ Agent process ended unexpectedly ({elapsed}s)."
        if accumulated_text:
            final_content = accumulated_text + artifacts_line + suffix
        else:
            final_content = f"Hit an error processing that.{suffix}"
        log.warning(f"CRASHED after {elapsed}s (exit {proc.returncode})")

    elif exit_reason == "timeout":
        suffix = f"\n\n---\n⏱️ Reached time limit ({args.timeout}s). Partial results above."
        if accumulated_text:
            final_content = accumulated_text + artifacts_line + suffix
        else:
            final_content = f"Timed out after {args.timeout}s with no output."
        log.warning(f"TIMEOUT after {elapsed}s")

    else:  # done
        if accumulated_text:
            final_content = accumulated_text + artifacts_line
        elif files_written:
            final_content = f"Done ({elapsed}s).{artifacts_line}"
        else:
            final_content = f"Completed ({elapsed}s) — no text output."

    # Final update to reply
    if reply_id:
        api.edit_message(reply_id, final_content)
    else:
        api.send_message(space_id=space_id, content=final_content,
                         parent_id=parent_id)

    # Signal: done (with reason)
    api.signal_processing(parent_id, "completed", space_id=space_id)

    # Trigger re-summarization now that the message has real content
    # (the initial "..." placeholder may have gotten a stale summary)
    if reply_id and len(final_content) > 50:
        api.request_summary(reply_id)

    log.info(f"Response {exit_reason} ({len(final_content)} chars, {tool_count} tools, {elapsed}s)")
    return final_content


# ---------------------------------------------------------------------------
# Mention detection
# ---------------------------------------------------------------------------

def get_author_name(event_data: dict) -> str:
    author = event_data.get("author", "")
    if isinstance(author, dict):
        return author.get("name", author.get("username", ""))
    return str(author)


def get_author_id(event_data: dict) -> str:
    author = event_data.get("author", "")
    if isinstance(author, dict):
        return str(author.get("id", "") or "")
    return str(event_data.get("agent_id", "") or "")


def resolve_history_thread_id(
    event_data: dict,
    *,
    agent_name: str,
    space_id: str,
    author: str = "",
) -> str:
    """Choose runtime-memory scope independently from reply parenting.

    Replies should attach to the inbound message id, but long-running listener
    agents need memory continuity across top-level mentions. Default to one
    runtime history per agent+space. Set AX_SENTINEL_HISTORY_SCOPE=conversation
    to isolate memory per message thread.
    """
    scope = os.environ.get("AX_SENTINEL_HISTORY_SCOPE", "space").strip().lower()
    msg_id = str(event_data.get("id") or "")
    parent_id = str(
        event_data.get("parent_id")
        or event_data.get("parentId")
        or event_data.get("thread_id")
        or ""
    )
    conversation_id = str(event_data.get("conversation_id") or "")

    if scope in {"message", "per_message"}:
        return msg_id or "default"
    if scope in {"conversation", "thread", "per_thread"}:
        return parent_id or conversation_id or msg_id or "default"
    if scope in {"author", "user"} and author:
        return f"space:{space_id}:agent:{agent_name}:author:{author}"

    return f"space:{space_id}:agent:{agent_name}"


def is_mentioned(event_data: dict, agent_name: str) -> bool:
    # Only respond to EXPLICIT @mentions typed in the message content.
    # Ignore router-inferred mentions (route_inferred=True in metadata) —
    # those cause cascade loops where every message triggers all agents.
    content = event_data.get("content", "")
    if f"@{agent_name.lower()}" in content.lower():
        return True
    # Check the mentions array, but only if the mention was NOT router-inferred
    metadata = event_data.get("metadata", {}) or {}
    if metadata.get("route_inferred") or metadata.get("router_inferred"):
        return False
    mentions = event_data.get("mentions", [])
    if agent_name.lower() in [m.lower() for m in mentions]:
        return True
    return False


def strip_mention(content: str, agent_name: str) -> str:
    import re
    stripped = re.sub(rf"@{re.escape(agent_name)}\b", "", content,
                      flags=re.IGNORECASE)
    return stripped.strip()


def _is_ax_noise(event_data: dict) -> bool:
    """Detect aX system noise that should never trigger a response."""
    content = event_data.get("content", "")
    author = get_author_name(event_data)

    # "aX chose not to reply" events
    if "chose not to reply" in content:
        return True
    # Tool result cards / "Request processed"
    if content.strip() in ("Request processed", ""):
        return True

    # aX forwarding/relaying — concierge rephrases user messages and routes them.
    # These are duplicates of mentions we've already received directly from users.
    # Pattern: aX says "@user is asking: ..." or "@user says ..." or "is currently executing"
    if author.lower() == "ax":
        # aX relay patterns — concierge rephrasing user messages
        lowered = content.lower()
        relay_patterns = [
            " is asking:", " is asking ", " says ", " says:", " wants ",
            " is requesting", " is inquiring", " is currently ",
            "request processed", " has requested",
        ]
        if any(pat in lowered for pat in relay_patterns):
            return True
        # aX acknowledgment echoes — concierge confirms agent progress updates.
        # These cause cascade loops: agent sends progress → aX acks with @agent → agent wakes.
        ack_patterns = [
            "acknowledged", "got it.", "noted.", "roger", "status recorded",
            "storing the", "clear blocker", "options:",
        ]
        if any(pat in lowered for pat in ack_patterns):
            return True

    # Very short aX routing confirmations (under 20 chars with no real question)
    metadata = event_data.get("metadata", {}) or {}
    if isinstance(metadata, dict):
        ui = metadata.get("ui", {}) or {}
        # Messages with widget/card payloads are tool results, not questions
        if ui.get("widget") or ui.get("cards"):
            return True
        # Messages with routing context are aX relaying, not asking
        routing = metadata.get("routing", {}) or {}
        if routing.get("routed_by_ax") or routing.get("mode") == "ax_relay":
            return True
        # Route-inferred messages from aX are forwards, not direct asks
        if author.lower() == "ax" and metadata.get("route_inferred"):
            return True
    return False


def should_respond(event_data: dict, agent_name: str, agent_id: str = "") -> bool:
    author = get_author_name(event_data)
    author_id = get_author_id(event_data)
    # Never respond to ourselves
    if author.lower() == agent_name.lower():
        return False
    if agent_id and author_id and author_id == agent_id:
        return False
    # Only respond if actually mentioned
    if not is_mentioned(event_data, agent_name):
        return False
    # Skip aX relay/system noise, but allow fresh explicit direct mentions from
    # other agents (including aX) to reach the worker.
    if _is_ax_noise(event_data):
        log.info(f"Skipping noise from @{author}: {event_data.get('content', '')[:60]}")
        return False
    return True


# ---------------------------------------------------------------------------
# Worker thread — processes mentions from the queue
# ---------------------------------------------------------------------------

def _is_paused(agent_name: str) -> bool:
    """Check if this agent (or all agents) are paused via file flags."""
    pause_all = Path.home() / ".ax" / "sentinel_pause"
    pause_one = Path.home() / ".ax" / f"sentinel_pause_{agent_name}"
    return pause_all.exists() or pause_one.exists()


def mention_worker(q: queue.Queue, api_holder: list, agent_name: str,
                   space_id: str, args, sessions: SessionStore,
                   histories: HistoryStore):
    """Background worker that processes mentions sequentially from a queue.

    api_holder is a single-element list [api] so the main thread can swap
    the client on reconnect and the worker always uses the current one.
    """
    _was_paused = False
    _rate_limit_backoff = 0  # Consecutive rate-limit hits
    _rate_limit_until = 0.0  # Epoch time when backoff expires

    while True:
        try:
            event_data = q.get(timeout=1.0)
        except queue.Empty:
            continue

        if event_data is None:  # Poison pill
            break

        # Rate limit cooldown — if we were recently rate-limited, wait
        now = time.time()
        if now < _rate_limit_until:
            wait_secs = int(_rate_limit_until - now)
            log.info(f"Rate limit cooldown — waiting {wait_secs}s before processing next message")
            time.sleep(_rate_limit_until - now)

        # Pause gate: hold the message, don't process it
        while _is_paused(agent_name):
            if not _was_paused:
                log.info(f"PAUSED — holding {q.qsize()+1} messages (touch ~/.ax/sentinel_pause to pause, rm to resume)")
                _was_paused = True
            time.sleep(2.0)
        if _was_paused:
            log.info("RESUMED — processing queued messages")
            _was_paused = False

        api = api_holder[0]  # Always use the current client

        author = get_author_name(event_data)
        content = event_data.get("content", "")
        msg_id = event_data.get("id", "")
        # Reply parenting and runtime history are deliberately separate:
        # replies attach to the inbound message, while runtime memory can be
        # scoped across an agent's whole space session.
        raw_parent = event_data.get("parent_id") or event_data.get("parentId") or event_data.get("thread_id")
        history_thread_id = resolve_history_thread_id(
            event_data,
            agent_name=agent_name,
            space_id=space_id,
            author=author,
        )
        log.info(
            "Thread resolution: msg=%s parent_raw=%s reply=%s history=%s",
            msg_id[:12],
            str(raw_parent)[:12] if raw_parent else "None",
            msg_id[:12],
            history_thread_id[:48],
        )

        prompt = strip_mention(content, agent_name)
        if not prompt:
            log.info(f"Empty prompt from @{author}, skipping")
            q.task_done()
            continue

        log.info(f"PROCESSING from @{author} (queue depth: {q.qsize()}): {prompt[:120]}")

        if args.dry_run:
            log.info(f"[DRY RUN] Would run {args.runtime} with: {prompt[:100]}")
            q.task_done()
            continue

        try:
            result = run_cli(
                message=prompt,
                workdir=args.workdir,
                args=args,
                api=api,
                parent_id=msg_id,
                space_id=space_id,
                sessions=sessions,
                histories=histories,
                thread_id=history_thread_id,
            )
            if result:
                log.info(f"Response complete ({len(result)} chars)")
                _rate_limit_backoff = 0  # Reset on success
            elif result == "":
                # Empty string = rate limited (run_sdk returns "" after cooldown)
                _rate_limit_backoff += 1
                if _rate_limit_backoff >= 5:
                    # Too many consecutive rate limits — pause completely
                    log.error(f"Rate limited {_rate_limit_backoff} times in a row — "
                              f"PAUSING agent. Remove ~/.ax/sentinel_pause to resume.")
                    from pathlib import Path
                    Path(f"{Path.home()}/.ax/sentinel_pause").touch()
                    # Also drain the queue so we don't hammer on resume
                    drained = 0
                    while not q.empty():
                        try:
                            q.get_nowait()
                            q.task_done()
                            drained += 1
                        except queue.Empty:
                            break
                    log.warning(f"Drained {drained} queued messages to prevent cascade on resume")
                else:
                    cooldown = 60 * (2 ** _rate_limit_backoff)  # 120s, 240s, 480s, 960s
                    _rate_limit_until = time.time() + cooldown
                    log.warning(f"Rate limit backoff #{_rate_limit_backoff}/5 — "
                                f"cooling down {cooldown}s before next message")
            else:
                log.warning("CLI returned empty response")
        except Exception as e:
            log.error(f"Error handling mention: {e}", exc_info=True)
        finally:
            q.task_done()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(args):
    cfg = _load_config()

    token = os.environ.get("AX_TOKEN", cfg.get("token", ""))
    base_url = os.environ.get("AX_BASE_URL",
                              cfg.get("base_url", "http://localhost:8002"))
    agent_name = args.agent or os.environ.get("AX_AGENT_NAME",
                                               cfg.get("agent_name", ""))
    agent_id = os.environ.get("AX_AGENT_ID", cfg.get("agent_id", ""))
    space_id = os.environ.get("AX_SPACE_ID", cfg.get("space_id", ""))
    internal_api_key = (
        os.environ.get("INTERNAL_DISPATCH_API_KEY")
        or os.environ.get("AGENT_RUNNER_API_KEY", "")
    )

    if not token:
        log.error("No token. Set AX_TOKEN or configure ~/.ax/config.toml")
        sys.exit(1)
    if not agent_name:
        log.error("No agent_name. Set AX_AGENT_NAME or use --agent flag")
        sys.exit(1)

    if args.workdir is None:
        args.workdir = f"/home/ax-agent/agents/{agent_name}"
    Path(args.workdir).mkdir(parents=True, exist_ok=True)

    api = AxAPI(base_url, token, agent_name, agent_id, internal_api_key, space_id=space_id)
    api_holder = [api]  # Mutable container so worker thread sees reconnected clients
    sessions = SessionStore()
    histories = HistoryStore()
    mention_queue: queue.Queue = queue.Queue(maxsize=50)

    log.info("=" * 60)
    log.info("CLI Agent v2")
    log.info(f"  Agent:    @{agent_name} ({agent_id[:12]}...)")
    log.info(f"  Space:    {space_id[:12]}...")
    log.info(f"  API:      {base_url}")
    log.info(f"  Home:     {args.workdir}")
    log.info(f"  Runtime:  {args.runtime}")
    log.info(f"  Mode:     {'DRY RUN' if args.dry_run else 'LIVE'}")
    log.info(f"  Timeout:  {args.timeout}s")
    log.info(f"  Stream:   edit every {args.update_interval}s")
    log.info("  Memory:   thread continuity enabled (runtime-specific)")
    log.info(f"  Queue:    threaded worker (no dropped messages)")
    log.info(f"  Signals:  agent_processing events enabled")
    log.info("=" * 60)

    # Start worker thread — pass api_holder so it always uses the current client
    worker = threading.Thread(
        target=mention_worker,
        args=(mention_queue, api_holder, agent_name, space_id, args, sessions, histories),
        daemon=True,
    )
    worker.start()

    # Dedup
    seen_ids: set[str] = set()
    SEEN_MAX = 500

    backoff = 1

    while True:
        try:
            log.info("Connecting to SSE...")
            with api.connect_sse() as resp:
                if resp.status_code != 200:
                    log.error(f"SSE connection failed: {resp.status_code}")
                    raise ConnectionError(f"SSE {resp.status_code}")

                for event_type, data in iter_sse(resp):
                    backoff = 1

                    if event_type == "connected":
                        if isinstance(data, dict):
                            log.info(
                                f"Connected — space={data.get('space_id', space_id)[:12]} "
                                f"user={data.get('user', '?')}"
                            )
                        else:
                            log.info("Connected to SSE stream")
                        log.info(f"Listening for @{agent_name} mentions... "
                                 f"(sessions: {sessions.count()}, queue: {mention_queue.qsize()})")
                        continue

                    if event_type in ("bootstrap", "heartbeat",
                                      "identity_bootstrap", "ping"):
                        continue

                    if event_type in ("message", "mention"):
                        if not isinstance(data, dict):
                            continue

                        msg_id = data.get("id", "")
                        if msg_id in seen_ids:
                            continue

                        if should_respond(data, agent_name, agent_id):
                            seen_ids.add(msg_id)
                            if len(seen_ids) > SEEN_MAX:
                                to_keep = list(seen_ids)[-SEEN_MAX // 2:]
                                seen_ids = set(to_keep)

                            # Queue the mention — SSE listener never blocks
                            try:
                                mention_queue.put_nowait(data)
                                log.info(f"Queued mention from @{get_author_name(data)} "
                                         f"(queue depth: {mention_queue.qsize()})")
                            except queue.Full:
                                log.warning("Queue full — dropping mention")

        except (httpx.ConnectError, httpx.ReadTimeout):
            log.warning(f"Connection lost or read timeout. Reconnecting in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except KeyboardInterrupt:
            log.info("Shutting down...")
            mention_queue.put(None)  # Poison pill
            worker.join(timeout=5)
            break
        except Exception as e:
            log.error(f"Error: {e}. Reconnecting in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        finally:
            api.close()
            api = AxAPI(
                base_url,
                token,
                agent_name,
                agent_id,
                internal_api_key,
                space_id=space_id,
            )
            api_holder[0] = api  # Worker thread picks up the new client


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    args = parse_args()
    run(args)
