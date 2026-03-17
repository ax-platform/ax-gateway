"""ax keys — PAT key management.

PATs are user credentials. An agent-bound PAT acts as the agent when used
with the X-Agent-Id header. User owns the token; agent scope limits where
it can be used.
"""
from typing import Optional

import typer
import httpx

from ..config import get_client
from ..output import JSON_OPTION, print_json, print_table, handle_error

app = typer.Typer(name="keys", help="API key management", no_args_is_help=True)


@app.command("create")
def create(
    name: str = typer.Option(..., "--name", help="Key name"),
    agent_id: Optional[list[str]] = typer.Option(None, "--agent-id", help="Bind to agent UUID (repeatable)"),
    agent: Optional[str] = typer.Option(None, "--agent", help="Bind to agent by name (resolves to UUID)"),
    as_json: bool = JSON_OPTION,
):
    """Create a new API key (PAT).

    Without --agent-id or --agent: unrestricted user PAT.
    With --agent-id or --agent: agent-bound PAT (acts as the agent).

    Examples:
        ax keys create --name "my-key"
        ax keys create --name "orion-key" --agent orion
        ax keys create --name "multi" --agent-id <uuid1> --agent-id <uuid2>
    """
    client = get_client()

    # Resolve --agent name to UUID if provided
    bound_ids = list(agent_id) if agent_id else []
    if agent:
        try:
            agents_data = client.list_agents()
            agents_list = agents_data if isinstance(agents_data, list) else agents_data.get("agents", [])
            match = next((a for a in agents_list if a.get("name", "").lower() == agent.lower()), None)
            if not match:
                typer.echo(f"Error: Agent '{agent}' not found in this space.", err=True)
                raise typer.Exit(1)
            bound_ids.append(str(match["id"]))
        except httpx.HTTPStatusError as e:
            handle_error(e)

    try:
        data = client.create_key(name, allowed_agent_ids=bound_ids or None)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    if as_json:
        print_json(data)
    else:
        token = data.get("token") or data.get("key") or data.get("raw_token")
        cred_id = data.get("credential_id", data.get("id", ""))
        typer.echo(f"Key created: {cred_id}")
        if bound_ids:
            typer.echo(f"Bound to: {', '.join(bound_ids)}")
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
