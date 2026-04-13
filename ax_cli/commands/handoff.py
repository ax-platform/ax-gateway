"""ax handoff - send work to an agent and wait for a completion signal."""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any, Optional

import httpx
import typer

from ..config import get_client, resolve_agent_name, resolve_space_id
from ..output import JSON_OPTION, console, handle_error, print_json
from .watch import _iter_sse

INTENTS: dict[str, dict[str, str]] = {
    "general": {
        "label": "Handoff",
        "priority": "medium",
        "prompt": "@{agent} Handoff: {instructions}\n\n{context}\nPlease reply with `{token}` when you have a useful update or completion.",
    },
    "review": {
        "label": "Review",
        "priority": "medium",
        "prompt": "@{agent} Review request: {instructions}\n\n{context}\nPlease reply with `{token}` and include findings, risks, and any friction.",
    },
    "implement": {
        "label": "Implementation",
        "priority": "high",
        "prompt": "@{agent} Implementation handoff: {instructions}\n\n{context}\nPlease reply with `{token}` and include branch, files changed, and validation.",
    },
    "qa": {
        "label": "QA",
        "priority": "medium",
        "prompt": "@{agent} QA handoff: {instructions}\n\n{context}\nPlease reply with `{token}` and include pass/fail status, repro steps, and evidence.",
    },
    "status": {
        "label": "Status check",
        "priority": "medium",
        "prompt": "@{agent} Status check: {instructions}\n\n{context}\nPlease reply with `{token}` and the current state, blocker, or next step.",
    },
    "incident": {
        "label": "Incident",
        "priority": "urgent",
        "prompt": "@{agent} Incident handoff: {instructions}\n\n{context}\nPlease reply with `{token}` as soon as you have triage, mitigation, or a blocker.",
    },
}

COMPLETION_WORDS = (
    "done",
    "complete",
    "completed",
    "finished",
    "shipped",
    "pushed",
    "opened pr",
    "pull request",
    "reviewed",
    "pass",
    "fail",
    "blocked",
)


def _streaming_reply_state(message: dict[str, Any]) -> dict[str, Any]:
    metadata = message.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    streaming = metadata.get("streaming_reply")
    return streaming if isinstance(streaming, dict) else {}


def _is_handoff_progress(message: dict[str, Any]) -> bool:
    streaming = _streaming_reply_state(message)
    if streaming.get("final") is False:
        return True
    content = str(message.get("content") or "").lstrip()
    return content.startswith("Working")


def _progress_label(message: dict[str, Any]) -> str:
    content = str(message.get("content") or "").strip()
    if not content:
        return "Working..."
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    return " ".join(lines[:3])[:180]


def _message_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("messages", "replies", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _sender_name(message: dict[str, Any]) -> str:
    author = message.get("author")
    candidates: list[str] = [
        str(message.get("display_name") or ""),
        str(message.get("agent_name") or ""),
        str(message.get("sender_handle") or ""),
        str(message.get("username") or ""),
        str(message.get("sender") or ""),
    ]
    if isinstance(author, dict):
        candidates.extend(
            [
                str(author.get("name") or ""),
                str(author.get("username") or ""),
                str(author.get("agent_name") or ""),
            ]
        )
    elif isinstance(author, str):
        candidates.append(author)
    return next((candidate.strip() for candidate in candidates if candidate and candidate.strip()), "")


def _message_timestamp(message: dict[str, Any]) -> float | None:
    raw = message.get("created_at") or message.get("timestamp") or message.get("server_time")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def _agent_matches(sender: str, agent_name: str) -> bool:
    sender_norm = sender.strip().lower().lstrip("@")
    agent_norm = agent_name.strip().lower().lstrip("@")
    return sender_norm == agent_norm


def _is_completion(content: str, token: str) -> bool:
    text = content.lower()
    return token.lower() in text or any(word in text for word in COMPLETION_WORDS)


def _matches_handoff_reply(
    message: dict[str, Any],
    *,
    agent_name: str,
    sent_message_id: str,
    token: str,
    current_agent_name: str,
    started_at: float,
    require_completion: bool,
) -> bool:
    msg_id = str(message.get("id") or "")
    if not msg_id or msg_id == sent_message_id:
        return False

    timestamp = _message_timestamp(message)
    if timestamp is not None and timestamp < started_at:
        return False

    sender = _sender_name(message)
    if not _agent_matches(sender, agent_name):
        return False

    content = str(message.get("content") or "")
    thread_match = message.get("parent_id") == sent_message_id or message.get("conversation_id") == sent_message_id
    token_match = token in content
    mention_match = bool(current_agent_name and f"@{current_agent_name}".lower() in content.lower())

    if not (thread_match or token_match or mention_match):
        return False

    if _is_handoff_progress(message) and token.lower() not in content.lower():
        return False

    if require_completion and not _is_completion(content, token):
        return False

    return True


def _matches_handoff_progress(message: dict[str, Any], **kwargs) -> bool:
    progress_kwargs = dict(kwargs)
    progress_kwargs["require_completion"] = False

    msg_id = str(message.get("id") or "")
    if not msg_id or msg_id == progress_kwargs["sent_message_id"]:
        return False

    timestamp = _message_timestamp(message)
    if timestamp is not None and timestamp < progress_kwargs["started_at"]:
        return False

    sender = _sender_name(message)
    if not _agent_matches(sender, progress_kwargs["agent_name"]):
        return False

    content = str(message.get("content") or "")
    thread_match = (
        message.get("parent_id") == progress_kwargs["sent_message_id"]
        or message.get("conversation_id") == progress_kwargs["sent_message_id"]
    )
    token_match = progress_kwargs["token"] in content
    mention_match = bool(
        progress_kwargs["current_agent_name"] and f"@{progress_kwargs['current_agent_name']}".lower() in content.lower()
    )

    return (thread_match or token_match or mention_match) and _is_handoff_progress(message)


def _recent_match(client, *, space_id: str, on_progress=None, **kwargs) -> dict[str, Any] | None:
    """Check recent messages and direct replies to avoid missing fast responses."""
    candidates: list[dict[str, Any]] = []

    try:
        candidates.extend(_message_items(client.list_replies(kwargs["sent_message_id"])))
    except Exception:
        pass

    try:
        candidates.extend(_message_items(client.list_messages(limit=30, space_id=space_id)))
    except Exception:
        pass

    seen: set[str] = set()
    for message in candidates:
        msg_id = str(message.get("id") or "")
        if msg_id in seen:
            continue
        seen.add(msg_id)
        if _matches_handoff_reply(message, **kwargs):
            return message
        if on_progress and _matches_handoff_progress(message, **kwargs):
            on_progress(message)
            continue
    return None


def _wait_for_handoff_reply(
    client,
    *,
    space_id: str,
    agent_name: str,
    sent_message_id: str,
    token: str,
    current_agent_name: str,
    started_at: float,
    timeout: int,
    require_completion: bool,
) -> dict[str, Any] | None:
    kwargs = {
        "agent_name": agent_name,
        "sent_message_id": sent_message_id,
        "token": token,
        "current_agent_name": current_agent_name,
        "started_at": started_at,
        "require_completion": require_completion,
    }
    last_progress = ""

    def on_progress(message: dict[str, Any]) -> None:
        nonlocal last_progress
        label = _progress_label(message)
        if label and label != last_progress:
            last_progress = label
            console.print(f"[dim]@{agent_name}: {label}[/dim]")

    match = _recent_match(client, space_id=space_id, on_progress=on_progress, **kwargs)
    if match:
        return match

    deadline = time.time() + timeout
    console.print(f"[dim]Watching @{agent_name} via SSE for up to {timeout}s...[/dim]")

    while timeout <= 0 or time.time() < deadline:
        remaining = max(1, deadline - time.time()) if timeout > 0 else 15
        read_timeout = min(10, remaining) if timeout > 0 else 10
        try:
            with client.connect_sse(
                space_id=space_id,
                timeout=httpx.Timeout(connect=10, read=read_timeout, write=10, pool=10),
            ) as response:
                if response.status_code != 200:
                    console.print(
                        f"[yellow]SSE unavailable ({response.status_code}); falling back to recent messages.[/yellow]"
                    )
                    return _recent_match(client, space_id=space_id, on_progress=on_progress, **kwargs)

                for event_type, data in _iter_sse(response):
                    if timeout > 0 and time.time() > deadline:
                        break
                    if event_type not in ("message", "mention") or not isinstance(data, dict):
                        continue
                    if _matches_handoff_reply(data, **kwargs):
                        return data
                    if _matches_handoff_progress(data, **kwargs):
                        on_progress(data)
                        continue
        except httpx.ReadTimeout:
            match = _recent_match(client, space_id=space_id, on_progress=on_progress, **kwargs)
            if match:
                return match
            continue
        except (httpx.ConnectError, httpx.ReadError):
            match = _recent_match(client, space_id=space_id, on_progress=on_progress, **kwargs)
            if match:
                return match
            time.sleep(1)
        except KeyboardInterrupt:
            raise typer.Exit(1)

    return _recent_match(client, space_id=space_id, on_progress=on_progress, **kwargs)


def _resolve_agent_id(client, agent_name: str) -> str | None:
    try:
        data = client.list_agents()
    except Exception:
        return None
    agents = data if isinstance(data, list) else data.get("agents", data.get("items", []))
    if not isinstance(agents, list):
        return None
    target = agent_name.lower().lstrip("@")
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        candidates = [
            str(agent.get("id") or ""),
            str(agent.get("name") or ""),
            str(agent.get("username") or ""),
            str(agent.get("handle") or ""),
            str(agent.get("agent_name") or ""),
        ]
        if any(candidate.lower().lstrip("@") == target for candidate in candidates if candidate):
            return str(agent.get("id") or "")
    return None


def _interactive_follow_up_loop(
    client,
    *,
    space_id: str,
    agent_name: str,
    current_agent_name: str,
    timeout: int,
    token: str,
    reply: dict[str, Any],
) -> None:
    """Prompt for threaded follow-ups after a watched handoff reply."""
    parent_id = str(reply.get("id") or "")
    if not parent_id:
        return

    while True:
        choice = typer.prompt("Next action: [r]eply, [e]xit, [n]o reply", default="e").strip().lower()
        if choice in {"e", "exit", "q", "quit"}:
            console.print("[dim]Exited follow-up mode.[/dim]")
            return
        if choice in {"n", "no", "no-reply", "no reply", "skip"}:
            console.print("[dim]No follow-up sent.[/dim]")
            return
        if choice not in {"r", "reply"}:
            console.print("[yellow]Choose r, e, or n.[/yellow]")
            continue

        content = typer.prompt(f"Reply to @{agent_name}").strip()
        if not content:
            console.print("[yellow]Empty reply skipped.[/yellow]")
            continue

        started_at = time.time()
        try:
            sent_data = client.send_message(space_id, content, parent_id=parent_id)
        except httpx.HTTPStatusError as exc:
            handle_error(exc)
            return

        sent = sent_data.get("message", sent_data)
        sent_message_id = str(sent.get("id") or sent_data.get("id") or "")
        console.print(f"[green]Follow-up sent:[/green] {sent_message_id}")
        if not sent_message_id:
            return

        next_reply = _wait_for_handoff_reply(
            client,
            space_id=space_id,
            agent_name=agent_name,
            sent_message_id=sent_message_id,
            token=token,
            current_agent_name=current_agent_name,
            started_at=started_at,
            timeout=timeout,
            require_completion=False,
        )
        if not next_reply:
            console.print(f"[yellow]No @{agent_name} follow-up reply within {timeout}s.[/yellow]")
            return

        parent_id = str(next_reply.get("id") or parent_id)
        console.print(f"[green]Reply received from @{agent_name}.[/green]")
        console.print(str(next_reply.get("content") or ""))


def _task_id(task_data: dict[str, Any]) -> str:
    task = task_data.get("task", task_data)
    return str(task.get("id") or "")


def run(
    agent: str = typer.Argument(..., help="Target agent (@name or name)"),
    instructions: str = typer.Argument(..., help="What the agent should do"),
    intent: str = typer.Option(
        "general",
        "--intent",
        "-i",
        help="Intent: general, review, implement, qa, status, incident",
    ),
    timeout: int = typer.Option(300, "--timeout", "-t", help="Seconds to wait for a reply"),
    priority: Optional[str] = typer.Option(None, "--priority", help="Task priority override"),
    create_task: bool = typer.Option(True, "--task/--no-task", help="Create a task for the handoff"),
    watch: bool = typer.Option(True, "--watch/--no-watch", help="Wait for the target agent response"),
    require_completion: bool = typer.Option(
        False,
        "--require-completion",
        help="Wait only for replies that include the handoff token or completion language",
    ),
    nudge: bool = typer.Option(False, "--nudge/--no-nudge", help="Send one nudge if the first wait times out"),
    follow_up: bool = typer.Option(
        False,
        "--follow-up/--no-follow-up",
        help="After a reply, prompt to send threaded follow-ups until exit.",
    ),
    space_id: Optional[str] = typer.Option(None, "--space-id", "-s", help="Override default space"),
    as_json: bool = JSON_OPTION,
):
    """Hand work to an agent: create task, send message, watch SSE, and return the result."""
    normalized_intent = intent.strip().lower()
    if normalized_intent not in INTENTS:
        allowed = ", ".join(sorted(INTENTS))
        typer.echo(f"Error: unknown intent '{intent}'. Use one of: {allowed}", err=True)
        raise typer.Exit(1)

    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)
    agent_name = agent.lstrip("@")
    current_agent_name = resolve_agent_name(client=client) or ""
    spec = INTENTS[normalized_intent]
    task_priority = priority or spec["priority"]
    handoff_id = f"handoff:{uuid.uuid4().hex[:8]}"
    target_agent_id = _resolve_agent_id(client, agent_name)

    task_data: dict[str, Any] | None = None
    task_error: str | None = None
    task_id = ""
    if create_task:
        try:
            task_data = client.create_task(
                sid,
                f"{spec['label']}: {instructions[:100]}",
                description=f"{instructions}\n\nHandoff token: `{handoff_id}`",
                priority=task_priority,
                assignee_id=target_agent_id,
            )
            task_id = _task_id(task_data)
            console.print(
                f"[green]Task created:[/green] {task_id[:8]}..." if task_id else "[green]Task created.[/green]"
            )
            if target_agent_id:
                console.print(f"[dim]Assigned to @{agent_name} ({target_agent_id[:8]}...)[/dim]")
            else:
                console.print(f"[yellow]Could not resolve @{agent_name}; task created without assignee.[/yellow]")
        except httpx.HTTPStatusError as exc:
            task_error = str(exc)
            console.print(f"[yellow]Task creation failed; continuing with message handoff: {task_error}[/yellow]")
        except Exception as exc:
            task_error = str(exc)
            console.print(f"[yellow]Task creation failed; continuing with message handoff: {task_error}[/yellow]")

    context_parts = [f"Handoff token: `{handoff_id}`"]
    if task_id:
        context_parts.append(f"Task ID: `{task_id}`")
    context_parts.append(
        "Reply in this thread if possible; otherwise mention the sender and include the handoff token."
    )
    content = spec["prompt"].format(
        agent=agent_name, instructions=instructions, context="\n".join(context_parts), token=handoff_id
    )

    started_at = time.time()
    try:
        sent_data = client.send_message(sid, content)
    except httpx.HTTPStatusError as exc:
        handle_error(exc)

    sent = sent_data.get("message", sent_data)
    sent_message_id = str(sent.get("id") or sent_data.get("id") or "")
    console.print(f"[green]Handoff sent:[/green] {sent_message_id}")

    if not watch or not sent_message_id:
        result = {
            "status": "sent",
            "intent": normalized_intent,
            "agent": agent_name,
            "handoff_id": handoff_id,
            "task": task_data,
            "task_error": task_error,
            "sent": sent_data,
            "reply": None,
        }
        if as_json:
            print_json(result)
        return

    reply = _wait_for_handoff_reply(
        client,
        space_id=sid,
        agent_name=agent_name,
        sent_message_id=sent_message_id,
        token=handoff_id,
        current_agent_name=current_agent_name,
        started_at=started_at,
        timeout=timeout,
        require_completion=require_completion,
    )

    if reply is None and nudge:
        nudge_content = (
            f"@{agent_name} Status nudge for `{handoff_id}`. "
            "Please reply in this thread with the current status or blocker."
        )
        try:
            client.send_message(sid, nudge_content, parent_id=sent_message_id)
            reply = _wait_for_handoff_reply(
                client,
                space_id=sid,
                agent_name=agent_name,
                sent_message_id=sent_message_id,
                token=handoff_id,
                current_agent_name=current_agent_name,
                started_at=started_at,
                timeout=timeout,
                require_completion=require_completion,
            )
        except Exception:
            pass

    status = "replied" if reply else "timeout"
    result = {
        "status": status,
        "intent": normalized_intent,
        "agent": agent_name,
        "agent_id": target_agent_id,
        "handoff_id": handoff_id,
        "task": task_data,
        "task_error": task_error,
        "sent": sent_data,
        "reply": reply,
    }

    if reply:
        console.print(f"[green]Reply received from @{agent_name}.[/green]")
        if as_json:
            print_json(result)
        else:
            console.print(str(reply.get("content") or ""))
            if follow_up:
                _interactive_follow_up_loop(
                    client,
                    space_id=sid,
                    agent_name=agent_name,
                    current_agent_name=current_agent_name,
                    timeout=timeout,
                    token=handoff_id,
                    reply=reply,
                )
    else:
        console.print(f"[yellow]No @{agent_name} reply within {timeout}s.[/yellow]")
        if as_json:
            print_json(result)
