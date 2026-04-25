# Vendored from ax-agents on 2026-04-25 — see ax_cli/runtimes/hermes/README.md
"""Claude Code CLI runtime — subprocess-based, uses Max subscription."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time

from . import BaseRuntime, RuntimeResult, StreamCallback, register

log = logging.getLogger("runtime.claude_cli")


@register("claude_cli")
class ClaudeCLIRuntime(BaseRuntime):
    """Runs claude -p as a subprocess. Uses the user's Claude subscription."""

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
            "claude", "-p",
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
        ]
        if extra.get("add_dir"):
            cmd.extend(["--add-dir", extra["add_dir"]])
        if session_id:
            cmd.extend(["--resume", session_id])
        if model:
            cmd.extend(["--model", model])
        if extra.get("allowed_tools"):
            cmd.extend(["--allowedTools", extra["allowed_tools"]])
        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])

        log.info(f"claude_cli: {workdir}"
                 + (f" (resume {session_id[:12]})" if session_id else " (new)"))

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

        cb = stream_cb or StreamCallback()
        accumulated = ""
        new_session_id = None
        tool_count = 0
        files_written = []
        start_time = time.time()
        last_activity = time.time()
        exit_reason = "done"
        finished = threading.Event()

        # Silence watchdog
        silence_kill = max(30, timeout)

        def watchdog():
            nonlocal exit_reason
            while not finished.wait(timeout=10.0):
                if time.time() - last_activity > silence_kill:
                    exit_reason = "timeout"
                    log.warning(f"Silence timeout ({silence_kill}s) — killing")
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

                if etype == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            accumulated = block["text"]
                            cb.on_text_complete(accumulated)
                        if block.get("type") == "tool_use":
                            tool_name = block.get("name", "")
                            tool_input = block.get("input", {})
                            tool_count += 1
                            summary = _tool_summary(tool_name, tool_input)
                            cb.on_tool_start(tool_name, summary)
                            if tool_name in ("Write", "write"):
                                path = tool_input.get("file_path", "")
                                if path:
                                    files_written.append(path)

                elif etype == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        accumulated += text
                        cb.on_text_delta(text)

                elif etype == "result":
                    result_text = event.get("result", "")
                    if result_text:
                        accumulated = result_text
                        cb.on_text_complete(accumulated)
                    sid = event.get("session_id", "")
                    if sid:
                        new_session_id = sid

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
        if proc.returncode != 0 and not accumulated:
            stderr = proc.stderr.read()
            log.error(f"claude exit {proc.returncode}: {stderr[:300]}")

        elapsed = int(time.time() - start_time)
        return RuntimeResult(
            text=accumulated,
            session_id=new_session_id,
            tool_count=tool_count,
            files_written=files_written,
            exit_reason=exit_reason,
            elapsed_seconds=elapsed,
        )


def _tool_summary(name: str, inp: dict) -> str:
    if name in ("Read", "read"):
        p = inp.get("file_path", "")
        return f"Reading {p.split('/')[-1]}..." if "/" in p else f"Reading {p}..."
    if name in ("Write", "write"):
        p = inp.get("file_path", "")
        return f"Writing {p.split('/')[-1]}..." if "/" in p else f"Writing {p}..."
    if name in ("Edit", "edit"):
        p = inp.get("file_path", "")
        return f"Editing {p.split('/')[-1]}..." if "/" in p else f"Editing {p}..."
    if name in ("Bash", "bash"):
        return f"Running: {str(inp.get('command', ''))[:60]}..."
    if name in ("Grep", "grep"):
        return f"Searching: {inp.get('pattern', '')}..."
    if name in ("Glob", "glob"):
        return f"Finding: {inp.get('pattern', '')}..."
    return f"Using {name}..."
