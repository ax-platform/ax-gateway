"""ax credentials — programmatic credential management (AUTH-SPEC-001 §8).

Requires a user PAT (axp_u_) which exchanges for user_admin JWT.
All operations are API-first — same as what the UI does.
"""

import httpx
import typer

from ..config import get_client
from ..output import JSON_OPTION, console, handle_error, print_json

app = typer.Typer(name="credentials", help="Credential management (PATs, enrollment tokens)", no_args_is_help=True)


@app.command("issue-agent-pat")
def issue_agent_pat(
    agent: str = typer.Argument(..., help="Agent name or ID to bind PAT to"),
    name: str = typer.Option(None, "--name", "-n", help="Label for the PAT"),
    expires_days: int = typer.Option(90, "--expires", help="PAT lifetime in days"),
    audience: str = typer.Option("cli", "--audience", help="Target: cli, mcp, or both"),
    as_json: bool = JSON_OPTION,
):
    """Issue an agent-bound PAT (axp_a_). The token is shown once.

    \b
    Examples:
        ax credentials issue-agent-pat my-bot
        ax credentials issue-agent-pat my-bot --audience mcp
        ax credentials issue-agent-pat my-bot --name "prod-key" --expires 30 --audience both
    """
    client = get_client()

    # Resolve agent name to ID if needed
    import re

    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
    if uuid_re.match(agent):
        agent_id = agent
    else:
        try:
            agents = client.mgmt_list_agents()
            match = next((a for a in agents if a.get("name") == agent), None)
            if not match:
                console.print(f"[red]Agent '{agent}' not found.[/red]")
                raise typer.Exit(1)
            agent_id = match["id"]
        except httpx.HTTPStatusError as e:
            handle_error(e)

    try:
        data = client.mgmt_issue_agent_pat(
            agent_id,
            name=name,
            expires_in_days=expires_days,
            audience=audience,
        )
    except httpx.HTTPStatusError as e:
        handle_error(e)

    if as_json:
        print_json(data)
    else:
        console.print("\n[green]Agent PAT created[/green]")
        console.print(f"  Agent: {agent} ({agent_id[:12]}...)")
        console.print(f"  Expires: {data.get('expires_at', '?')[:10]}")
        console.print("\n[bold]Token (save now — shown once):[/bold]")
        console.print(f"  {data.get('token', '?')}")


@app.command("issue-enrollment")
def issue_enrollment(
    name: str = typer.Option(None, "--name", "-n", help="Label for the token"),
    expires_hours: int = typer.Option(1, "--expires", help="Enrollment window in hours"),
    audience: str = typer.Option("cli", "--audience", help="Target: cli, mcp, or both"),
    as_json: bool = JSON_OPTION,
):
    """Issue an enrollment token that creates + binds an agent on first use.

    \b
    Give this token to a new agent. They run:
        ax auth init --token axp_a_... --agent their-name

    The agent is created and bound automatically.
    """
    client = get_client()
    try:
        data = client.mgmt_issue_enrollment(
            name=name,
            expires_in_hours=expires_hours,
            audience=audience,
        )
    except httpx.HTTPStatusError as e:
        handle_error(e)

    if as_json:
        print_json(data)
    else:
        console.print("\n[green]Enrollment token created[/green]")
        console.print(f"  Expires: {data.get('expires_at', '?')[:19]}")
        console.print(f"  State: {data.get('lifecycle_state', '?')}")
        console.print("\n[bold]Token (save now — shown once):[/bold]")
        console.print(f"  {data.get('token', '?')}")
        console.print("\n[cyan]Give to new agent:[/cyan]")
        console.print(f"  ax auth init --token {data.get('token', 'TOKEN')[:12]}... --agent AGENT_NAME")


@app.command("revoke")
def revoke(
    credential_id: str = typer.Argument(..., help="Credential UUID to revoke"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Revoke a PAT immediately. Future exchanges are blocked."""
    if not yes:
        confirm = typer.confirm(f"Revoke credential {credential_id[:12]}...?")
        if not confirm:
            raise typer.Abort()

    client = get_client()
    try:
        client.mgmt_revoke_credential(credential_id)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    console.print(f"[red]Revoked:[/red] {credential_id}")


@app.command("list")
def list_credentials(as_json: bool = JSON_OPTION):
    """List all credentials you own."""
    client = get_client()
    try:
        creds = client.mgmt_list_credentials()
    except httpx.HTTPStatusError as e:
        handle_error(e)

    if as_json:
        print_json(creds)
    else:
        if not creds:
            console.print("[dim]No credentials found.[/dim]")
            return
        for c in creds:
            state = c.get("lifecycle_state", "?")
            color = "green" if state == "active" else "red" if state == "revoked" else "yellow"
            agent = c.get("bound_agent_id") or "none"
            if agent != "none":
                agent = agent[:12] + "..."
            console.print(f"  [{color}]{state:<10s}[/{color}] {c['key_id']}  agent={agent:<16s}  {c.get('name', '')}")
