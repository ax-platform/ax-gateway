# Vendored from ax-agents on 2026-04-25 — see ax_cli/runtimes/hermes/README.md
"""Codex CLI runtime — subprocess-based, uses ChatGPT subscription."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time

from . import BaseRuntime, RuntimeResult, StreamCallback, register

log = logging.getLogger("runtime.codex_cli")


@register("codex_cli")
class CodexCLIRuntime(BaseRuntime):
    """Runs codex exec as a subprocess. Uses ChatGPT subscription."""

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
        extra = extra_args or {}
        cmd = [
            "codex", "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-C", workdir,
        ]
        if session_id:
            cmd = ["codex", "exec", "resume", session_id, "--json",
                   "--dangerously-bypass-approvals-and-sandbox"]
        if extra.get("disable_mcp"):
            cmd.extend(["-c", "mcp_servers.ax-platform.enabled=false"])
        if model:
            cmd.extend(["-m", model])

        log.info(f"codex_cli: {workdir}"
                 + (f" (resume {session_id[:12]})" if session_id else " (new)"))

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            text=True,
            env={**dict(__import__("os").environ),
                 "PATH": f"/home/ax-agent/.npm-global/bin:{__import__('os').environ.get('PATH', '')}"},
        )
        proc.stdin.write(message)
        proc.stdin.close()

        cb = stream_cb or StreamCallback()
        accumulated = ""
        new_session_id = None
        tool_count = 0
        files_written = []
        start_time = time.time()
        last_activity = time.time()
        exit_reason = "done"
        finished = threading.Event()

        silence_kill = max(30, timeout)

        def watchdog():
            nonlocal exit_reason
            while not finished.wait(timeout=10.0):
                if time.time() - last_activity > silence_kill:
                    exit_reason = "timeout"
                    proc.kill()
                    return

        wd = threading.Thread(target=watchdog, daemon=True)
        wd.start()

        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                last_activity = time.time()
                etype = event.get("type", "")

                if etype == "thread.started":
                    tid = event.get("thread_id", "")
                    if tid:
                        new_session_id = tid

                elif etype == "item.started":
                    item = event.get("item", {}) or {}
                    if item.get("type") == "command_execution":
                        tool_count += 1
                        cmd_str = item.get("command", "")
                        cb.on_tool_start("bash", _summarize(cmd_str))

                elif etype == "item.completed":
                    item = event.get("item", {}) or {}
                    if item.get("type") == "agent_message":
                        text = item.get("text", "")
                        if text:
                            accumulated = text
                            cb.on_text_complete(accumulated)

        except Exception as e:
            log.error(f"Stream error: {e}")
            exit_reason = "crashed"
        finally:
            finished.set()

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        if proc.returncode != 0 and exit_reason == "done":
            exit_reason = "crashed"

        elapsed = int(time.time() - start_time)
        return RuntimeResult(
            text=accumulated,
            session_id=new_session_id,
            tool_count=tool_count,
            files_written=files_written,
            exit_reason=exit_reason,
            elapsed_seconds=elapsed,
        )


def _summarize(command: str) -> str:
    short = " ".join(command.split())[:90]
    lowered = short.lower()
    if "apply_patch" in lowered:
        return "Applying patch..."
    if any(t in lowered for t in (" rg ", "grep ", "find ")):
        return "Searching..."
    if any(t in lowered for t in ("cat ", "head ", "ls ", "git ")):
        return "Reading files..."
    return f"Running: {short}..."
