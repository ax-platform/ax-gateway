"""ax tasks — create, list, get, update."""

from typing import Optional

import httpx
import typer

from ..config import get_client, resolve_space_id
from ..output import JSON_OPTION, console, handle_error, print_json, print_kv, print_table

app = typer.Typer(name="tasks", help="Task operations", no_args_is_help=True)


@app.command("create")
def create(
    title: str = typer.Argument(..., help="Task title"),
    description: Optional[str] = typer.Option(None, "--description", help="Task description"),
    priority: str = typer.Option("medium", "--priority", help="Priority: low, medium, high, urgent"),
    assign_to: Optional[str] = typer.Option(None, "--assign-to", help="Assign task to an agent (agent UUID)"),
    notify: bool = typer.Option(
        True, "--notify/--no-notify", help="Send a message notifying the team about the new task"
    ),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    as_json: bool = JSON_OPTION,
):
    """Create a task and optionally notify the team."""
    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)
    try:
        data = client.create_task(
            sid,
            title,
            description=description,
            priority=priority,
            agent_id=assign_to,
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
            msg = f"New task created: **{title}** (id: `{tid}…`, priority: {prio})"
            client.send_message(sid, msg)
            if not as_json:
                console.print("[dim]Team notified.[/dim]")
        except Exception:
            if not as_json:
                console.print("[yellow]Task created but team notification failed.[/yellow]")


@app.command("list")
def list_tasks(
    limit: int = typer.Option(20, "--limit", help="Max tasks to return"),
    as_json: bool = JSON_OPTION,
):
    """List tasks."""
    client = get_client()
    try:
        data = client.list_tasks(limit=limit)
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
