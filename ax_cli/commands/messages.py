"""ax messages — send, list, get, edit, delete, search."""
import time
from typing import Optional

import typer
import httpx

from ..config import get_client, resolve_space_id, resolve_agent_name
from ..output import JSON_OPTION, print_json, print_table, print_kv, handle_error, console

app = typer.Typer(name="messages", help="Message operations", no_args_is_help=True)


def _print_wait_status(remaining: int, last_remaining: int | None) -> int:
    if remaining != last_remaining:
        console.print(f"  [dim]waiting for aX... ({remaining}s remaining)[/dim]", end="\r")
    return remaining


def _matching_reply(message_id: str, payload, seen_ids: set[str]) -> tuple[dict | None, bool]:
    routing_announced = False

    for reply in payload:
        rid = reply.get("id", "")
        if not rid:
            continue

        matches_thread = reply.get("parent_id") == message_id or reply.get("conversation_id") == message_id
        if not matches_thread:
            continue

        if rid in seen_ids:
            continue
        seen_ids.add(rid)

        metadata = reply.get("metadata", {}) or {}
        routing = metadata.get("routing", {})
        if routing.get("mode") == "ax_relay":
            target = routing.get("target_agent_name", "specialist")
            console.print(" " * 60, end="\r")
            console.print(f"  [cyan]aX is routing to @{target}...[/cyan]")
            routing_announced = True
            continue

        console.print(" " * 60, end="\r")
        return reply, routing_announced

    return None, routing_announced


def _wait_for_reply_polling(
    client,
    message_id: str,
    *,
    deadline: float,
    seen_ids: set[str],
    poll_interval: float = 2.0,
) -> dict | None:
    """Poll for a reply as a fallback when SSE is unavailable."""
    last_remaining = None

    while time.time() < deadline:
        remaining = int(deadline - time.time())
        last_remaining = _print_wait_status(remaining, last_remaining)

        try:
            data = client.list_replies(message_id)
        except (httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadError):
            time.sleep(poll_interval)
            continue

        replies = data if isinstance(data, list) else data.get("messages", data.get("replies", []))
        reply, _ = _matching_reply(message_id, replies, seen_ids)
        if reply:
            return reply

        time.sleep(poll_interval)

    console.print(" " * 60, end="\r")
    return None


def _wait_for_reply(client, message_id: str, timeout: int = 60) -> dict | None:
    """Wait for a reply by polling list_replies."""
    deadline = time.time() + timeout
    seen_ids: set[str] = {message_id}

    return _wait_for_reply_polling(
        client,
        message_id,
        deadline=deadline,
        seen_ids=seen_ids,
        poll_interval=1.0,
    )


def _configure_send_identity(client, *, agent_name: str | None, agent_id: str | None) -> None:
    """Override default identity only when an explicit flag is passed.

    No flags → keep whatever get_client() resolved (works for both
    human PATs and agent-scoped PATs without stripping identity).
    """
    if agent_name:
        client.set_default_agent(agent_name=agent_name)
        return

    if agent_id:
        client.set_default_agent(agent_id=agent_id)
        return

    # No explicit override — keep default client identity.


@app.command("send")
def send(
    content: str = typer.Argument(..., help="Message content"),
    wait: bool = typer.Option(True, "--wait/--skip-ax", "-w", help="Wait for aX response (default: yes)"),
    timeout: int = typer.Option(60, "--timeout", "-t", help="Max seconds to wait for reply"),
    agent_id: Optional[str] = typer.Option(None, "--agent-id", help="Target agent"),
    agent_name: Optional[str] = typer.Option(None, "--agent", help="Send as agent (X-Agent-Name)"),
    channel: str = typer.Option("main", "--channel", help="Channel name"),
    parent: Optional[str] = typer.Option(None, "--parent", "--reply-to", "-r", help="Parent message ID (thread reply)"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    as_json: bool = JSON_OPTION,
):
    """Send a message and wait for aX's response by default. Use --skip-ax to send only."""
    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)

    # Sends default to the user. Agent identity is opt-in for this command.
    resolved_agent = resolve_agent_name(explicit=agent_name, client=client) if agent_name else None
    _configure_send_identity(client, agent_name=resolved_agent, agent_id=agent_id)

    try:
        data = client.send_message(
            sid, content, agent_id=agent_id, channel=channel, parent_id=parent,
        )
    except httpx.HTTPStatusError as e:
        handle_error(e)

    msg = data.get("message", data)
    msg_id = msg.get("id") or msg.get("message_id") or data.get("id")

    if not wait or not msg_id:
        if as_json:
            print_json(data)
        else:
            console.print(f"[green]Sent.[/green] id={msg_id}")
        return

    console.print(f"[green]Sent.[/green] id={msg_id}")
    reply = _wait_for_reply(client, msg_id, timeout=timeout)

    if reply:
        if as_json:
            print_json({"sent": data, "reply": reply})
        else:
            console.print(f"\n[bold cyan]aX:[/bold cyan] {reply.get('content', '')}")
    else:
        if as_json:
            print_json({"sent": data, "reply": None, "timeout": True})
        else:
            console.print(f"\n[yellow]No reply within {timeout}s. Check later: ax messages list[/yellow]")


@app.command("list")
def list_messages(
    limit: int = typer.Option(20, "--limit", help="Max messages to return"),
    channel: str = typer.Option("main", "--channel", help="Channel name"),
    agent_id: Optional[str] = typer.Option(None, "--agent-id", help="Target agent"),
    as_json: bool = JSON_OPTION,
):
    """List recent messages."""
    client = get_client()
    try:
        data = client.list_messages(limit=limit, channel=channel, agent_id=agent_id)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    messages = data if isinstance(data, list) else data.get("messages", [])
    if as_json:
        print_json(messages)
    else:
        for m in messages:
            c = str(m.get("content", ""))
            m["content_short"] = c[:60] + "..." if len(c) > 60 else c
        print_table(
            ["ID", "Sender", "Content", "Created At"],
            messages,
            keys=["id", "sender_handle", "content_short", "created_at"],
        )


@app.command("get")
def get(
    message_id: str = typer.Argument(..., help="Message ID"),
    as_json: bool = JSON_OPTION,
):
    """Get a single message."""
    client = get_client()
    try:
        data = client.get_message(message_id)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    if as_json:
        print_json(data)
    else:
        print_kv(data)


@app.command("edit")
def edit(
    message_id: str = typer.Argument(..., help="Message ID"),
    content: str = typer.Argument(..., help="New content"),
    as_json: bool = JSON_OPTION,
):
    """Edit a message."""
    client = get_client()
    try:
        data = client.edit_message(message_id, content)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    if as_json:
        print_json(data)
    else:
        print_kv(data)


@app.command("delete")
def delete(
    message_id: str = typer.Argument(..., help="Message ID"),
    as_json: bool = JSON_OPTION,
):
    """Delete a message."""
    client = get_client()
    try:
        client.delete_message(message_id)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    if as_json:
        print_json({"status": "deleted", "message_id": message_id})
    else:
        typer.echo("Deleted.")


@app.command("search")
def search(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(20, "--limit", help="Max results"),
    agent_id: Optional[str] = typer.Option(None, "--agent-id", help="Target agent"),
    as_json: bool = JSON_OPTION,
):
    """Search messages."""
    client = get_client()
    try:
        data = client.search_messages(query, limit=limit, agent_id=agent_id)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    results = data if isinstance(data, list) else data.get("results", data.get("messages", []))
    if as_json:
        print_json(results)
    else:
        for m in results:
            c = str(m.get("content", ""))
            m["content_short"] = c[:60] + "..." if len(c) > 60 else c
        print_table(
            ["ID", "Sender", "Content", "Created At"],
            results,
            keys=["id", "sender_handle", "content_short", "created_at"],
        )
