"""ax tasks — create, list, get, update."""

from typing import Optional
from uuid import UUID

import httpx
import typer

from ..config import get_client, resolve_space_id
from ..output import JSON_OPTION, console, handle_error, print_json, print_kv, print_table

app = typer.Typer(name="tasks", help="Task operations", no_args_is_help=True)


def _agent_items(result: object) -> list[dict]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if not isinstance(result, dict):
        return []
    for key in ("agents", "items", "results"):
        items = result.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _agent_names(agent: dict) -> set[str]:
    names: set[str] = set()
    for key in ("name", "username", "handle", "display_name"):
        value = agent.get(key)
        if isinstance(value, str) and value.strip():
            names.add(value.strip().lower().removeprefix("@"))
    return names


def _resolve_assignee_id(client, assignee: str | None, *, space_id: str) -> str | None:
    if not assignee:
        return None

    candidate = assignee.strip()
    if not candidate:
        return None

    try:
        return str(UUID(candidate))
    except ValueError:
        pass

    handle = candidate.removeprefix("@").lower()
    try:
        agents_result = client.list_agents(space_id=space_id, limit=500)
    except httpx.HTTPStatusError as e:
        handle_error(e)

    matches = [agent for agent in _agent_items(agents_result) if handle in _agent_names(agent)]
    if not matches:
        typer.echo(f"Error: No visible agent found for assignment target '{assignee}'.", err=True)
        raise typer.Exit(1)
    if len(matches) > 1:
        typer.echo(f"Error: Assignment target '{assignee}' matched multiple agents. Use an agent UUID.", err=True)
        raise typer.Exit(1)

    agent_id = matches[0].get("id")
    if not agent_id:
        typer.echo(f"Error: Agent '{assignee}' did not include an id in the API response.", err=True)
        raise typer.Exit(1)
    return str(agent_id)


def _mention_prefix(mention: str | None) -> str:
    if not mention:
        return ""
    value = mention.strip()
    if not value:
        return ""
    return value if value.startswith("@") else f"@{value}"


@app.command("create")
def create(
    title: str = typer.Argument(..., help="Task title"),
    description: Optional[str] = typer.Option(None, "--description", help="Task description"),
    priority: str = typer.Option("medium", "--priority", help="Priority: low, medium, high, urgent"),
    assign_to: Optional[str] = typer.Option(
        None, "--assign-to", "--assign", help="Assign task to an agent (handle, @handle, or UUID)"
    ),
    notify: bool = typer.Option(
        True, "--notify/--no-notify", help="Send a message notifying the team about the new task"
    ),
    mention: Optional[str] = typer.Option(None, "--mention", help="@mention a user or agent in the task notification"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    as_json: bool = JSON_OPTION,
):
    """Create a task and optionally notify the team."""
    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)
    assignee_id = _resolve_assignee_id(client, assign_to, space_id=sid)
    try:
        data = client.create_task(
            sid,
            title,
            description=description,
            priority=priority,
            assignee_id=assignee_id,
        )
    except httpx.HTTPStatusError as e:
        handle_error(e)
    task = data.get("task", data)
    tid = str(task.get("id", ""))[:8]
    if as_json:
        print_json(task)
    else:
        console.print(f'[green]Created:[/green] "{task.get("title")}" (id={tid}…, priority={task.get("priority")})')

    if notify:
        try:
            prio = task.get("priority", "medium")
            prefix = _mention_prefix(mention or assign_to)
            msg = f"New task created: **{title}** (id: `{tid}…`, priority: {prio})"
            if prefix:
                msg = f"{prefix} {msg}"
            client.send_message(sid, msg)
            if not as_json:
                console.print("[dim]Team notified.[/dim]")
        except Exception:
            if not as_json:
                console.print("[yellow]Task created but team notification failed.[/yellow]")


@app.command("list")
def list_tasks(
    limit: int = typer.Option(20, "--limit", help="Max tasks to return"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    as_json: bool = JSON_OPTION,
):
    """List tasks."""
    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)
    try:
        data = client.list_tasks(limit=limit, space_id=sid)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    tasks = data if isinstance(data, list) else data.get("tasks", [])
    if as_json:
        print_json(tasks)
    else:
        print_table(
            ["ID", "Title", "Status", "Priority"],
            tasks,
            keys=["id", "title", "status", "priority"],
        )


@app.command("get")
def get(
    task_id: str = typer.Argument(..., help="Task ID"),
    as_json: bool = JSON_OPTION,
):
    """Get a single task."""
    client = get_client()
    try:
        data = client.get_task(task_id)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    if as_json:
        print_json(data)
    else:
        print_kv(data)


@app.command("update")
def update(
    task_id: str = typer.Argument(..., help="Task ID"),
    priority: Optional[str] = typer.Option(None, "--priority", help="New priority"),
    status: Optional[str] = typer.Option(None, "--status", help="New status"),
    as_json: bool = JSON_OPTION,
):
    """Update a task."""
    fields = {}
    if priority is not None:
        fields["priority"] = priority
    if status is not None:
        fields["status"] = status
    if not fields:
        typer.echo("Error: Provide at least one field to update (--priority, --status).", err=True)
        raise typer.Exit(1)
    client = get_client()
    try:
        data = client.update_task(task_id, **fields)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    if as_json:
        print_json(data)
    else:
        print_kv(data)
