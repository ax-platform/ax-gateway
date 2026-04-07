"""ax agents — agent listing, creation, and management."""

import httpx
import typer

from ..config import get_client, resolve_space_id
from ..output import JSON_OPTION, console, handle_error, print_json, print_kv, print_table

app = typer.Typer(name="agents", help="Agent management", no_args_is_help=True)


@app.command("list")
def list_agents(as_json: bool = JSON_OPTION):
    """List agents in the current space."""
    client = get_client()
    try:
        data = client.list_agents()
    except httpx.HTTPStatusError as e:
        handle_error(e)
    agents = data if isinstance(data, list) else data.get("agents", [])
    if as_json:
        print_json(agents)
    else:
        print_table(
            ["ID", "Name", "Status"],
            agents,
            keys=["id", "name", "status"],
        )


@app.command("create")
def create_agent(
    name: str = typer.Argument(..., help="Agent name"),
    description: str = typer.Option(None, "--description", "-d", help="Agent description"),
    system_prompt: str = typer.Option(None, "--system-prompt", help="System prompt"),
    model: str = typer.Option(None, "--model", "-m", help="LLM model"),
    cloud: bool = typer.Option(False, "--cloud", help="Enable cloud agent"),
    can_manage_agents: bool = typer.Option(
        False, "--can-manage-agents", help="Allow this agent to manage other agents"
    ),
    space_id: str = typer.Option(None, "--space-id", help="Target space"),
    as_json: bool = JSON_OPTION,
):
    """Create a new agent.

    Uses the management API (user_admin JWT) when available,
    falls back to legacy /api/v1/agents for Cognito auth.
    """
    client = get_client()
    try:
        # Try management API first (exchange-based auth)
        if hasattr(client, "_exchanger") and client._exchanger:
            data = client.mgmt_create_agent(
                name,
                description=description,
                system_prompt=system_prompt,
                model=model,
                space_id=space_id,
            )
        else:
            data = client.create_agent(
                name,
                description=description,
                system_prompt=system_prompt,
                model=model,
                space_id=space_id,
                enable_cloud_agent=cloud,
                can_manage_agents=can_manage_agents,
            )
    except httpx.HTTPStatusError as e:
        handle_error(e)
    if as_json:
        print_json(data)
    else:
        console.print(f"[green]Created agent:[/green] {data['name']} ({data['id']})")
        print_kv(
            {
                "origin": data.get("origin"),
                "status": data.get("status"),
                "space_id": data.get("space_id"),
            }
        )


@app.command("get")
def get_agent(
    identifier: str = typer.Argument(..., help="Agent name or UUID"),
    as_json: bool = JSON_OPTION,
):
    """Get agent details by name or UUID."""
    client = get_client()
    try:
        data = client.get_agent(identifier)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    if as_json:
        print_json(data)
    else:
        print_kv(data)


@app.command("update")
def update_agent(
    identifier: str = typer.Argument(..., help="Agent name or UUID"),
    description: str = typer.Option(None, "--description", "-d"),
    system_prompt: str = typer.Option(None, "--system-prompt"),
    model: str = typer.Option(None, "--model", "-m"),
    agent_type: str = typer.Option(None, "--type", "-t", help="Agent type: sentinel, assistant, cloud_gcp, etc."),
    bio: str = typer.Option(None, "--bio", "-b", help="Short bio"),
    specialization: str = typer.Option(None, "--specialization", "-s", help="Specialization area"),
    status: str = typer.Option(None, "--status", help="active or inactive"),
    as_json: bool = JSON_OPTION,
):
    """Update an agent's metadata.

    Examples:
        ax agents update backend_sentinel --type sentinel --model claude-sonnet-4-6
        ax agents update anvil --bio "Infra and ops" --specialization "server management"
    """
    client = get_client()
    fields = {}
    if description is not None:
        fields["description"] = description
    if system_prompt is not None:
        fields["system_prompt"] = system_prompt
    if model is not None:
        fields["model"] = model
    if agent_type is not None:
        fields["agent_type"] = agent_type
    if bio is not None:
        fields["bio"] = bio
    if specialization is not None:
        fields["specialization"] = specialization
    if status is not None:
        fields["status"] = status

    if not fields:
        typer.echo("Nothing to update. Use --type, --model, --bio, --description, --status, etc.", err=True)
        raise typer.Exit(1)

    try:
        data = client.update_agent(identifier, **fields)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    if as_json:
        print_json(data)
    else:
        console.print(f"[green]Updated agent:[/green] {data['name']}")


@app.command("delete")
def delete_agent(
    identifier: str = typer.Argument(..., help="Agent name or UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Delete an agent."""
    if not yes:
        confirm = typer.confirm(f"Delete agent '{identifier}'?")
        if not confirm:
            raise typer.Abort()

    client = get_client()
    try:
        data = client.delete_agent(identifier)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    console.print(f"[red]Deleted:[/red] {data.get('message', identifier)}")


@app.command("status")
def status(as_json: bool = JSON_OPTION):
    """Show agent presence (online/offline) in the current space."""
    client = get_client()
    try:
        data = client.get_agents_presence()
    except httpx.HTTPStatusError as e:
        handle_error(e)
    agents = data.get("agents", [])
    if as_json:
        print_json(agents)
    else:
        for a in agents:
            indicator = "[green]online[/green]" if a.get("presence") == "online" else "[dim]offline[/dim]"
            agent_type = a.get("agent_type", "assistant")
            last = a.get("last_active", "—")
            console.print(f"  {indicator}  {a['name']:<20s}  {agent_type:<12s}  last_active={last}")


@app.command("tools")
def tools(
    agent_id: str = typer.Argument(..., help="Agent ID"),
    space_id: str = typer.Option(None, "--space-id", help="Override default space"),
    as_json: bool = JSON_OPTION,
):
    """Show enabled tools for an agent."""
    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)
    try:
        data = client.get_agent_tools(sid, agent_id)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    if as_json:
        print_json(data)
    else:
        print_kv(data)


@app.command("avatar")
def avatar(
    agent: str = typer.Argument(..., help="Agent name to generate avatar for"),
    agent_type: str = typer.Option(
        "default", "--type", "-t", help="Agent type for color theme (sentinel, mcp, space_agent, cloud)"
    ),
    size: int = typer.Option(128, "--size", "-s", help="Avatar size in pixels"),
    output: str = typer.Option(None, "--output", "-o", help="Save to file (default: print SVG)"),
    set_avatar: bool = typer.Option(False, "--set", help="Upload and set as the agent's avatar_url"),
    as_json: bool = JSON_OPTION,
):
    """Generate or set an agent's avatar.

    Generate a unique SVG avatar based on agent name:
        ax agents avatar backend_sentinel
        ax agents avatar backend_sentinel --type sentinel -o avatar.svg

    Generate and set as the agent's profile picture:
        ax agents avatar backend_sentinel --set
    """
    from ..avatar import avatar_data_uri, generate_avatar

    svg = generate_avatar(agent, agent_type, size)

    if output:
        with open(output, "w") as f:
            f.write(svg)
        console.print(f"[green]Saved:[/green] {output}")
    elif set_avatar:
        client = get_client()
        data_uri = avatar_data_uri(agent, agent_type, size)
        try:
            # Find the agent by name
            agents_data = client.list_agents()
            agents_list = agents_data if isinstance(agents_data, list) else agents_data.get("agents", [])
            target = next((a for a in agents_list if a.get("name", "").lower() == agent.lower()), None)
            if not target:
                console.print(f"[red]Agent '{agent}' not found[/red]")
                raise typer.Exit(1)
            # Update avatar_url
            r = client._http.patch(f"/api/v1/agents/{target['id']}", json={"avatar_url": data_uri})
            r.raise_for_status()
            console.print(f"[green]Avatar set for @{agent}[/green]")
        except httpx.HTTPStatusError as e:
            handle_error(e)
    elif as_json:
        import json

        print(json.dumps({"name": agent, "svg": svg, "data_uri": avatar_data_uri(agent, agent_type, size)}))
    else:
        print(svg)
