#!/usr/bin/env python3
"""Headless live smoke test for the Claude Code aX channel bridge.

This starts the channel MCP server as a subprocess, performs the minimal MCP
handshake, sends a real aX message to the listener agent, verifies that the
channel receives it, and optionally calls the channel reply tool to verify the
completed status path.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class ProcessOutput:
    source: str
    line: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a headless live smoke test for axctl channel.")
    parser.add_argument("--listener-profile", required=True, help="Agent profile used by the channel listener.")
    parser.add_argument("--sender-profile", required=True, help="Profile used to send the test message.")
    parser.add_argument(
        "--profile-workdir",
        default=None,
        help="Working directory used when evaluating axctl profile env, for profile verification.",
    )
    parser.add_argument("--agent", required=True, help="Agent name the channel listens as.")
    parser.add_argument("--space-id", required=True, help="Space id to bridge and send into.")
    parser.add_argument(
        "--case",
        choices=["delivery", "reply"],
        default="reply",
        help="delivery verifies working; reply also calls the reply tool and verifies completed.",
    )
    parser.add_argument("--timeout", type=float, default=25.0, help="Seconds to wait for each expected event.")
    parser.add_argument(
        "--channel-command",
        default="axctl channel --debug",
        help="Command to launch the channel server. It runs with listener profile env applied.",
    )
    parser.add_argument(
        "--message",
        default="headless channel smoke",
        help="Message body suffix; @agent is prepended automatically.",
    )
    return parser.parse_args()


def profile_env(profile: str, *, cwd: str | None = None) -> dict[str, str]:
    cmd = f'eval "$(axctl profile env {shlex.quote(profile)})" && env'
    result = subprocess.run(["bash", "-lc", cmd], check=True, capture_output=True, text=True, cwd=cwd)
    env: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.startswith("AX_"):
            env[key] = value
    return env


def enqueue_lines(stream, source: str, out: "queue.Queue[ProcessOutput]") -> None:
    for line in iter(stream.readline, ""):
        out.put(ProcessOutput(source, line.rstrip("\n")))


def start_reader_threads(proc: subprocess.Popen[str], out: "queue.Queue[ProcessOutput]") -> None:
    assert proc.stdout is not None
    assert proc.stderr is not None
    threading.Thread(target=enqueue_lines, args=(proc.stdout, "stdout", out), daemon=True).start()
    threading.Thread(target=enqueue_lines, args=(proc.stderr, "stderr", out), daemon=True).start()


def send_json(proc: subprocess.Popen[str], payload: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    proc.stdin.flush()


def parse_json_line(line: str) -> dict[str, Any] | None:
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def wait_for_output(
    out: "queue.Queue[ProcessOutput]",
    *,
    timeout: float,
    predicate,
    label: str,
) -> ProcessOutput:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            item = out.get(timeout=0.25)
        except queue.Empty:
            continue
        print(f"[channel:{item.source}] {item.line}", file=sys.stderr)
        if predicate(item):
            return item
    raise TimeoutError(f"Timed out waiting for {label}")


def start_processing_watcher(sender_env: dict[str, str], space_id: str) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.update(sender_env)
    env["AX_SPACE_ID"] = space_id
    return subprocess.Popen(
        ["axctl", "events", "stream", "--filter", "agent_processing", "--json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def send_test_message(sender_env: dict[str, str], *, space_id: str, agent: str, message: str) -> str:
    env = os.environ.copy()
    env.update(sender_env)
    env["AX_SPACE_ID"] = space_id
    content = f"@{agent} {message}"
    result = subprocess.run(
        [
            "axctl",
            "send",
            "--space-id",
            space_id,
            "--to",
            agent,
            "--no-wait",
            "--json",
            content,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    data = json.loads(result.stdout)
    message_data = data.get("sent", {}).get("message") or data.get("message") or data
    message_id = message_data.get("id")
    if not message_id:
        raise RuntimeError(f"Could not find sent message id in: {result.stdout}")
    print(f"[smoke] sent message {message_id}", file=sys.stderr)
    return str(message_id)


def wait_for_processing_event(
    out: "queue.Queue[ProcessOutput]",
    *,
    message_id: str,
    status: str,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            item = out.get(timeout=0.25)
        except queue.Empty:
            continue
        line = item.line
        line = line.strip()
        print(f"[events:{item.source}] {line}", file=sys.stderr)
        if item.source != "stdout":
            continue
        payload = parse_json_line(line)
        data = payload.get("data", {}) if payload else {}
        if data.get("message_id") == message_id and data.get("status") == status:
            return data
    raise TimeoutError(f"Timed out waiting for agent_processing {status} for {message_id}")


def stop_process(proc: subprocess.Popen[str]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def main() -> int:
    args = parse_args()
    listener_env = profile_env(args.listener_profile, cwd=args.profile_workdir)
    sender_env = profile_env(args.sender_profile, cwd=args.profile_workdir)
    listener_env["AX_SPACE_ID"] = args.space_id

    proc = subprocess.Popen(
        shlex.split(args.channel_command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, **listener_env},
    )
    out: queue.Queue[ProcessOutput] = queue.Queue()
    start_reader_threads(proc, out)
    watcher = start_processing_watcher(sender_env, args.space_id)
    events_out: queue.Queue[ProcessOutput] = queue.Queue()
    start_reader_threads(watcher, events_out)

    try:
        send_json(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "ax-channel-smoke", "version": "0.1"},
                },
            },
        )
        wait_for_output(
            out,
            timeout=args.timeout,
            label="initialize response",
            predicate=lambda item: (parse_json_line(item.line) or {}).get("id") == 1,
        )
        send_json(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        message_id = send_test_message(
            sender_env,
            space_id=args.space_id,
            agent=args.agent,
            message=f"{args.message} {int(time.time())}",
        )
        wait_for_output(
            out,
            timeout=args.timeout,
            label="channel notification",
            predicate=lambda item: (parse_json_line(item.line) or {})
            .get("params", {})
            .get("meta", {})
            .get("message_id")
            == message_id,
        )
        working = wait_for_processing_event(
            events_out,
            message_id=message_id,
            status="working",
            timeout=args.timeout,
        )
        print(f"[smoke] working event ok: {working}", file=sys.stderr)

        if args.case == "reply":
            send_json(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "reply",
                        "arguments": {
                            "reply_to": message_id,
                            "text": "Headless channel smoke reply: received.",
                        },
                    },
                },
            )
            wait_for_output(
                out,
                timeout=args.timeout,
                label="reply tool response",
                predicate=lambda item: (parse_json_line(item.line) or {}).get("id") == 2,
            )
            completed = wait_for_processing_event(
                events_out,
                message_id=message_id,
                status="completed",
                timeout=args.timeout,
            )
            print(f"[smoke] completed event ok: {completed}", file=sys.stderr)

        print(json.dumps({"ok": True, "message_id": message_id, "case": args.case}))
        return 0
    finally:
        with contextlib.suppress(Exception):
            stop_process(watcher)
        with contextlib.suppress(Exception):
            stop_process(proc)


if __name__ == "__main__":
    raise SystemExit(main())
