# Vendored from ax-agents on 2026-04-25 — see ax_cli/runtimes/hermes/README.md
"""OpenAI SDK runtime — uses ChatGPT OAuth subscription via Codex endpoint."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

from . import BaseRuntime, RuntimeResult, StreamCallback, register

log = logging.getLogger("runtime.openai_sdk")

CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"
CODEX_SHARED_TOKEN_PATH = Path.home() / ".ax" / "codex-token"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
TOKEN_BLOCK_SECONDS = 600
_BLOCKED_TOKENS: dict[str, float] = {}

# Preamble injected into system prompt for SDK runtimes.
# This is the single source of truth for how all sentinel agents operate.
SDK_PREAMBLE = """\
# How You Work

You are a sentinel agent. Someone @mentioned you with a task. Do the work and respond.

## The Three Channels

There are exactly three ways you communicate. Don't mix them up.

**1. Tools** — how you do work (read_file, write_file, edit_file, bash, grep, glob_files).
   Use tools to read code, write code, run tests, git commit, git push, create PRs.
   Tool usage is invisible to the team — only you see the results.

**2. ax send** — how you talk to the team DURING work.
   `ax` is on your PATH. Use `ax send "message" --skip-ax` to post a message as yourself.
   Use this ONLY for: one ack when you start, delegating to another agent, or escalating to a human.
   Maximum 2 messages per task. Never @mention yourself. Only @mention others.

**3. Your final text** — your response when you're done.
   When you stop calling tools and write plain text, that text gets posted as your reply
   automatically. This is how you report what you did. Write it once — don't also send it
   via ax send.

That's it. Tools for work, ax send for team coordination, final text for your response.

## How to Do a Task

```
1. ax send "On it — implementing X" --skip-ax       # ack (1 message)
2. Create a worktree:                                 # ALWAYS use a worktree
   bash: /home/ax-agent/agents/tools/bootstrap_worktree.sh <repo> <your-name> <id8> <slug>
3. cd into the worktree, read the code you need       # tools
4. Write your changes                                 # tools
5. Validate: bash python3 -m py_compile / npm test    # tools
6. bash: git add + git commit + git push              # tools
7. bash: gh pr create --title "..." --body "..."      # tools (if ready)
8. Write your final text:                             # response
   "Pushed branch X. PR #206 open. Changed uploads.py. Validated with py_compile."
```

**ALWAYS work in a worktree, never in the shared repo root.** Other agents may be
working in the same repo. Use `/home/ax-agent/agents/tools/bootstrap_worktree.sh`
to create a clean worktree from the correct base branch.

Implement first, explore second. Commit before you run out of turns. Ship code, not promises.

## When the Task Is Too Big

If you can't finish in one go: commit and push what you have, then continue or hand off.

**Self-continuation** — mention yourself to keep going:
```
bash: git add -A && git commit -m "feat: add endpoint (wip)" && git push -u origin my-branch
bash: ax send "@backend_sentinel continue on branch my-branch: run tests and create PR" --skip-ax
```
Your final text: "Pushed WIP to my-branch. Continuing with tests."

You won't respond to your own message (the system prevents that), but another agent or
aX will see it and route it back to you, which wakes you up for the next step.

**Hand off to another agent:**
```
bash: ax send "@frontend_sentinel I pushed API changes on branch X — can you add the UI?" --skip-ax
```

**Escalate to a human:**
```
bash: ax send "@madtank need your input: should this require auth?" --skip-ax
```

## Hard Rules

- **Max 2 ax send messages per task.** One ack, one handoff/continuation. That's it.
- **Don't duplicate your response.** Your final text is posted automatically. Don't also ax send it.
- **Don't say "I will..." as your final text.** Do the work or say what's left. No promises.
- **Commit before exploring.** Working code in a branch beats a perfect plan in your head.
- **NEVER include tool output in your final text.** No file contents, no code dumps, no
  `[tool:...]` blocks, no bash output, no line numbers. Your final message must read like
  a clean status update a human would write: what you did, what branch, what PR. If someone
  sees raw code or file contents in your response, you failed.
- **Stay on your assignment.** If you were told to implement X, do X. Don't context-switch
  to investigate Y because another message arrived. Finish and push X first.

## Tools Reference

| Tool | What it does |
|------|-------------|
| `read_file` | Read a file with line numbers. Use offset/limit for large files. |
| `write_file` | Create or overwrite a file. Creates parent dirs. |
| `edit_file` | Find-and-replace exact text in a file. |
| `bash` | Run any command — git, tests, builds, ax CLI, curl. 120s timeout. |
| `grep` | Search file contents with regex (ripgrep). |
| `glob_files` | Find files by pattern (e.g. `**/*.py`). |

## ax CLI Quick Reference

```
ax send "msg" --skip-ax     # post a message as yourself
ax messages list --limit 10  # recent messages
ax tasks list                # check tasks
ax agents list               # see the team
```

## Shared Tools

All at `/home/ax-agent/agents/tools/`:
- `bootstrap_worktree.sh <repo> <owner> <id8> <slug>` — create clean task worktree
- `build_service.sh <service> [--context <path>]` — build Docker for dev server (api, dispatch, mcp, space_agent, frontend)
- `disk_status.sh` — check disk and Docker space usage
- `prune_disk.sh [--execute]` — clean up dangling images, build cache, stale worktrees
- `validate_repo.sh` — run repo validation

**Before building Docker, always run `disk_status.sh` first.** If disk is >85%, run `prune_disk.sh --execute`.

## Repos

All at `/home/ax-agent/shared/repos/`: ax-backend, ax-frontend, ax-mcp-server, ax-cli, ax-agents, ax-infrastructure.
Your workspace is your workdir — write freely there. Shared repos: read freely, write to task branches only.
"""


def _read_token_file(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _load_auth_json_token() -> str:
    """Load access_token from ~/.codex/auth.json (ChatGPT OAuth)."""
    try:
        data = json.loads(CODEX_AUTH_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get("tokens", {}).get("access_token", "")).strip()


def _oauth_token_candidates() -> list[tuple[str, str]]:
    """Return candidate auth tokens in priority order, de-duplicated."""
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    now = time.time()

    def add(source: str, token: str) -> None:
        token = token.strip()
        if not token or token in seen or _is_token_blocked(token, now):
            return
        seen.add(token)
        candidates.append((source, token))

    add("env:AX_CODEX_TOKEN", os.environ.get("AX_CODEX_TOKEN", ""))

    token_file_override = os.environ.get("AX_CODEX_TOKEN_FILE", "").strip()
    if token_file_override:
        add(f"file:{token_file_override}", _read_token_file(Path(token_file_override).expanduser()))

    add(f"file:{CODEX_SHARED_TOKEN_PATH}", _read_token_file(CODEX_SHARED_TOKEN_PATH))
    add(f"file:{CODEX_AUTH_PATH}", _load_auth_json_token())
    return candidates


def _is_auth_error(error: Exception) -> bool:
    error_str = str(error).lower()
    auth_markers = (
        "token_expired",
        "provided authentication token is expired",
        "oauth token has expired",
        "authentication_error",
        "401",
    )
    return any(marker in error_str for marker in auth_markers)


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _is_token_blocked(token: str, now: float | None = None) -> bool:
    until = _BLOCKED_TOKENS.get(_token_fingerprint(token))
    return bool(until and until > (now or time.time()))


def _block_token(token: str) -> None:
    _BLOCKED_TOKENS[_token_fingerprint(token)] = time.time() + TOKEN_BLOCK_SECONDS


def _unblock_token(token: str) -> None:
    _BLOCKED_TOKENS.pop(_token_fingerprint(token), None)


def _get_client(token: str):
    """Create an OpenAI client using ChatGPT subscription OAuth."""
    from openai import OpenAI
    return OpenAI(api_key=token, base_url=CODEX_BASE_URL)


@register("openai_sdk")
class OpenAISDKRuntime(BaseRuntime):
    """Runs agent turns via OpenAI Python SDK with ChatGPT OAuth.

    Uses the responses API with tool_use for the agent loop.
    Streams text back via StreamCallback.
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
        from tools import TOOL_DEFINITIONS, execute_tool

        cb = stream_cb or StreamCallback()
        model = model or "gpt-5.4"
        base_instructions = system_prompt or "You are a helpful coding assistant."
        instructions = SDK_PREAMBLE + "\n\n" + base_instructions

        # Build conversation from session history or start fresh
        extra = extra_args or {}
        history: list[dict] = list(extra.get("history", []))
        history.append({"role": "user", "content": message})

        final_text = ""  # User-visible reply only; tool transcript stays internal
        tool_count = 0
        files_written = []
        start_time = time.time()
        max_turns = 25  # Safety limit on agent loop iterations
        active_token_source: str | None = None

        for turn in range(max_turns):
            log.info(f"openai_sdk: turn {turn + 1}, {len(history)} messages")

            stream = None
            last_error: Exception | None = None
            token_candidates = _oauth_token_candidates()

            for token_source, token in token_candidates:
                try:
                    if token_source != active_token_source:
                        log.info(f"openai_sdk: auth source={token_source}")
                        active_token_source = token_source
                    client = _get_client(token)
                    stream = client.responses.create(
                        model=model,
                        instructions=instructions,
                        input=history,
                        tools=TOOL_DEFINITIONS,
                        store=False,
                        stream=True,
                    )
                    _unblock_token(token)
                    break
                except Exception as e:
                    last_error = e
                    if _is_auth_error(e):
                        _block_token(token)
                        log.warning(
                            f"openai_sdk: auth failed using {token_source}; "
                            "trying next token source"
                        )
                        continue
                    break

            if stream is None:
                e = last_error or RuntimeError("No OpenAI auth token available")
                error_str = str(e)
                log.error(f"API error: {error_str}")

                # Rate limit detection — backoff silently, don't post error to chat
                is_rate_limit = "429" in error_str or "rate" in error_str.lower() or "usage_limit" in error_str.lower()
                if is_rate_limit:
                    log.warning("Rate limited — backing off, will NOT post error to chat")
                    return RuntimeResult(
                        text="",  # Empty — don't pollute chat with 429 errors
                        history=history,
                        tool_count=tool_count,
                        files_written=files_written,
                        exit_reason="rate_limited",
                        elapsed_seconds=int(time.time() - start_time),
                    )

                # Other API errors — report but keep it clean
                if not final_text:
                    if _is_auth_error(e):
                        final_text = "Agent could not authenticate with the OpenAI runtime."
                    else:
                        final_text = "Agent encountered an API error and could not complete the task."
                return RuntimeResult(
                    text=final_text,
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="crashed",
                    elapsed_seconds=int(time.time() - start_time),
                )

            # Process the streamed response
            turn_text = ""
            tool_calls = []
            current_fn_name = ""
            current_fn_args = ""
            current_call_id = ""

            for event in stream:
                etype = getattr(event, "type", "")

                # Buffer text for this turn only. We only publish completed
                # assistant text after we know the turn did not pivot into
                # tool calls, which keeps raw/tool-adjacent chatter out of chat.
                if etype == "response.output_text.delta":
                    delta = event.delta
                    turn_text += delta

                # Function call building
                elif etype == "response.function_call_arguments.delta":
                    current_fn_args += event.delta

                elif etype == "response.output_item.added":
                    item = event.item
                    if getattr(item, "type", "") == "function_call":
                        current_fn_name = item.name
                        current_fn_args = ""
                        current_call_id = item.call_id

                elif etype == "response.output_item.done":
                    item = event.item
                    if getattr(item, "type", "") == "function_call":
                        tool_calls.append({
                            "call_id": item.call_id,
                            "name": item.name,
                            "arguments": item.arguments,
                        })
                        current_fn_name = ""
                        current_fn_args = ""

                elif etype == "response.completed":
                    pass  # End of response

            # If there were tool calls, execute them and continue the loop
            if tool_calls:
                tool_results_text = []
                if turn_text:
                    tool_results_text.append(f"[assistant] {turn_text}")

                # Keep tool activity internal to the runtime/model context.
                # Visible progress is carried by processing/tool_call signals.
                for tc in tool_calls:
                    tool_count += 1
                    try:
                        args = json.loads(tc["arguments"])
                    except json.JSONDecodeError:
                        args = {}

                    # Summarize tool call for display
                    tool_summary = _tool_display(tc["name"], args)

                    log.info(f"Tool: {tc['name']}({json.dumps(args)[:80]})")
                    cb.on_tool_start(tc["name"], tool_summary)
                    result = execute_tool(tc["name"], args, workdir)

                    if tc["name"] == "write_file" and not result.is_error:
                        files_written.append(args.get("path", ""))

                    summary = result.output[:200] if len(result.output) > 200 else result.output
                    cb.on_tool_end(tc["name"], summary)

                    # Cap tool output for model context
                    output = result.output[:10000]
                    err_flag = " [ERROR]" if result.is_error else ""
                    tool_results_text.append(
                        f"[tool:{tc['name']}]{err_flag}\n{output}"
                    )

                # Collapse tool interaction into text messages for Codex endpoint
                history.append({
                    "role": "assistant",
                    "content": "\n\n".join(tool_results_text),
                })
                turns_left = max_turns - turn - 1
                if turns_left <= 3:
                    nudge = (f"Tool results above. You have {turns_left} turns left. "
                             "COMMIT and PUSH your work NOW, then write your final summary. "
                             "Do NOT call ax send again. "
                             "IMPORTANT: Your final text must be a CLEAN status update — "
                             "no tool output, no file contents, no code, no [tool:...] blocks. "
                             "Just: what you did, branch name, PR number, validation result.")
                else:
                    nudge = ("Tool results above. Continue working. "
                             "Do NOT call ax send — you already sent your ack. "
                             "Just use tools to do the work, then write your final answer. "
                             "Remember: your final text must be CLEAN — no tool output, no code dumps.")
                history.append({
                    "role": "user",
                    "content": nudge,
                })

                cb.on_status("thinking")

                # Continue to next turn (model sees tool results)
                continue

            # No tool calls this turn
            visible_turn_text = turn_text.strip()

            # First turn, no tools used at all?  The model likely just
            # acknowledged ("Sure, I'll work on it") without doing anything.
            # Signal "accepted" via SSE heartbeat, then re-prompt once to
            # force actual tool use.  Second text-only turn is accepted as
            # the real answer (the task may genuinely not need tools).
            if turn == 0 and tool_count == 0 and visible_turn_text:
                cb.on_status("accepted")  # SSE heartbeat: committed to work
                log.info("openai_sdk: text-only first turn — re-prompting for tool use")
                log.debug("openai_sdk: rejected ack text: %s", visible_turn_text[:200])
                history.append({
                    "role": "assistant",
                    "content": visible_turn_text,
                })
                history.append({
                    "role": "user",
                    "content": (
                        "You acknowledged the task but used zero tools. "
                        "That text will NOT be posted — it was captured as a heartbeat. "
                        "Now actually do the work: use read_file, bash, write_file, etc. "
                        "Do not respond with text again until you have used tools to "
                        "make real progress."
                    ),
                })
                continue  # Force another turn — go use tools

            # Genuine completion (either had tool use, or second text-only turn)
            if visible_turn_text:
                final_text = visible_turn_text
                cb.on_text_complete(final_text)
                history.append({
                    "role": "assistant",
                    "content": visible_turn_text,
                })
            break

        elapsed = int(time.time() - start_time)
        log.info(f"openai_sdk: done in {elapsed}s, {tool_count} tools, "
                 f"{len(final_text)} chars")

        return RuntimeResult(
            text=final_text,
            history=history,
            session_id=None,  # Session managed via history in extra_args
            tool_count=tool_count,
            files_written=files_written,
            exit_reason="done",
            elapsed_seconds=elapsed,
        )


def _tool_display(name: str, args: dict) -> str:
    """Human-readable one-liner for tool activity log."""
    if name == "read_file":
        p = args.get("path", "")
        return f"Read {p.split('/')[-1]}" if "/" in p else f"Read {p}"
    if name == "write_file":
        p = args.get("path", "")
        return f"Write {p.split('/')[-1]}" if "/" in p else f"Write {p}"
    if name == "edit_file":
        p = args.get("path", "")
        return f"Edit {p.split('/')[-1]}" if "/" in p else f"Edit {p}"
    if name == "bash":
        cmd = str(args.get("command", ""))[:60]
        return f"Run: {cmd}"
    if name == "grep":
        return f"Search: {args.get('pattern', '')}"
    if name == "glob_files":
        return f"Find: {args.get('pattern', '')}"
    return f"{name}"
