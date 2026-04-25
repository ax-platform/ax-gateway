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
                        choices=["claude", "codex", "claude_cli", "codex_cli", "openai_sdk"],
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

    def count(self) -> int:
        with self._lock:
            return len(self._store)


# ---------------------------------------------------------------------------
# API Client
# ---------------------------------------------------------------------------

class AxAPI:
    """Thin wrapper for the aX REST API."""

    def __init__(self, base_url: str, token: str, agent_name: str,
                 agent_id: str, internal_api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.agent_name = agent_name
        self.agent_id = agent_id
        self.internal_api_key = internal_api_key
        self._client = httpx.Client(timeout=30.0)

    def _headers(self) -> dict:
        h = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        if self.agent_id:
            h["X-Agent-Id"] = self.agent_id
        return h

    def send_message(self, space_id: str, content: str,
                     parent_id: str | None = None) -> dict | None:
        body = {
            "content": content,
            "space_id": space_id,
            "channel": "main",
            "message_type": "text",
        }
        if parent_id:
            body["parent_id"] = parent_id
        try:
            resp = self._client.post(
                f"{self.base_url}/api/v1/messages",
                json=body,
                headers=self._headers(),
            )
            if resp.status_code == 200 and resp.text:
                return resp.json().get("message", {})
            else:
                log.warning(f"send_message: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            log.error(f"send_message error: {e}")
        return None

    def edit_message(self, message_id: str, content: str) -> bool:
        try:
            resp = self._client.patch(
                f"{self.base_url}/api/v1/messages/{message_id}",
                json={"content": content},
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

    def signal_processing(self, message_id: str, status: str = "started",
                          space_id: str = ""):
        """Fire an agent_processing event so the frontend shows a status indicator."""
        if not self.internal_api_key:
            return
        try:
            self._client.post(
                f"{self.base_url}/auth/internal/agent-status",
                json={
                    "agent_name": self.agent_name,
                    "agent_id": self.agent_id,
                    "status": status,
                    "message_id": message_id,
                    "space_id": space_id,
                },
                headers={
                    "X-API-Key": self.internal_api_key,
                    "Content-Type": "application/json",
                },
            )
        except Exception as e:
            log.debug(f"signal_processing error (non-fatal): {e}")

    def connect_sse(self) -> httpx.Response:
        # Use a read timeout so we reconnect if the ALB silently drops
        # the connection (default idle timeout ~60s). 90s gives headroom.
        return self._client.stream(
            "GET",
            f"{self.base_url}/api/sse/messages",
            params={"token": self.token},
            headers=self._headers(),
            timeout=httpx.Timeout(connect=10.0, read=90.0, write=10.0, pool=10.0),
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
    cmd = [
        "codex", "exec",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "-C", workdir,
    ]
    if session_id:
        # Codex uses `codex exec resume --last` or by session ID
        cmd = ["codex", "exec", "resume", session_id, "--json",
               "--dangerously-bypass-approvals-and-sandbox"]
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
) -> str:
    """Execute via a runtime plugin, bridging its StreamCallback to aX messages."""
    import sys
    # Ensure the agents directory is on sys.path so runtimes/ and tools/ can import
    agents_dir = str(Path(__file__).parent)
    if agents_dir not in sys.path:
        sys.path.insert(0, agents_dir)

    from runtimes import get_runtime, StreamCallback

    runtime = get_runtime(runtime_name)
    thread_id = parent_id or "default"
    existing_session = sessions.get(thread_id)

    log.info(f"Runtime: {runtime_name} in {workdir}"
             + (f" (session {existing_session[:12]})" if existing_session else " (new)"))

    # Signal: processing started
    api.signal_processing(parent_id, "started", space_id=space_id)
    api.signal_processing(parent_id, "thinking", space_id=space_id)

    # No intermediate message creates/edits during execution.
    # Every POST/PATCH triggers the backend dispatch pipeline → concierge,
    # causing a cascade. Instead, we only use signal_processing() for status
    # (SSE-only, no DB, no dispatch) and create ONE message at the end.
    reply_id = None
    accumulated_text = ""
    tool_count = 0

    # StreamCallback — accumulates text, signals status via SSE only
    class AxStreamCallback(StreamCallback):
        def on_text_delta(self, text: str):
            nonlocal accumulated_text
            accumulated_text += text

        def on_text_complete(self, text: str):
            nonlocal accumulated_text
            accumulated_text = text

        def on_tool_start(self, tool_name: str, summary: str):
            nonlocal tool_count
            tool_count += 1
            api.signal_processing(parent_id, "tool_call", space_id=space_id)

        def on_tool_end(self, tool_name: str, summary: str):
            pass

        def on_status(self, status: str):
            api.signal_processing(parent_id, status, space_id=space_id)

    # Load system prompt from agent CLAUDE.md
    claude_md = Path(workdir) / "CLAUDE.md"
    system_prompt = claude_md.read_text() if claude_md.exists() else None

    # Build extra args
    extra = {
        "add_dir": "/home/ax-agent/shared/repos",
    }
    if hasattr(args, "disable_codex_mcp") and args.disable_codex_mcp:
        extra["disable_mcp"] = True
    if hasattr(args, "allowed_tools") and args.allowed_tools:
        extra["allowed_tools"] = args.allowed_tools

    # Execute
    result = runtime.execute(
        message,
        workdir=workdir,
        model=args.model,
        system_prompt=system_prompt,
        session_id=existing_session,
        stream_cb=AxStreamCallback(),
        timeout=args.timeout,
        extra_args=extra,
    )

    # Save session
    if result.session_id:
        sessions.set(thread_id, result.session_id)
        log.info(f"Session saved: {result.session_id[:12]}")

    # Build final content
    final = result.text
    if result.files_written:
        names = [p.split("/")[-1] for p in result.files_written]
        final += "\n\n📄 Wrote: " + ", ".join(names)

    if result.exit_reason == "crashed":
        final += f"\n\n---\n⚠️ Agent ended unexpectedly ({result.elapsed_seconds}s)."
    elif result.exit_reason == "timeout":
        final += f"\n\n---\n⏱️ Timed out ({result.elapsed_seconds}s)."

    if not final:
        final = f"Completed ({result.elapsed_seconds}s) — no text output."

    # Final message update
    if reply_id:
        api.edit_message(reply_id, final)
    else:
        api.send_message(space_id=space_id, content=final, parent_id=parent_id)

    api.signal_processing(parent_id, "completed", space_id=space_id)

    if reply_id and len(final) > 50:
        api.request_summary(reply_id)

    log.info(f"Runtime {runtime_name}: {result.exit_reason} "
             f"({len(final)} chars, {result.tool_count} tools, {result.elapsed_seconds}s)")
    return final


def run_cli(message: str, workdir: str, args, api: AxAPI,
            parent_id: str, space_id: str,
            sessions: SessionStore) -> str:
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
    if runtime_name in ("claude_cli", "codex_cli", "openai_sdk"):
        return _run_via_runtime_plugin(
            runtime_name, message, workdir, args, api,
            parent_id, space_id, sessions,
        )

    # ── Legacy subprocess path (fallback, should not be reached) ────
    thread_id = parent_id or "default"
    existing_session = sessions.get(thread_id)

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
                        api.signal_processing(parent_id, "tool_call", space_id=space_id)

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
                                parent_id, "tool_call", space_id=space_id)

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
        sessions.set(thread_id, new_session_id)
        log.info(f"Session saved: {new_session_id[:12]} for thread {thread_id[:12]}")

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


def is_mentioned(event_data: dict, agent_name: str) -> bool:
    mentions = event_data.get("mentions", [])
    if agent_name.lower() in [m.lower() for m in mentions]:
        return True
    content = event_data.get("content", "")
    if f"@{agent_name.lower()}" in content.lower():
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


def should_respond(event_data: dict, agent_name: str) -> bool:
    author = get_author_name(event_data)
    # Never respond to ourselves
    if author.lower() == agent_name.lower():
        return False
    # Only respond if actually mentioned
    if not is_mentioned(event_data, agent_name):
        return False
    # Never respond to aX (concierge). When a user @mentions us, we get the
    # message directly from SSE. aX also receives it and re-routes a rephrased
    # copy — responding to both creates a cascade. Ignore all aX messages.
    if author.lower() == "ax":
        log.info(f"Ignoring aX message: {event_data.get('content', '')[:60]}")
        return False
    # Skip other system noise (tool results, routing metadata)
    if _is_ax_noise(event_data):
        log.info(f"Skipping noise from @{author}: {event_data.get('content', '')[:60]}")
        return False
    # Respond to humans and other agents who explicitly @mention us.
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
                   space_id: str, args, sessions: SessionStore):
    """Background worker that processes mentions sequentially from a queue.

    api_holder is a single-element list [api] so the main thread can swap
    the client on reconnect and the worker always uses the current one.
    """
    _was_paused = False
    while True:
        try:
            event_data = q.get(timeout=1.0)
        except queue.Empty:
            continue

        if event_data is None:  # Poison pill
            break

        # Pause gate: hold the message, don't process it
        while _is_paused(agent_name):
            if not _was_paused:
                log.info(f"PAUSED — holding {q.qsize()+1} messages "
                         f"(touch ~/.ax/sentinel_pause to pause, rm to resume)")
                _was_paused = True
            time.sleep(2.0)
        if _was_paused:
            log.info("RESUMED — processing queued messages")
            _was_paused = False

        api = api_holder[0]  # Always use the current client

        author = get_author_name(event_data)
        content = event_data.get("content", "")
        msg_id = event_data.get("id", "")
        # Thread ID: use parent_id if this is a threaded reply, otherwise msg_id starts a new thread
        raw_parent = event_data.get("parent_id") or event_data.get("parentId") or event_data.get("thread_id")
        parent_id = raw_parent or msg_id
        log.info(f"Thread resolution: msg={msg_id[:12]} parent_raw={raw_parent} -> thread={parent_id[:12] if parent_id else 'none'}")

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
            )
            if result:
                log.info(f"Response complete ({len(result)} chars)")
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
    internal_api_key = os.environ.get("AGENT_RUNNER_API_KEY", "")

    if not token:
        log.error("No token. Set AX_TOKEN or configure ~/.ax/config.toml")
        sys.exit(1)
    if not agent_name:
        log.error("No agent_name. Set AX_AGENT_NAME or use --agent flag")
        sys.exit(1)

    if args.workdir is None:
        args.workdir = f"/home/ax-agent/agents/{agent_name}"
    Path(args.workdir).mkdir(parents=True, exist_ok=True)

    api = AxAPI(base_url, token, agent_name, agent_id, internal_api_key)
    api_holder = [api]  # Mutable container so worker thread sees reconnected clients
    sessions = SessionStore()
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
    log.info(f"  Sessions: continuity enabled (--resume)")
    log.info(f"  Queue:    threaded worker (no dropped messages)")
    log.info(f"  Signals:  agent_processing events enabled")
    log.info("=" * 60)

    # Start worker thread — pass api_holder so it always uses the current client
    worker = threading.Thread(
        target=mention_worker,
        args=(mention_queue, api_holder, agent_name, space_id, args, sessions),
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

                        if should_respond(data, agent_name):
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
            api = AxAPI(base_url, token, agent_name, agent_id, internal_api_key)
            api_holder[0] = api  # Worker thread picks up the new client


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    args = parse_args()
    run(args)
