"""ax credentials — programmatic credential management (AUTH-SPEC-001 §8).

Requires a user PAT (axp_u_) which exchanges for user_admin JWT.
All operations are API-first — same as what the UI does.
"""

import httpx
import typer

from ..config import get_client
from ..output import EXIT_NOT_OK, JSON_OPTION, console, handle_error, print_json, print_table

app = typer.Typer(name="credentials", help="Credential management (PATs, enrollment tokens)", no_args_is_help=True)


def _active_agent_credentials(credentials: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for credential in credentials:
        if credential.get("lifecycle_state") != "active":
            continue
        agent_id = credential.get("bound_agent_id")
        if not agent_id:
            continue
        grouped.setdefault(agent_id, []).append(credential)
    return grouped


def build_credential_audit(credentials: list[dict]) -> dict:
    """Build the non-destructive agent PAT hygiene report."""
    agents: list[dict] = []
    for agent_id, active in sorted(_active_agent_credentials(credentials).items()):
        active = sorted(active, key=lambda c: str(c.get("created_at") or ""))
        count = len(active)
        if count == 1:
            status = "ok"
            severity = "ok"
            recommendation = "one active PAT"
        elif count == 2:
            status = "rotation_window"
            severity = "warning"
            recommendation = "verify the replacement works, then revoke the older PAT"
        else:
            status = "cleanup_required"
            severity = "violation"
            recommendation = "revoke stale PATs before minting another token"

        agents.append(
            {
                "agent_id": agent_id,
                "active_count": count,
                "status": status,
                "severity": severity,
                "recommendation": recommendation,
                "credentials": [
                    {
                        "credential_id": c.get("credential_id"),
                        "key_id": c.get("key_id"),
                        "name": c.get("name"),
                        "audience": c.get("audience"),
                        "created_at": c.get("created_at"),
                        "expires_at": c.get("expires_at"),
                        "last_used_at": c.get("last_used_at"),
                    }
                    for c in active
                ],
            }
        )

    summary = {
        "agents_checked": len(agents),
        "ok": sum(1 for agent in agents if agent["status"] == "ok"),
        "rotation_windows": sum(1 for agent in agents if agent["status"] == "rotation_window"),
        "cleanup_required": sum(1 for agent in agents if agent["status"] == "cleanup_required"),
    }
    return {
        "policy": {
            "normal_active_agent_pats": 1,
            "rotation_window_active_agent_pats": 2,
            "max_active_agent_pats": 2,
        },
        "summary": summary,
        "agents": agents,
    }


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
    Give this enrollment token to a new agent. They run the legacy
    project-local runtime init, not the user bootstrap login:
        axctl auth init --token axp_a_... --agent their-name

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
        console.print(f"  axctl auth init --token {data.get('token', 'TOKEN')[:12]}... --agent AGENT_NAME")


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


@app.command("audit")
def audit(
    as_json: bool = JSON_OPTION,
    strict: bool = typer.Option(False, "--strict", help="Exit non-zero when any agent has more than two active PATs"),
):
    """Audit active agent PAT counts without minting or revoking credentials."""
    client = get_client()
    try:
        creds = client.mgmt_list_credentials()
    except httpx.HTTPStatusError as e:
        handle_error(e)

    report = build_credential_audit(creds)
    if as_json:
        print_json(report)
    else:
        summary = report["summary"]
        console.print(
            "[bold]Agent PAT audit[/bold] "
            f"ok={summary['ok']} rotation_windows={summary['rotation_windows']} "
            f"cleanup_required={summary['cleanup_required']}"
        )
        if not report["agents"]:
            console.print("[dim]No active agent-bound PATs found.[/dim]")
        else:
            print_table(
                ["Agent", "Active", "Status", "Recommendation"],
                [
                    {
                        "agent": agent["agent_id"],
                        "active": agent["active_count"],
                        "status": agent["status"],
                        "recommendation": agent["recommendation"],
                    }
                    for agent in report["agents"]
                ],
                keys=["agent", "active", "status", "recommendation"],
            )

    if strict and report["summary"]["cleanup_required"]:
        raise typer.Exit(EXIT_NOT_OK)


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
