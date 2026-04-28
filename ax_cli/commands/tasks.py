"""ax tasks — create, list, get, update."""

import uuid
from typing import Any, Optional
from uuid import UUID

import httpx
import typer

from ..config import get_client, resolve_space_id
from ..output import JSON_OPTION, console, handle_error, mention_prefix, print_json, print_kv, print_table

app = typer.Typer(name="tasks", help="Task operations", no_args_is_help=True)
SPACE_OPTION = typer.Option(None, "--space", "--space-id", "-s", help="Target space id, slug, or name")


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


def _space_items(result: object) -> list[dict]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if not isinstance(result, dict):
        return []
    for key in ("spaces", "items", "results"):
        items = result.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _space_summary(client, space_id: str) -> dict[str, str]:
    try:
        spaces = _space_items(client.list_spaces())
    except Exception:
        return {"id": space_id, "label": space_id}

    for space in spaces:
        sid = str(space.get("id") or space.get("space_id") or "")
        if sid == space_id:
            slug = space.get("slug")
            name = space.get("name")
            label = str(slug or name or sid)
            result = {"id": sid, "label": label}
            if slug:
                result["slug"] = str(slug)
            if name:
                result["name"] = str(name)
            return result
    return {"id": space_id, "label": space_id}


def _annotate_task_space(task: dict[str, Any], space: dict[str, str]) -> dict[str, Any]:
    task.setdefault("space_id", space["id"])
    if space.get("slug"):
        task.setdefault("space_slug", space["slug"])
    if space.get("name"):
        task.setdefault("space_name", space["name"])
    return task


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


_mention_prefix = mention_prefix


def _task_signal_metadata(
    task: dict[str, Any],
    *,
    space_id: str,
    title: str,
    description: str | None,
    assignee_id: str | None,
    assignee_label: str | None,
) -> dict[str, Any]:
    task_id = str(task.get("id") or "")
    tool_call_id = f"task:{task_id}" if task_id else str(uuid.uuid4())
    priority = str(task.get("priority") or "medium")
    status = str(task.get("status") or "open")
    summary = description or task.get("description") or f"Priority {priority} task created from axctl."
    task_item = dict(task)
    task_item.setdefault("title", title)
    task_item.setdefault("priority", priority)
    task_item.setdefault("status", status)
    if description and "description" not in task_item:
        task_item["description"] = description
    if assignee_id and "assignee_id" not in task_item:
        task_item["assignee_id"] = assignee_id

    assignee = None
    if assignee_id or assignee_label:
        assignee = {
            "id": assignee_id,
            "name": assignee_label.strip().removeprefix("@") if assignee_label else None,
        }

    card_payload: dict[str, Any] = {
        "title": title,
        "summary": summary,
        "task_id": task_id or None,
        "priority": priority,
        "status": status,
        "assignee": assignee,
        "source": "axctl_tasks_create",
        "delivery": "task_notification",
    }

    return {
        "ui": {
            "cards": [
                {
                    "card_id": f"task-signal:{task_id or tool_call_id}",
                    "type": "task",
                    "version": 1,
                    "payload": card_payload,
                }
            ],
            "widget": {
                "kind": "mcp_app",
                "tool_name": "tasks",
                "tool_action": "get" if task_id else "list",
                "tool_call_id": tool_call_id,
                "resource_uri": "ui://tasks/detail" if task_id else "ui://tasks/board",
                "display_mode": "inline",
                "lifecycle": "complete",
                "revision": 1,
                "title": "Task Detail" if task_id else "Task Board",
                "arguments": {
                    "action": "get" if task_id else "list",
                    "space_id": space_id,
                    "task_id": task_id or None,
                },
                "initial_data": {
                    "kind": "task",
                    "version": 1,
                    "action": "get" if task_id else "list",
                    "items": [task_item],
                    "count": 1,
                    "selected_task_id": task_id or None,
                    "space_id": space_id,
                    "source": "axctl_tasks_create",
                },
                "result_kind": "tasks",
                "source": "axctl_tasks_create",
            },
        },
        "app_signal": {
            "app": "tasks/detail" if task_id else "tasks",
            "resource_uri": "ui://tasks/detail" if task_id else "ui://tasks/board",
            "tool_call_id": tool_call_id,
            "task_id": task_id or None,
            "source": "axctl_tasks_create",
        },
    }


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
    space: Optional[str] = SPACE_OPTION,
    as_json: bool = JSON_OPTION,
):
    """Create a task and optionally notify the team."""
    client = get_client()
    sid = resolve_space_id(client, explicit=space)
    space_info = _space_summary(client, sid)
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
    except RuntimeError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    task = data.get("task", data)
    if isinstance(task, dict):
        task = _annotate_task_space(task, space_info)
    tid = str(task.get("id", ""))[:8]
    if as_json:
        print_json(task)
    else:
        console.print(
            f'[green]Created:[/green] "{task.get("title")}" '
            f"(id={tid}…, priority={task.get('priority')}) in {space_info['label']} ({sid})"
        )

    if notify:
        try:
            prio = task.get("priority", "medium")
            prefix = _mention_prefix(mention or assign_to)
            msg = f"New task created: **{title}** (id: `{tid}…`, priority: {prio}). Open the task card for details."
            if prefix:
                msg = f"{prefix} {msg}"
            client.send_message(
                sid,
                msg,
                metadata=_task_signal_metadata(
                    task,
                    space_id=sid,
                    title=title,
                    description=description,
                    assignee_id=assignee_id,
                    assignee_label=mention or assign_to,
                ),
                message_type="system",
            )
            if not as_json:
                console.print("[dim]Team notified.[/dim]")
        except Exception:
            if not as_json:
                console.print("[yellow]Task created but team notification failed.[/yellow]")


@app.command("list")
def list_tasks(
    limit: int = typer.Option(20, "--limit", help="Max tasks to return"),
    space: Optional[str] = SPACE_OPTION,
    as_json: bool = JSON_OPTION,
):
    """List tasks."""
    client = get_client()
    sid = resolve_space_id(client, explicit=space)
    space_info = _space_summary(client, sid)
    try:
        data = client.list_tasks(limit=limit, space_id=sid)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    tasks = data if isinstance(data, list) else data.get("tasks", [])
    if as_json:
        if isinstance(tasks, list):
            for task in tasks:
                if isinstance(task, dict):
                    _annotate_task_space(task, space_info)
        print_json(tasks)
    else:
        console.print(f"[dim]Space: {space_info['label']} ({sid})[/dim]")
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
