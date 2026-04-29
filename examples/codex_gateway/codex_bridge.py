#!/usr/bin/env python3
"""codex_bridge.py — Gateway-managed bridge for the Codex CLI.

This bridge is designed for `ax gateway agents add ... --type exec`.
It converts `codex exec --json` events into lightweight Gateway progress
events so the Gateway can publish agent-processing and tool-call activity
back to aX while Codex is still working.

Usage example:

    ax gateway agents add codex \
      --type exec \
      --exec "python3 examples/codex_gateway/codex_bridge.py" \
      --workdir /absolute/path/to/repo
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from typing import Any

EVENT_PREFIX = "AX_GATEWAY_EVENT "
DEFAULT_MODEL = os.environ.get("CODEX_GATEWAY_MODEL", "gpt-5.4")
DEFAULT_SANDBOX = os.environ.get("CODEX_GATEWAY_SANDBOX", "workspace-write")
MAX_SLEEP_SECONDS = 300
SLEEP_RE = re.compile(r"\b(?:sleep|pause|wait)\s+(?:for\s+)?(\d+)\s*(?:seconds?|secs?|s)\b", re.IGNORECASE)
TIMER_RE = re.compile(r"\b(\d+)\s*(?:seconds?|secs?|s)\s+(?:timer|countdown)\b", re.IGNORECASE)
TIMER_FOR_RE = re.compile(r"\b(?:timer|countdown)\s+(?:for\s+)?(\d+)\s*(?:seconds?|secs?|s)\b", re.IGNORECASE)


def emit_event(payload: dict[str, Any]) -> None:
    print(f"{EVENT_PREFIX}{json.dumps(payload, sort_keys=True)}", flush=True)


def _read_prompt() -> str:
    if len(sys.argv) > 1 and sys.argv[-1] != "-":
        return sys.argv[-1]
    env_prompt = os.environ.get("AX_MENTION_CONTENT", "").strip()
    if env_prompt:
        return env_prompt
    stdin_text = sys.stdin.read().strip()
    return stdin_text


def _sleep_demo_seconds(prompt: str) -> int | None:
    for regex in (SLEEP_RE, TIMER_RE, TIMER_FOR_RE):
        match = regex.search(prompt)
        if not match:
            continue
        seconds = int(match.group(1))
        if 0 < seconds <= MAX_SLEEP_SECONDS:
            return seconds
    return None


def _run_sleep_demo(seconds: int) -> int:
    tool_call_id = f"sleep-{uuid.uuid4()}"
    start = time.monotonic()
    emit_event({"kind": "status", "status": "thinking", "message": f"Planning sleep for {seconds}s"})
    emit_event({"kind": "status", "status": "processing", "message": f"Sleeping for {seconds}s"})
    emit_event(
        {
            "kind": "tool_start",
            "tool_name": "sleep",
            "tool_action": "sleep",
            "status": "tool_call",
            "tool_call_id": tool_call_id,
            "arguments": {"seconds": seconds},
            "message": f"Sleeping for {seconds}s",
        }
    )
    deadline = time.monotonic() + seconds
    while True:
        remaining = max(0, int(round(deadline - time.monotonic())))
        if remaining <= 0:
            break
        emit_event(
            {
                "kind": "activity",
                "activity": f"Sleeping... {remaining}s remaining",
            }
        )
        time.sleep(min(5, remaining))
    emit_event(
        {
            "kind": "tool_result",
            "tool_name": "sleep",
            "tool_action": "sleep",
            "tool_call_id": tool_call_id,
            "arguments": {"seconds": seconds},
            "initial_data": {"slept_seconds": seconds},
            "status": "tool_complete",
            "duration_ms": int((time.monotonic() - start) * 1000),
        }
    )
    emit_event({"kind": "status", "status": "completed"})
    print(f"Paused for {seconds} seconds and I am back.")
    return 0


def _codex_command(prompt: str) -> list[str]:
    workdir = os.environ.get("CODEX_GATEWAY_WORKDIR", os.getcwd())
    system_prompt = os.environ.get(
        "CODEX_GATEWAY_SYSTEM_PROMPT",
        "You are Codex running as a Gateway-managed aX agent. "
        "Be concise and helpful. Keep replies under 2000 characters unless the task truly needs more detail. "
        "When useful, inspect the local workspace and use tools.",
    ).strip()
    full_prompt = f"{system_prompt}\n\nUser message:\n{prompt.strip()}"
    return [
        "codex",
        "exec",
        "--json",
        "--color",
        "never",
        "--skip-git-repo-check",
        "--sandbox",
        DEFAULT_SANDBOX,
        "--model",
        DEFAULT_MODEL,
        "--cd",
        workdir,
        full_prompt,
    ]


def _tool_event_payload(item: dict[str, Any], *, phase: str) -> tuple[str, dict[str, Any]]:
    item_type = str(item.get("type") or "tool")
    item_id = str(item.get("id") or uuid.uuid4())
    if item_type == "command_execution":
        command = str(item.get("command") or "").strip()
        arguments = {"command": command} if command else None
        initial_data: dict[str, Any] = {}
        if item.get("aggregated_output"):
            initial_data["output"] = str(item.get("aggregated_output"))[:4000]
        if item.get("exit_code") is not None:
            initial_data["exit_code"] = item.get("exit_code")
        payload = {
            "tool_name": "shell",
            "tool_action": command or "command_execution",
            "tool_call_id": item_id,
            "arguments": arguments,
            "initial_data": initial_data or None,
            "message": f"Running command: {command}" if command else "Running command",
            "status": (
                "tool_call"
                if phase == "start"
                else ("tool_complete" if int(item.get("exit_code") or 0) == 0 else "error")
            ),
        }
        return item_type, payload
    payload = {
        "tool_name": item_type,
        "tool_action": str(item.get("title") or item_type),
        "tool_call_id": item_id,
        "initial_data": {"item": item},
        "message": f"Using {item_type}",
        "status": "tool_call" if phase == "start" else "tool_complete",
    }
    return item_type, payload


def _run_codex(prompt: str) -> int:
    cmd = _codex_command(prompt)
    final_text = ""
    stderr_lines: list[str] = []

    emit_event({"kind": "status", "status": "thinking", "message": "Starting Codex"})

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    for raw in process.stdout:
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event_type = str(payload.get("type") or "")
        item = payload.get("item") if isinstance(payload.get("item"), dict) else None
        if event_type == "item.started" and item is not None and str(item.get("type") or "") != "agent_message":
            _, tool_payload = _tool_event_payload(item, phase="start")
            emit_event({"kind": "tool_start", **tool_payload})
            continue
        if event_type == "item.completed" and item is not None:
            item_type = str(item.get("type") or "")
            if item_type == "agent_message":
                text = str(item.get("text") or "").strip()
                if text:
                    final_text = text
                continue
            _, tool_payload = _tool_event_payload(item, phase="result")
            emit_event({"kind": "tool_result", **tool_payload})
            continue
        if event_type == "turn.completed":
            emit_event({"kind": "status", "status": "completed"})

    stderr_lines.extend(process.stderr.readlines())
    return_code = process.wait()
    stderr_text = "\n".join(line.strip() for line in stderr_lines if line.strip())
    if return_code != 0:
        if stderr_text:
            print(f"Codex bridge failed:\n{stderr_text[:2000]}")
        else:
            print(f"Codex bridge failed with exit code {return_code}.")
        return return_code

    if final_text:
        print(final_text)
    else:
        print("Codex finished without a final reply.")
    return 0


def main() -> int:
    prompt = _read_prompt()
    if not prompt:
        print("(no mention content received)", file=sys.stderr)
        return 1

    sleep_seconds = _sleep_demo_seconds(prompt)
    if sleep_seconds is not None:
        return _run_sleep_demo(sleep_seconds)

    return _run_codex(prompt)


if __name__ == "__main__":
    raise SystemExit(main())
