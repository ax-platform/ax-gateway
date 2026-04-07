"""ax keys — PAT key management."""

from typing import Optional

import httpx
import typer

from ..config import get_client
from ..output import JSON_OPTION, handle_error, print_json, print_table

app = typer.Typer(name="keys", help="API key management", no_args_is_help=True)


@app.command("create")
def create(
    name: str = typer.Option(..., "--name", help="Key name"),
    agent_id: Optional[list[str]] = typer.Option(
        None, "--scope-to-agent", help="Restrict this key to a specific agent UUID (repeatable)"
    ),
    as_json: bool = JSON_OPTION,
):
    """Create a new API key."""
    client = get_client()
    try:
        data = client.create_key(name, allowed_agent_ids=agent_id or None)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    if as_json:
        print_json(data)
    else:
        token = data.get("token") or data.get("key") or data.get("raw_token")
        typer.echo(f"Key created: {data.get('credential_id', data.get('id', ''))}")
        if token:
            typer.echo(f"Token: {token}")
        typer.echo("Save this token — it won't be shown again.")


@app.command("list")
def list_keys(as_json: bool = JSON_OPTION):
    """List all API keys."""
    client = get_client()
    try:
        data = client.list_keys()
    except httpx.HTTPStatusError as e:
        handle_error(e)
    keys = data if isinstance(data, list) else data.get("keys", [])
    if as_json:
        print_json(keys)
    else:
        print_table(
            ["Credential ID", "Name", "Scopes", "Allowed Agent IDs", "Last Used At", "Created At", "Revoked At"],
            keys,
            keys=["credential_id", "name", "scopes", "allowed_agent_ids", "last_used_at", "created_at", "revoked_at"],
        )


@app.command("revoke")
def revoke(credential_id: str = typer.Argument(..., help="Credential ID to revoke")):
    """Revoke an API key."""
    client = get_client()
    try:
        client.revoke_key(credential_id)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    typer.echo("Revoked.")


@app.command("rotate")
def rotate(
    credential_id: str = typer.Argument(..., help="Credential ID to rotate"),
    as_json: bool = JSON_OPTION,
):
    """Rotate an API key — issues new token, revokes old."""
    client = get_client()
    try:
        data = client.rotate_key(credential_id)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    if as_json:
        print_json(data)
    else:
        token = data.get("token") or data.get("key") or data.get("raw_token")
        if token:
            typer.echo(f"New token: {token}")
        typer.echo("Save this token — it won't be shown again.")
