#!/usr/bin/env python3
"""probe_bridge.py — deterministic Gateway-managed probe runtime.

This bridge is intentionally boring. It does not call Codex or any external
tooling. Instead, it emits a fixed sequence of Gateway status/tool/activity
events so we can test the message monitor with a predictable trace.

Usage example:

    ax gateway agents add gateway-probe \
      --type exec \
      --exec "python3 examples/gateway_probe/probe_bridge.py" \
      --workdir /absolute/path/to/repo

Example prompts:

    @gateway-probe probe
    @gateway-probe probe 6
    @gateway-probe run a 10 second probe
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from typing import Any

EVENT_PREFIX = "AX_GATEWAY_EVENT "
DEFAULT_SECONDS = 6
MAX_SECONDS = 60
SECONDS_RE = re.compile(r"\b(\d+)\s*(?:seconds?|secs?|s)?\b", re.IGNORECASE)
PROBE_RE = re.compile(r"\bprobe\b", re.IGNORECASE)


def emit_event(payload: dict[str, Any]) -> None:
    print(f"{EVENT_PREFIX}{json.dumps(payload, sort_keys=True)}", flush=True)


def _read_prompt() -> str:
    if len(sys.argv) > 1 and sys.argv[-1] != "-":
        return sys.argv[-1]
    env_prompt = os.environ.get("AX_MENTION_CONTENT", "").strip()
    if env_prompt:
        return env_prompt
    return sys.stdin.read().strip()


def _probe_seconds(prompt: str) -> int:
    match = SECONDS_RE.search(prompt)
    if not match:
        return DEFAULT_SECONDS
    seconds = int(match.group(1))
    if seconds < 1:
        return 1
    return min(MAX_SECONDS, seconds)


def _is_probe_prompt(prompt: str) -> bool:
    if not prompt.strip():
        return True
    return bool(PROBE_RE.search(prompt))


def _run_probe(seconds: int) -> int:
    tool_call_id = f"probe-sleep-{uuid.uuid4()}"
    start = time.monotonic()
    emit_event({"kind": "status", "status": "started", "message": "Probe accepted"})
    emit_event({"kind": "status", "status": "thinking", "message": f"Probe planning {seconds}s run"})
    emit_event({"kind": "status", "status": "processing", "message": f"Probe sleeping for {seconds}s"})
    emit_event(
        {
            "kind": "tool_start",
            "tool_name": "probe_sleep",
            "tool_action": "sleep",
            "tool_call_id": tool_call_id,
            "status": "tool_call",
            "arguments": {"seconds": seconds},
            "message": f"Probe sleeping for {seconds}s",
        }
    )

    for remaining in range(seconds, 0, -1):
        emit_event(
            {
                "kind": "activity",
                "activity": f"Probe tick {seconds - remaining + 1}/{seconds} ({remaining}s left)",
            }
        )
        time.sleep(1)

    emit_event(
        {
            "kind": "tool_result",
            "tool_name": "probe_sleep",
            "tool_action": "sleep",
            "tool_call_id": tool_call_id,
            "arguments": {"seconds": seconds},
            "initial_data": {"slept_seconds": seconds, "probe": True},
            "status": "tool_complete",
            "duration_ms": int((time.monotonic() - start) * 1000),
            "message": "Probe sleep finished",
        }
    )
    emit_event({"kind": "status", "status": "completed", "message": "Probe complete"})
    print(f"PROBE_OK seconds={seconds}")
    return 0


def main() -> int:
    prompt = _read_prompt()
    if not _is_probe_prompt(prompt):
        print(
            "PROBE_ERROR unsupported prompt. Use 'probe' optionally followed by a duration, for example 'probe 6'."
        )
        return 1
    return _run_probe(_probe_seconds(prompt))


if __name__ == "__main__":
    raise SystemExit(main())
