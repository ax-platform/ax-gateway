"""OpenAI SDK runtime — uses ChatGPT OAuth subscription via Codex endpoint."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from . import BaseRuntime, RuntimeResult, StreamCallback, register

log = logging.getLogger("runtime.openai_sdk")

CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"
# NOTE: This is ChatGPT's internal Codex API, not a public OpenAI endpoint.
# It may change without notice. Auth is via ChatGPT OAuth (Plus/Pro subscription).
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"

# Preamble injected into system prompt for SDK runtimes.
# The model should produce text output — the runtime handles posting to aX.
SDK_PREAMBLE = """
IMPORTANT RUNTIME RULES:
- Your text output IS your reply. It will be posted to the chat automatically.
- Do NOT use bash to call `ax send` or any messaging CLI — that creates duplicate messages.
- Use the provided tools (read_file, write_file, edit_file, bash, grep, glob_files) for work.
- When you're done, just write your answer as text. It will be delivered to the user.
"""


def _load_oauth_token() -> str:
    """Load access_token from ~/.codex/auth.json (ChatGPT OAuth).

    Checks expiry and fails fast if the token is expired rather than
    silently producing 401s mid-execution.
    """
    if not CODEX_AUTH_PATH.exists():
        raise RuntimeError(
            f"OAuth token not found at {CODEX_AUTH_PATH}. "
            "Run 'codex' once to authenticate via ChatGPT OAuth."
        )
    data = json.loads(CODEX_AUTH_PATH.read_text())
    token = data["tokens"]["access_token"]
    expires_at = data.get("tokens", {}).get("expires_at")
    if expires_at and time.time() > expires_at:
        raise RuntimeError(
            f"OAuth token expired at {time.strftime('%Y-%m-%d %H:%M', time.localtime(expires_at))}. "
            "Run 'codex' once to refresh via ChatGPT OAuth."
        )
    return token


def _get_client():
    """Create an OpenAI client using ChatGPT subscription OAuth."""
    from openai import OpenAI
    token = _load_oauth_token()
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

        client = _get_client()
        cb = stream_cb or StreamCallback()
        model = model or "gpt-5.4"
        base_instructions = system_prompt or "You are a helpful coding assistant."
        instructions = SDK_PREAMBLE + "\n\n" + base_instructions

        # Build conversation from session history or start fresh
        extra = extra_args or {}
        history: list[dict] = extra.get("history", [])
        history.append({"role": "user", "content": message})

        accumulated_text = ""  # Running log of all activity
        tool_count = 0
        files_written = []
        start_time = time.time()
        max_turns = 25  # Safety limit on agent loop iterations

        for turn in range(max_turns):
            log.info(f"openai_sdk: turn {turn + 1}, {len(history)} messages")

            try:
                stream = client.responses.create(
                    model=model,
                    instructions=instructions,
                    input=history,
                    tools=TOOL_DEFINITIONS,
                    store=False,
                    stream=True,
                )
            except Exception as e:
                log.error(f"API error: {e}")
                if not accumulated_text:
                    accumulated_text = f"API error: {e}"
                return RuntimeResult(
                    text=accumulated_text,
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

                # Text streaming — append to running log
                if etype == "response.output_text.delta":
                    delta = event.delta
                    turn_text += delta
                    accumulated_text += delta
                    cb.on_text_delta(delta)

                # Function call building
                elif etype == "response.function_call_arguments.delta":
                    current_fn_args += event.delta

                elif etype == "response.output_item.added":
                    item = event.item
                    if getattr(item, "type", "") == "function_call":
                        current_fn_name = item.name
                        current_fn_args = ""
                        current_call_id = item.call_id
                        cb.on_tool_start(current_fn_name, f"Calling {current_fn_name}...")

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

                # Append tool activity to the running log so the user sees progress
                for tc in tool_calls:
                    tool_count += 1
                    try:
                        args = json.loads(tc["arguments"])
                    except json.JSONDecodeError:
                        args = {}

                    # Summarize tool call for display
                    tool_summary = _tool_display(tc["name"], args)

                    log.info(f"Tool: {tc['name']}({json.dumps(args)[:80]})")
                    result = execute_tool(tc["name"], args, workdir)

                    if tc["name"] == "write_file" and not result.is_error:
                        files_written.append(args.get("path", ""))

                    # Append tool step to running log
                    short_result = result.output[:300] if len(result.output) > 300 else result.output
                    err_tag = " **error**" if result.is_error else ""
                    step_line = f"\n\n▸ **{tool_summary}**{err_tag}\n```\n{short_result}\n```"
                    accumulated_text += step_line
                    cb.on_text_complete(accumulated_text)

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
                history.append({
                    "role": "user",
                    "content": "Tool results above. Continue working on the task. "
                               "If you're done, provide your final answer.",
                })

                # Add separator in running log before next turn's text
                accumulated_text += "\n\n---\n\n"
                cb.on_text_complete(accumulated_text)

                # Continue to next turn (model sees tool results)
                continue

            # No tool calls — we're done
            break

        elapsed = int(time.time() - start_time)
        log.info(f"openai_sdk: done in {elapsed}s, {tool_count} tools, "
                 f"{len(accumulated_text)} chars")

        return RuntimeResult(
            text=accumulated_text,
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
