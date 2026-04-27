# Vendored from ax-agents on 2026-04-25 — see ax_cli/runtimes/hermes/README.md
"""Agent tools for SDK-based runtimes.

These tools are runtime-agnostic — any SDK runtime can use them.
Each tool returns an OpenAI-compatible tool definition and an execute function.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
from dataclasses import dataclass


@dataclass
class ToolResult:
    output: str
    is_error: bool = False


# ── Path security ─────────────────────────────────────────────────────────
# Agents can read shared repos and their own home. They can only write to
# their own worktrees/workspace and /tmp.  Secrets are always blocked.

BLOCKED_READ_PATTERNS = [
    "/.ax/",  "/.codex/",  "/.aws/",  "/.ssh/",
    "/.env", "/secrets", "/credentials",
]

def _check_read_path(path: str) -> str | None:
    """Return error message if path is blocked for reading, else None."""
    resolved = os.path.realpath(path)
    for pattern in BLOCKED_READ_PATTERNS:
        if pattern in resolved:
            return f"Access denied: {path} (blocked pattern: {pattern})"
    return None


def _check_write_path(path: str, workdir: str) -> str | None:
    """Return error message if path is blocked for writing, else None."""
    resolved = os.path.realpath(path)
    # Check blocked patterns first
    err = _check_read_path(path)
    if err:
        return err
    # Writing is only allowed in:
    # - agent's own worktrees/workspace dirs
    # - /tmp
    allowed_prefixes = [
        os.path.realpath(workdir),             # agent home dir
        "/tmp",
    ]
    # Also allow writing in any worktree under agents/
    agents_dir = os.path.realpath("/home/ax-agent/agents")
    if resolved.startswith(agents_dir):
        # Must be under a worktrees/ or workspace/ subdir
        rel = resolved[len(agents_dir):]
        if "/worktrees/" in rel or "/workspace/" in rel or "/notes/" in rel:
            return None
    if not any(resolved.startswith(p) for p in allowed_prefixes):
        return f"Write denied: {path} (not in agent workspace or /tmp)"
    return None


def _check_bash_command(command: str) -> str | None:
    """Return error message if command is blocked, else None."""
    blocked = [
        "cat ~/.ax/", "cat ~/.codex/", "cat ~/.aws/", "cat ~/.ssh/",
        "cat /home/ax-agent/.ax/", "cat /home/ax-agent/.codex/",
        "rm -rf /",
    ]
    for pattern in blocked:
        if pattern in command:
            return f"Command blocked: contains '{pattern}'"
    return None


# ── Tool definitions (OpenAI responses API format) ──────────────────────────

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "name": "read_file",
        "description": "Read a file from disk. Returns file contents with line numbers.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute file path"},
                "offset": {"type": "integer", "description": "Start line (1-indexed)", "default": 1},
                "limit": {"type": "integer", "description": "Max lines to read", "default": 2000},
            },
            "required": ["path"],
        },
    },
    {
        "type": "function",
        "name": "write_file",
        "description": "Write content to a file (creates parent dirs). Overwrites existing.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute file path"},
                "content": {"type": "string", "description": "File content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "type": "function",
        "name": "edit_file",
        "description": "Replace exact text in a file. old_text must match exactly.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute file path"},
                "old_text": {"type": "string", "description": "Exact text to find"},
                "new_text": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "type": "function",
        "name": "bash",
        "description": "Run a shell command. Returns stdout + stderr.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 120},
            },
            "required": ["command"],
        },
    },
    {
        "type": "function",
        "name": "grep",
        "description": "Search file contents using ripgrep. Returns matching lines.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search"},
                "path": {"type": "string", "description": "Directory or file to search"},
                "glob": {"type": "string", "description": "File glob filter (e.g. '*.py')"},
            },
            "required": ["pattern"],
        },
    },
    {
        "type": "function",
        "name": "glob_files",
        "description": "Find files matching a glob pattern. Returns file paths.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (e.g. '**/*.py')"},
                "path": {"type": "string", "description": "Base directory to search from"},
            },
            "required": ["pattern"],
        },
    },
]


# ── Tool execution ──────────────────────────────────────────────────────────

def execute_tool(name: str, args: dict, workdir: str) -> ToolResult:
    """Execute a tool by name. Returns ToolResult."""
    fn = _TOOL_FNS.get(name)
    if not fn:
        return ToolResult(output=f"Unknown tool: {name}", is_error=True)
    try:
        return fn(args, workdir)
    except Exception as e:
        return ToolResult(output=f"Error: {type(e).__name__}: {e}", is_error=True)


def _read_file(args: dict, workdir: str) -> ToolResult:
    path = args["path"]
    err = _check_read_path(path)
    if err:
        return ToolResult(output=err, is_error=True)
    offset = max(1, args.get("offset", 1))
    limit = args.get("limit", 2000)
    try:
        with open(path, "r") as f:
            lines = f.readlines()
        selected = lines[offset - 1:offset - 1 + limit]
        numbered = "".join(
            f"{offset + i:>6}\t{line}" for i, line in enumerate(selected)
        )
        return ToolResult(output=numbered or "(empty file)")
    except FileNotFoundError:
        return ToolResult(output=f"File not found: {path}", is_error=True)


def _write_file(args: dict, workdir: str) -> ToolResult:
    path = args["path"]
    err = _check_write_path(path, workdir)
    if err:
        return ToolResult(output=err, is_error=True)
    content = args["content"]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return ToolResult(output=f"Wrote {len(content)} bytes to {path}")


def _edit_file(args: dict, workdir: str) -> ToolResult:
    path = args["path"]
    err = _check_write_path(path, workdir)
    if err:
        return ToolResult(output=err, is_error=True)
    old_text = args["old_text"]
    new_text = args["new_text"]
    with open(path, "r") as f:
        content = f.read()
    count = content.count(old_text)
    if count == 0:
        return ToolResult(output="old_text not found in file", is_error=True)
    if count > 1:
        return ToolResult(
            output=f"old_text matches {count} times — must be unique", is_error=True
        )
    content = content.replace(old_text, new_text, 1)
    with open(path, "w") as f:
        f.write(content)
    return ToolResult(output=f"Edited {path}")


def _bash(args: dict, workdir: str) -> ToolResult:
    command = args["command"]
    err = _check_bash_command(command)
    if err:
        return ToolResult(output=err, is_error=True)
    timeout = args.get("timeout", 120)
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout
        if result.stderr:
            output += f"\nSTDERR:\n{result.stderr}"
        if result.returncode != 0:
            output += f"\n(exit code {result.returncode})"
        # Truncate very long output
        if len(output) > 30000:
            output = output[:15000] + "\n...(truncated)...\n" + output[-15000:]
        return ToolResult(output=output or "(no output)")
    except subprocess.TimeoutExpired:
        return ToolResult(output=f"Command timed out after {timeout}s", is_error=True)


def _grep(args: dict, workdir: str) -> ToolResult:
    pattern = args["pattern"]
    path = args.get("path", workdir)
    cmd = ["rg", "--no-heading", "-n", pattern, path]
    if args.get("glob"):
        cmd.extend(["--glob", args["glob"]])
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, cwd=workdir
        )
        output = result.stdout
        if len(output) > 20000:
            output = output[:20000] + "\n...(truncated)..."
        return ToolResult(output=output or "(no matches)")
    except FileNotFoundError:
        return ToolResult(output="rg (ripgrep) not found", is_error=True)


def _glob_files(args: dict, workdir: str) -> ToolResult:
    pattern = args["pattern"]
    base = args.get("path", workdir)
    matches = sorted(str(p) for p in pathlib.Path(base).glob(pattern))
    if len(matches) > 200:
        matches = matches[:200]
        matches.append("...(truncated, 200+ matches)")
    return ToolResult(output="\n".join(matches) or "(no matches)")


_TOOL_FNS = {
    "read_file": _read_file,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "bash": _bash,
    "grep": _grep,
    "glob_files": _glob_files,
}
