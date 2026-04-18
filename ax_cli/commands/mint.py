"""ax token mint — single command to create an agent PAT.

Handles the full bootstrap flow: detect user PAT → resolve agent →
exchange for user_admin JWT → issue agent PAT → optionally save + profile.

Requires a user PAT (axp_u_). Fails clearly if run with an agent PAT.
"""

import os
import re
import sys
from pathlib import Path
from typing import Optional

import httpx
import typer

from ..config import get_user_client, resolve_user_token
from ..output import JSON_OPTION, console, handle_error, print_json

app = typer.Typer(name="token", help="Token management", no_args_is_help=True)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _resolve_agent_id(client, agent: str) -> tuple[str, str]:
    """Resolve agent name or UUID to (agent_id, agent_name).

    Uses user_access JWT (which has 'agents' scope) instead of user_admin
    (which lacks 'agents.list'). This is a known scope gap — user_admin
    only has agents.create, not agents.list. Workaround: resolve via the
    regular agents list endpoint, which works with user_access.
    """
    if _UUID_RE.match(agent):
        # Already a UUID — try to get the name
        try:
            data = client.get_agent(agent)
            agent_data = data.get("agent", data) if isinstance(data, dict) else data
            return agent, agent_data.get("name", agent)
        except Exception:
            return agent, agent
    # Name lookup via the standard agents endpoint (user_access scope).
    # Some agents may be hidden from list but still exist — if list fails,
    # fall back to direct get_agent() lookup.
    try:
        agents_data = client.list_agents()
        agents = agents_data if isinstance(agents_data, list) else agents_data.get("agents", [])
        match = next((a for a in agents if a.get("name", "").lower() == agent.lower()), None)
        if match:
            return match["id"], match.get("name", agent)
    except httpx.HTTPStatusError:
        pass  # list failed — try direct lookup below

    # Fallback: direct agent lookup by name (handles agents hidden from list)
    try:
        data = client.get_agent(agent)
        agent_data = data.get("agent", data) if isinstance(data, dict) else data
        agent_id = agent_data.get("id")
        if agent_id:
            return agent_id, agent_data.get("name", agent)
    except httpx.HTTPStatusError:
        pass

    return None, agent  # not found — caller decides whether to create


def _is_management_route_miss_error(exc: httpx.HTTPStatusError) -> bool:
    """Return true when the management route was missing or caught by frontend."""
    response = exc.response
    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type or response.text.lstrip().startswith("<!"):
        return True
    return response.status_code in {404, 405}


def _create_agent_for_mint(client, agent: str) -> dict:
    """Create an agent for token minting using the best available API route."""
    try:
        data = client.mgmt_create_agent(agent)
    except httpx.HTTPStatusError as exc:
        if not _is_management_route_miss_error(exc):
            raise
        data = client.create_agent(agent)
    return data.get("agent", data) if isinstance(data, dict) else data


@app.command()
def mint(
    agent: str = typer.Argument(..., help="Agent name or UUID"),
    name: str = typer.Option(None, "--name", "-n", help="Label for the PAT (default: <agent>-cli)"),
    expires_days: int = typer.Option(90, "--expires", help="PAT lifetime in days"),
    audience: str = typer.Option("both", "--audience", help="Target: cli, mcp, or both"),
    create: bool = typer.Option(False, "--create", help="Create the agent if it doesn't exist"),
    save_to: Optional[str] = typer.Option(
        None, "--save-to", help="Directory to save token file (writes .ax/config.toml)"
    ),
    profile_name: Optional[str] = typer.Option(None, "--profile", help="Create a named profile after minting"),
    env_name: Optional[str] = typer.Option(
        None,
        "--env",
        help="Use a named user-login environment created with `axctl login --env`",
    ),
    print_token: Optional[bool] = typer.Option(
        None,
        "--print-token/--no-print-token",
        help=("Print the raw PAT. Defaults to yes when not saving, and no when using --save-to or --profile."),
    ),
    as_json: bool = JSON_OPTION,
):
    """Mint an agent PAT in one shot.

    \b
    Requires a user PAT (axp_u_). The full flow:
      1. Verify you have a user PAT
      2. Resolve agent name to UUID
      3. Exchange for user_admin JWT
      4. Issue agent-bound PAT
      5. Optionally save token + create profile

    \b
    Examples:
        ax token mint backend_sentinel
        ax token mint backend_sentinel --audience both --expires 30
        ax token mint backend_sentinel --save-to /home/agent/.ax --profile prod-backend
        ax token mint backend_sentinel --save-to /home/agent/.ax --no-print-token
        ax token mint bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb
    """

    def status(message: str) -> None:
        if not as_json:
            console.print(message)

    if env_name:
        os.environ["AX_USER_ENV"] = env_name

    # Step 1: Verify user PAT
    token = resolve_user_token()
    if not token:
        console.print("[red]No user token found.[/red] Run axctl login with a user PAT.")
        raise typer.Exit(1)

    if token.startswith("axp_a_"):
        console.print("[red]Cannot mint with an agent PAT.[/red]")
        console.print("You need a user PAT (axp_u_...) to create agent tokens.")
        console.print("[dim]A user PAT can be created at Settings > Credentials on the platform.[/dim]")
        raise typer.Exit(1)

    if not token.startswith("axp_u_"):
        console.print(f"[yellow]Warning: token prefix '{token[:6]}' is not a recognized PAT type.[/yellow]")
        console.print("[dim]Expected axp_u_ (user PAT). Proceeding anyway.[/dim]")

    client = get_user_client()

    # Step 2: Resolve agent name → UUID (uses user_access JWT via standard endpoint)
    status(f"[cyan]Resolving agent '{agent}'...[/cyan]")
    agent_id, agent_name = _resolve_agent_id(client, agent)

    if agent_id is None:
        if create:
            status(f"[yellow]Agent '{agent}' not found. Creating...[/yellow]")
            try:
                agent_data = _create_agent_for_mint(client, agent)
                agent_id = agent_data.get("id", "")
                agent_name = agent_data.get("name", agent)
                status(f"[green]Created:[/green] {agent_name} ({agent_id[:12]}...)")
            except httpx.HTTPStatusError as e:
                handle_error(e)
                raise typer.Exit(1)
        elif not as_json and sys.stdin.isatty():
            console.print(f"[yellow]Agent '{agent}' not found.[/yellow]")
            if typer.confirm("Create it?"):
                try:
                    agent_data = _create_agent_for_mint(client, agent)
                    agent_id = agent_data.get("id", "")
                    agent_name = agent_data.get("name", agent)
                    status(f"[green]Created:[/green] {agent_name} ({agent_id[:12]}...)")
                except httpx.HTTPStatusError as e:
                    handle_error(e)
                    raise typer.Exit(1)
            else:
                raise typer.Exit(1)
        else:
            console.print(f"[red]Agent '{agent}' not found.[/red] Use --create to create it.")
            raise typer.Exit(1)
    else:
        status(f"[green]Found:[/green] {agent_name} ({agent_id[:12]}...)")

    # Step 3+4: Issue agent PAT (uses user_admin JWT via mgmt endpoint)
    pat_name = name or f"{agent_name}-cli"
    status(f"[cyan]Minting PAT '{pat_name}' (audience={audience}, expires={expires_days}d)...[/cyan]")
    try:
        data = client.mgmt_issue_agent_pat(
            agent_id,
            name=pat_name,
            expires_in_days=expires_days,
            audience=audience,
        )
    except httpx.HTTPStatusError as e:
        handle_error(e)
        raise typer.Exit(1)

    new_token = data.get("token", "")
    if not new_token:
        console.print("[red]Mint succeeded but no token in response.[/red]")
        if as_json:
            print_json(data)
        raise typer.Exit(1)

    # Step 5: Save token if requested
    token_file = None
    if save_to:
        save_dir = Path(save_to).expanduser().resolve()
        ax_dir = save_dir / ".ax" if not save_dir.name == ".ax" else save_dir
        ax_dir.mkdir(parents=True, exist_ok=True)

        token_file = ax_dir / f"{agent_name}_token"
        token_file.write_text(new_token)
        token_file.chmod(0o600)
        status(f"[green]Token saved:[/green] {token_file}")

        # Also write a minimal config.toml
        config_file = ax_dir / "config.toml"
        config_content = (
            f'token_file = "{token_file}"\n'
            f'base_url = "{client.base_url}"\n'
            f'agent_name = "{agent_name}"\n'
            f'agent_id = "{agent_id}"\n'
        )
        config_file.write_text(config_content)
        config_file.chmod(0o600)
        status(f"[green]Config saved:[/green] {config_file}")

    # Step 6: Create profile if requested
    if profile_name:
        if not token_file:
            # Save token to default location first
            default_dir = Path.home() / ".ax"
            default_dir.mkdir(parents=True, exist_ok=True)
            token_file = default_dir / f"{agent_name}_token"
            token_file.write_text(new_token)
            token_file.chmod(0o600)
            status(f"[green]Token saved:[/green] {token_file}")

        try:
            import socket
            from datetime import datetime, timezone

            from .profile import _profile_path, _token_sha256, _workdir_hash, _write_toml

            profile_data = {
                "name": profile_name,
                "base_url": client.base_url,
                "agent_name": agent_name,
                "token_file": str(token_file.resolve()),
                "token_sha256": _token_sha256(str(token_file)),
                "host_binding": socket.gethostname(),
                "workdir_hash": _workdir_hash(),
                "workdir_path": str(Path.cwd().resolve()),
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "agent_id": agent_id,
            }
            _write_toml(_profile_path(profile_name), profile_data)
            status(f"[green]Profile created:[/green] {profile_name}")
        except Exception as e:
            status(f"[yellow]Profile creation failed: {e}[/yellow]")
            status("[dim]You can create it manually: ax profile add ...[/dim]")

    should_print_token = print_token if print_token is not None else not bool(token_file or profile_name)

    # Output
    if as_json:
        result = {
            "agent_name": agent_name,
            "agent_id": agent_id,
            "name": pat_name,
            "audience": audience,
            "expires_in_days": expires_days,
            "token_printed": should_print_token,
        }
        if should_print_token:
            result["token"] = new_token
        else:
            result["token_redacted"] = True
        if token_file:
            result["token_file"] = str(token_file)
        if profile_name:
            result["profile"] = profile_name
        print_json(result)
    else:
        console.print("\n[bold green]Agent PAT minted successfully[/bold green]")
        console.print(f"  Agent: {agent_name} ({agent_id[:12]}...)")
        console.print(f"  Label: {pat_name}")
        console.print(f"  Audience: {audience}")
        console.print(f"  Expires: {data.get('expires_at', '?')[:10]}")
        if token_file:
            console.print(f"\n[dim]Saved to {token_file}[/dim]")
        if profile_name:
            console.print(f"[dim]Profile '{profile_name}' created. Use: ax profile use {profile_name}[/dim]")
        if should_print_token:
            console.print("\n[bold]Token (save now — shown once):[/bold]")
            console.print(f"  {new_token}")
        else:
            console.print("\n[dim]Token was stored locally and not printed. Use --print-token to display it.[/dim]")
