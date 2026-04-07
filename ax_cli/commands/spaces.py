"""ax spaces — list, create, and manage spaces."""

from typing import Optional

import httpx
import typer

from ..config import get_client, resolve_space_id
from ..output import JSON_OPTION, console, handle_error, print_json, print_kv, print_table

app = typer.Typer(name="spaces", help="Space management", no_args_is_help=True)


@app.command("list")
def list_spaces(
    as_json: bool = JSON_OPTION,
):
    """List all spaces you belong to."""
    client = get_client()
    try:
        spaces = client.list_spaces()
    except httpx.HTTPStatusError as e:
        handle_error(e)
    if not isinstance(spaces, list):
        spaces = spaces.get("spaces", spaces.get("items", []))
    if as_json:
        print_json(spaces)
    else:
        print_table(
            ["ID", "Name", "Visibility", "Members"],
            spaces,
            keys=["id", "name", "visibility", "member_count"],
        )


@app.command("create")
def create(
    name: str = typer.Argument(..., help="Space name"),
    description: Optional[str] = typer.Option(None, "--description", "-d", help="Space description"),
    visibility: str = typer.Option("private", "--visibility", "-v", help="private, invite_only, or public"),
    as_json: bool = JSON_OPTION,
):
    """Create a new space."""
    client = get_client()
    try:
        result = client.create_space(name, description=description, visibility=visibility)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    space = result.get("space", result) if isinstance(result, dict) else result
    if as_json:
        print_json(space)
    else:
        console.print(
            f"[green]Created:[/green] {space.get('name')} (id={str(space.get('id', ''))[:8]}…, visibility={space.get('visibility')})"
        )


@app.command("get")
def get_space(
    space_id: str = typer.Argument(..., help="Space ID"),
    as_json: bool = JSON_OPTION,
):
    """Get space details."""
    client = get_client()
    try:
        data = client.get_space(space_id)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    if as_json:
        print_json(data)
    else:
        print_kv(data)


@app.command("members")
def members(
    space_id: Optional[str] = typer.Argument(None, help="Space ID (default: current space)"),
    as_json: bool = JSON_OPTION,
):
    """List members of a space."""
    client = get_client()
    sid = space_id or resolve_space_id(client)
    try:
        data = client.list_space_members(sid)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    members_list = data if isinstance(data, list) else data.get("members", [])
    if as_json:
        print_json(members_list)
    else:
        print_table(
            ["User", "Role"],
            members_list,
            keys=["username", "role"],
        )
