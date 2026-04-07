"""ax auth — identity and token management."""

from pathlib import Path

import httpx
import typer

from ..config import (
    _global_config_dir,
    _load_local_config,
    _local_config_dir,
    _save_config,
    get_client,
    resolve_agent_name,
    resolve_token,
    save_token,
)
from ..output import JSON_OPTION, console, handle_error, print_json, print_kv

app = typer.Typer(name="auth", help="Authentication & identity", no_args_is_help=True)
token_app = typer.Typer(name="token", help="Token management", no_args_is_help=True)
app.add_typer(token_app, name="token")


@app.command()
def whoami(as_json: bool = JSON_OPTION):
    """Show current identity — principal, bound agent, resolved spaces."""
    client = get_client()
    try:
        data = client.whoami()
    except httpx.HTTPStatusError as e:
        handle_error(e)

    bound = data.get("bound_agent")
    if bound:
        data["resolved_space_id"] = bound.get("default_space_id", "none")
    else:
        from ..config import resolve_space_id

        try:
            space_id = resolve_space_id(client, explicit=None)
            data["resolved_space_id"] = space_id
        except SystemExit:
            data["resolved_space_id"] = "unresolved (set AX_SPACE_ID or use --space-id)"

    # Show resolved agent name
    resolved = resolve_agent_name(client=client)
    if resolved:
        data["resolved_agent"] = resolved

    # Show local config path if it exists
    local = _local_config_dir()
    if local and (local / "config.toml").exists():
        data["local_config"] = str(local / "config.toml")

    if as_json:
        print_json(data)
    else:
        print_kv(data)


@app.command("init")
def init(
    token: str = typer.Option(None, "--token", "-t", help="PAT token (axp_u_... or axp_a_...)"),
    base_url: str = typer.Option("http://localhost:8002", "--url", "-u", help="API base URL"),
    agent: str = typer.Option(None, "--agent", "-a", help="Agent name or ID (auto-detected if not set)"),
    space_id: str = typer.Option(None, "--space-id", "-s", help="Space ID (auto-detected if not set)"),
):
    """Set up authentication for this project.

    Just provide your PAT — everything else is auto-discovered:

    \b
        ax auth init --token axp_u_...
        ax auth init --token axp_u_... --url https://next.paxai.app

    The CLI will:
    1. Verify the token works (exchange it for a JWT)
    2. Discover your identity, spaces, and agents
    3. Auto-select defaults if there's only one option
    4. Save everything to .ax/config.toml

    After init, all commands just work — no flags needed.
    """
    from pathlib import Path

    if not token:
        console.print("[red]Token required.[/red] Get one from Settings > Credentials in the UI.")
        console.print("  ax auth init --token axp_u_YOUR_TOKEN_HERE")
        raise typer.Exit(1)

    # --agent accepts both name and UUID
    import re

    _uuid_pattern = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
    agent_name = None
    agent_id = None
    if agent and _uuid_pattern.match(agent):
        agent_id = agent
    elif agent:
        agent_name = agent

    try:
        local = _local_config_dir(create=True)
    except TypeError:
        local = _local_config_dir()
    if not local:
        local = Path.cwd() / ".ax"

    cfg = _load_local_config()
    cfg["token"] = token
    cfg["base_url"] = base_url

    is_enrollment = token.startswith("axp_a_")
    console.print(f"\n[cyan]Connecting to {base_url}...[/cyan]")

    if is_enrollment:
        # --- Agent token flow: register new agent OR connect to already-bound agent ---
        resolved_name = agent_name or agent_id

        # First try: exchange with agent_name (enrollment/auto-register)
        registered = False
        if resolved_name:
            console.print(f"[cyan]Registering agent '{resolved_name}'...[/cyan]")
        else:
            # No name given — check if token is already bound
            console.print("[cyan]Checking token...[/cyan]")

        try:
            exchange_body = {
                "requested_token_class": "agent_access",
                "scope": "messages tasks context agents spaces search",
                "audience": "ax-api",
            }
            if resolved_name:
                exchange_body["agent_name"] = resolved_name
            r = httpx.post(
                f"{base_url}/auth/exchange",
                json=exchange_body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                timeout=15.0,
            )
            r.raise_for_status()
            data = r.json()
            cfg["agent_id"] = data.get("agent_id", "")
            cfg["agent_name"] = data.get("agent_name", resolved_name or "")
            registered = True

            # Cache the JWT from enrollment so AxClient doesn't double-exchange
            if data.get("access_token") and data.get("expires_in"):
                try:
                    from ..token_cache import TokenExchanger

                    exchanger = TokenExchanger(base_url, token)
                    import time

                    exchanger._cache["enrollment"] = {
                        "access_token": data["access_token"],
                        "exp": time.time() + data["expires_in"],
                        "token_class": "agent_access",
                        "pat_key_id": exchanger.pat_key_id,
                    }
                    exchanger._save_disk_cache()
                except Exception:
                    pass

            if resolved_name:
                console.print(f"[green]Agent registered:[/green] {cfg['agent_name']} ({cfg['agent_id'][:12]}...)")
            else:
                console.print(f"[green]Connected:[/green] {cfg['agent_name']} ({cfg['agent_id'][:12]}...)")
        except httpx.HTTPStatusError as e:
            # If already bound, the exchange needs agent_id — discover it via whoami
            try:
                detail = e.response.json().get("detail", {})
                error_code = detail.get("error", "") if isinstance(detail, dict) else ""
            except Exception:
                error_code = ""

            if error_code in ("agent_not_found", "binding_not_allowed"):
                # Token may already be bound — try discovering via authenticate
                console.print("[cyan]Token already bound. Discovering agent...[/cyan]")
                try:
                    from ..client import AxClient

                    client = AxClient(base_url=base_url, token=token)
                    me = client.whoami()
                    bound = me.get("bound_agent")
                    if bound and bound.get("agent_id"):
                        cfg["agent_id"] = bound["agent_id"]
                        cfg["agent_name"] = bound.get("agent_name", "")
                        registered = True
                        console.print(
                            f"[green]Found bound agent:[/green] {cfg['agent_name']} ({cfg['agent_id'][:12]}...)"
                        )
                    else:
                        console.print("[red]Token is bound but agent not found in response.[/red]")
                        raise typer.Exit(1)
                except typer.Exit:
                    raise
                except Exception as ex:
                    console.print(f"[red]Could not discover bound agent:[/red] {ex}")
                    raise typer.Exit(1)
            else:
                msg = detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)
                console.print(f"[red]Registration failed:[/red] {msg}")
                raise typer.Exit(1)
        except Exception as e:
            console.print(f"[red]Connection failed:[/red] {e}")
            raise typer.Exit(1)

        if not registered:
            if not resolved_name:
                console.print("[yellow]This is an enrollment token. Provide an agent name:[/yellow]")
                console.print("  ax auth init --token axp_a_... --agent my-agent-name")
            raise typer.Exit(1)

        console.print("[green]Token bound.[/green] Exchange successful.")

        # Discover space
        try:
            from ..client import AxClient

            client = AxClient(base_url=base_url, token=token, agent_id=cfg.get("agent_id"))
            me = client.whoami()
            bound = me.get("bound_agent")
            if bound and bound.get("default_space_id"):
                cfg["space_id"] = bound["default_space_id"]
                console.print(f"[green]Space:[/green] {bound.get('default_space_name', cfg['space_id'][:12])}")
        except Exception:
            pass

    else:
        # --- User token flow: discover identity + spaces + agents ---
        try:
            from ..token_cache import TokenExchanger

            exchanger = TokenExchanger(base_url, token)
            exchanger.get_token("user_access", scope="messages tasks context agents spaces search")
            console.print("[green]Token verified.[/green] Exchange successful.")
        except Exception as e:
            console.print(f"[red]Token verification failed:[/red] {e}")
            console.print("Check that the token is valid and the URL is correct.")
            raise typer.Exit(1)

        try:
            from ..client import AxClient

            client = AxClient(base_url=base_url, token=token)
            me = client.whoami()
            username = me.get("username", "unknown")
            console.print(f"[green]Identity:[/green] {username} ({me.get('email', '')})")

            bound = me.get("bound_agent")
            if bound:
                cfg["agent_id"] = bound.get("agent_id", "")
                cfg["agent_name"] = bound.get("agent_name", "")
                if bound.get("default_space_id"):
                    cfg["space_id"] = bound["default_space_id"]
                console.print(
                    f"[green]Bound agent:[/green] {bound.get('agent_name')} ({bound.get('agent_id', '')[:12]}...)"
                )
        except Exception:
            pass

        # Discover spaces
        if not cfg.get("space_id") and not space_id:
            try:
                spaces = client.list_spaces()
                space_list = spaces.get("spaces", spaces) if isinstance(spaces, dict) else spaces
                if isinstance(space_list, list) and len(space_list) == 1:
                    cfg["space_id"] = str(space_list[0].get("id"))
                    console.print(f"[green]Space:[/green] {space_list[0].get('name')} (auto-selected)")
                elif isinstance(space_list, list) and len(space_list) > 1:
                    console.print(f"\n[yellow]{len(space_list)} spaces found.[/yellow] Use --space-id to pick one:")
                    for s in space_list[:5]:
                        console.print(f"  {s.get('name')} — {s.get('id')}")
            except Exception:
                pass

        # Discover agents
        if not cfg.get("agent_id") and not agent_id:
            try:
                agents_data = client.list_agents()
                agent_list = agents_data.get("agents", agents_data) if isinstance(agents_data, dict) else agents_data
                if isinstance(agent_list, list) and len(agent_list) == 1:
                    cfg["agent_id"] = str(agent_list[0].get("id"))
                    cfg["agent_name"] = agent_list[0].get("name", "")
                    console.print(f"[green]Agent:[/green] {agent_list[0].get('name')} (auto-selected)")
                elif isinstance(agent_list, list) and len(agent_list) > 1:
                    console.print(f"\n[cyan]{len(agent_list)} agents available.[/cyan] Use --agent-id to pick one.")
            except Exception:
                pass

    # Apply explicit overrides
    if agent_name:
        cfg["agent_name"] = agent_name
    if agent_id:
        cfg["agent_id"] = agent_id
    if space_id:
        cfg["space_id"] = space_id

    # Save
    _save_config(cfg, local=True)
    config_path = local / "config.toml"
    console.print(f"\n[green]Saved:[/green] {config_path}")
    for k, v in cfg.items():
        if k == "token":
            v = v[:6] + "..." + v[-4:] if len(v) > 10 else "***"
        console.print(f"  {k} = {v}")

    console.print("\n[cyan]You're ready.[/cyan] Try: ax auth whoami")

    # Check .gitignore
    root = local.parent
    gitignore = root / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text()
        if ".ax/" not in content and ".ax" not in content:
            console.print(f"[yellow]Reminder:[/yellow] Add .ax/ to {gitignore}")
    elif (root / ".git").exists():
        console.print("[yellow]Reminder:[/yellow] Add .ax/ to .gitignore")


@app.command("exchange")
def exchange(
    token_class: str = typer.Option(
        "user_access", "--class", "-c", help="Token class: user_access, user_admin, agent_access"
    ),
    scope: str = typer.Option(
        "messages tasks context agents spaces search", "--scope", "-s", help="Space-separated scopes"
    ),
    agent_id: str = typer.Option(None, "--agent", "-a", help="Agent ID (required for agent_access)"),
    audience: str = typer.Option("ax-api", "--audience", help="Target audience: ax-api or ax-mcp"),
    resource: str = typer.Option(None, "--resource", help="RFC 8707 resource URI (e.g. https://next.paxai.app/mcp)"),
    as_json: bool = JSON_OPTION,
):
    """Exchange PAT for a short-lived JWT (AUTH-SPEC-001 §9).

    The PAT is read from config. The JWT is printed (masked by default).
    Use --json to get the full exchange response for scripting.
    """
    token = resolve_token()
    if not token:
        console.print("[red]No token configured.[/red] Use `ax auth init` or `ax auth token set`.")
        raise typer.Exit(1)
    if not token.startswith("axp_"):
        console.print("[red]Token is not a PAT (must start with axp_).[/red]")
        raise typer.Exit(1)

    from ..config import resolve_base_url
    from ..token_cache import TokenExchanger

    exchanger = TokenExchanger(resolve_base_url(), token)
    try:
        jwt = exchanger.get_token(
            token_class,
            agent_id=agent_id,
            audience=audience,
            scope=scope,
        )
    except httpx.HTTPStatusError as e:
        handle_error(e)

    if as_json:
        # Decode claims for display without verification
        import base64
        import json as json_mod

        parts = jwt.split(".")
        if len(parts) == 3:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json_mod.loads(base64.urlsafe_b64decode(payload))
            print_json(
                {
                    "access_token": jwt[:20] + "...",
                    "token_class": claims.get("token_class"),
                    "sub": claims.get("sub"),
                    "scope": claims.get("scope"),
                    "expires_in": claims.get("exp", 0) - claims.get("iat", 0),
                    "agent_id": claims.get("agent_id"),
                }
            )
        else:
            print_json({"access_token": jwt[:20] + "..."})
    else:
        console.print(f"[green]Exchanged:[/green] {token_class}")
        console.print(f"  JWT: {jwt[:20]}...{jwt[-10:]}")
        console.print("  Cached until expiry. Use --json for details.")


@token_app.command("set")
def token_set(
    token: str = typer.Argument(..., help="PAT token (axp_u_...)"),
    global_: bool = typer.Option(False, "--global", "-g", help="Save to ~/.ax/ instead of local .ax/"),
):
    """Save token to local .ax/config.toml (default) or ~/.ax/ with --global."""
    save_token(token, local=not global_)
    if global_:
        config_path = _global_config_dir() / "config.toml"
    else:
        local_dir = _local_config_dir() or (Path.cwd() / ".ax")
        config_path = local_dir / "config.toml"
    typer.echo(f"Token saved to {config_path}")


@token_app.command("show")
def token_show():
    """Show saved token (masked)."""
    token = resolve_token()
    if not token:
        typer.echo("No token configured.", err=True)
        raise typer.Exit(1)
    if len(token) > 10:
        masked = token[:6] + "..." + token[-4:]
    else:
        masked = token[:2] + "..." + token[-2:]
    typer.echo(masked)
