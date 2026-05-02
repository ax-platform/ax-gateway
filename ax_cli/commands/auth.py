"""ax auth — identity and token management."""

import os
from pathlib import Path

import httpx
import typer

from ..config import (
    _global_config_dir,
    _load_local_config,
    _load_user_config,
    _local_config_dir,
    _save_config,
    _save_user_config,
    diagnose_auth_config,
    get_client,
    resolve_agent_name,
    resolve_gateway_config,
    resolve_token,
    save_token,
)
from ..output import EXIT_NOT_OK, JSON_OPTION, apply_envelope, console, handle_error, print_json, print_kv

app = typer.Typer(name="auth", help="Authentication & identity", no_args_is_help=True)
token_app = typer.Typer(name="token", help="Token management", no_args_is_help=True)
app.add_typer(token_app, name="token")

DEFAULT_LOGIN_BASE_URL = "https://paxai.app"


def _mask_token_prefix(token: str) -> str:
    """Show enough token shape to confirm paste without exposing the secret."""
    token = token.strip()
    if not token:
        return "***"
    if len(token) <= 4:
        return "*" * len(token)
    return f"{token[:6]}{'*' * 8}"


def _resolve_login_token(token: str | None) -> str:
    """Return an explicit token or prompt for one without echoing it."""
    if token and token.strip():
        return token.strip()

    console.print("[cyan]Paste your aX token. Input is hidden.[/cyan]")
    entered = typer.prompt("Token", hide_input=True).strip()
    if not entered:
        console.print("[red]Token required.[/red] Get one from Settings > Credentials in the UI.")
        raise typer.Exit(1)
    console.print(f"[green]Token captured:[/green] {_mask_token_prefix(entered)}")
    return entered


def _candidate_space_id(space: dict) -> str | None:
    value = space.get("id", space.get("space_id"))
    return str(value) if value else None


def _select_login_space(space_list: list[dict]) -> dict | None:
    """Pick only an unambiguous default; login itself should not force space setup."""
    if len(space_list) == 1:
        return space_list[0]

    for key in ("is_current", "current", "is_default", "default"):
        matches = [space for space in space_list if space.get(key) is True]
        if len(matches) == 1:
            return matches[0]

    personal = [
        space
        for space in space_list
        if space.get("is_personal") is True or str(space.get("space_mode", "")).lower() == "personal"
    ]
    if len(personal) == 1:
        return personal[0]

    return None


def login_user(
    token: str | None = None,
    *,
    base_url: str = DEFAULT_LOGIN_BASE_URL,
    space_id: str | None = None,
    agent: str | None = None,
    env_name: str | None = None,
) -> None:
    """Log in a human user without touching agent runtime config."""
    token = _resolve_login_token(token)
    if agent:
        console.print("[yellow]Ignoring --agent for user login. Use an agent PAT/profile for agent runtime.[/yellow]")

    cfg = _load_user_config(env_name)
    cfg["token"] = token
    cfg["base_url"] = base_url
    cfg["principal_type"] = "user"
    if env_name:
        cfg["environment"] = env_name
    cfg.pop("agent_id", None)
    cfg.pop("agent_name", None)

    console.print(f"\n[cyan]Connecting to {base_url}...[/cyan]")
    try:
        from ..token_cache import TokenExchanger

        exchanger = TokenExchanger(base_url, token)
        exchanger.get_token(
            "user_access",
            scope="messages tasks context agents spaces search",
            force_refresh=True,
        )
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

        if space_id:
            cfg["space_id"] = space_id
        elif not cfg.get("space_id"):
            spaces = client.list_spaces()
            space_list = spaces.get("spaces", spaces) if isinstance(spaces, dict) else spaces
            if isinstance(space_list, list):
                selected_space = _select_login_space([s for s in space_list if isinstance(s, dict)])
                if selected_space:
                    selected_id = _candidate_space_id(selected_space)
                    if selected_id:
                        cfg["space_id"] = selected_id
                        console.print(f"[green]Space:[/green] {selected_space.get('name', selected_id)}")
                elif len(space_list) > 1:
                    console.print(
                        f"\n[yellow]{len(space_list)} spaces found.[/yellow] No default space selected during login."
                    )
    except Exception:
        if space_id:
            cfg["space_id"] = space_id

    config_path = _save_user_config(cfg, env_name=env_name)
    console.print(f"\n[green]Saved user login:[/green] {config_path}")
    for k, v in cfg.items():
        if k == "token":
            v = v[:6] + "..." + v[-4:] if len(v) > 10 else "***"
        console.print(f"  {k} = {v}")

    console.print("\n[cyan]You're ready.[/cyan] Try: ax auth whoami")


@app.command("doctor")
def doctor(
    env_name: str = typer.Option(
        None,
        "--env",
        help="Diagnose a named user-login environment created with `axctl login --env`",
    ),
    space_id: str = typer.Option(None, "--space-id", help="Show this explicit space override in the resolution"),
    as_json: bool = JSON_OPTION,
):
    """Explain effective auth/config resolution without calling the API."""
    data = diagnose_auth_config(env_name=env_name, explicit_space_id=space_id)
    effective = data["effective"]
    apply_envelope(
        data,
        summary={
            "command": "ax auth doctor",
            "principal_intent": effective.get("principal_intent"),
            "auth_source": effective.get("auth_source"),
            "host": effective.get("host"),
            "space_id": effective.get("space_id"),
            "warnings": len(data.get("warnings", [])),
            "problems": len(data.get("problems", [])),
        },
        details=data.get("problems") or data.get("warnings") or [],
    )
    if as_json:
        print_json(data)
    else:
        status = "[green]OK[/green]" if data["ok"] else "[red]PROBLEM[/red]"
        console.print(f"[bold]aX auth doctor:[/bold] {status}")
        console.print(f"  principal_intent = {effective.get('principal_intent')}")
        console.print(f"  auth_source      = {effective.get('auth_source')}")
        console.print(f"  token_kind       = {effective.get('token_kind')} ({effective.get('token')})")
        console.print(f"  base_url         = {effective.get('base_url')} ({effective.get('base_url_source')})")
        console.print(f"  host             = {effective.get('host')}")
        console.print(f"  space_id         = {effective.get('space_id')} ({effective.get('space_source')})")
        console.print(f"  agent_name       = {effective.get('agent_name')} ({effective.get('agent_name_source')})")
        console.print(f"  agent_id         = {effective.get('agent_id')} ({effective.get('agent_id_source')})")
        if data.get("runtime_config"):
            console.print(f"  runtime_config   = {data['runtime_config']}")
        if data.get("selected_env"):
            console.print(f"  selected_env     = {data['selected_env']}")
        if data.get("selected_profile"):
            console.print(f"  selected_profile = {data['selected_profile']}")
        for warning in data.get("warnings", []):
            console.print(f"[yellow]warning:[/yellow] {warning['code']} - {warning.get('reason')}")
        for problem in data.get("problems", []):
            console.print(f"[red]problem:[/red] {problem['code']} - {problem.get('reason')}")

    if not data["ok"]:
        raise typer.Exit(EXIT_NOT_OK)


@app.command()
def whoami(as_json: bool = JSON_OPTION):
    """Show current identity — principal, bound agent, resolved spaces."""
    gateway_cfg = resolve_gateway_config()
    if gateway_cfg:
        from .messages import _gateway_local_call

        data = _gateway_local_call(gateway_cfg=gateway_cfg, method="whoami")
        data.setdefault("control_plane", "gateway")
        data.setdefault("gateway_url", gateway_cfg.get("url"))
        data.setdefault("local_config", gateway_cfg.get("local_config"))
        if as_json:
            print_json(data)
        else:
            print_kv(data)
        return

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
    runtime_config = os.environ.get("AX_CONFIG_FILE")
    if runtime_config:
        data["runtime_config"] = runtime_config

    if as_json:
        print_json(data)
    else:
        print_kv(data)


@app.command("init")
def init(
    token: str = typer.Option(None, "--token", "-t", help="PAT token (prompted securely if omitted)"),
    base_url: str = typer.Option(DEFAULT_LOGIN_BASE_URL, "--url", "-u", help="API base URL"),
    agent: str = typer.Option(None, "--agent", "-a", help="Agent name or ID (auto-detected if not set)"),
    space_id: str = typer.Option(None, "--space-id", "-s", help="Optional default space ID"),
):
    """Legacy project-local runtime init.

    For normal user bootstrap, run `axctl login` first. This command writes
    local `.ax/config.toml` runtime config for a project or agent worktree.

    Just provide your PAT — everything else is auto-discovered:

    \b
        axctl login
        axctl login --url https://paxai.app

    The CLI will:
    1. Verify the token works (exchange it for a JWT)
    2. Discover your identity, spaces, and agents
    3. Auto-select a default space only when it is unambiguous
    4. Save everything to .ax/config.toml

    After this legacy init, project-local commands can use the saved runtime
    config without flags.
    """
    from pathlib import Path

    token = _resolve_login_token(token)

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

    is_enrollment = token.startswith("axp_a_")
    cfg = _load_local_config()
    cfg["token"] = token
    cfg["base_url"] = base_url
    cfg["principal_type"] = "agent" if is_enrollment else "user"
    if not is_enrollment:
        cfg.pop("agent_id", None)
        cfg.pop("agent_name", None)
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
                console.print("  axctl auth init --token axp_a_... --agent my-agent-name")
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
            exchanger.get_token(
                "user_access",
                scope="messages tasks context agents spaces search",
                force_refresh=True,
            )
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
        except Exception:
            pass

        # Discover spaces
        if not cfg.get("space_id") and not space_id:
            try:
                spaces = client.list_spaces()
                space_list = spaces.get("spaces", spaces) if isinstance(spaces, dict) else spaces
                if isinstance(space_list, list):
                    selected_space = _select_login_space([s for s in space_list if isinstance(s, dict)])
                    if selected_space:
                        selected_id = _candidate_space_id(selected_space)
                        if selected_id:
                            cfg["space_id"] = selected_id
                            console.print(f"[green]Space:[/green] {selected_space.get('name', selected_id)}")
                    elif len(space_list) > 1:
                        console.print(
                            f"\n[yellow]{len(space_list)} spaces found.[/yellow] "
                            "No default space selected during login."
                        )
            except Exception:
                pass

        if agent:
            console.print(
                "[yellow]Ignoring --agent for user login. Use an agent PAT/profile for agent runtime.[/yellow]"
            )

    # Apply explicit overrides
    if is_enrollment and agent_name:
        cfg["agent_name"] = agent_name
    if is_enrollment and agent_id:
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
    agent_id: str = typer.Option(None, "--agent", "-a", help="Existing agent ID for agent_access"),
    agent_name: str = typer.Option(None, "--agent-name", help="Agent name for first local enrollment/bind"),
    audience: str = typer.Option("ax-api", "--audience", help="Target audience: ax-api or ax-mcp"),
    resource: str = typer.Option(None, "--resource", help="RFC 8707 resource URI (e.g. https://paxai.app/mcp)"),
    requested_ttl: int = typer.Option(None, "--ttl", help="Requested token TTL in seconds"),
    as_json: bool = JSON_OPTION,
):
    """Exchange PAT for a short-lived JWT (AUTH-SPEC-001 §9).

    The PAT is read from config. The JWT is printed (masked by default).
    Use --json to get the full exchange response for scripting.
    """
    token = resolve_token()
    if not token:
        console.print(
            "[red]No token configured.[/red] For Gateway-managed agents, use "
            "`ax gateway local ... --workdir <path>` so Gateway can broker the "
            "agent identity. For user setup, log into Gateway with `ax gateway login`."
        )
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
            agent_name=agent_name,
            audience=audience,
            scope=scope,
            requested_ttl=requested_ttl,
            resource=resource,
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
    """Advanced: save token to local .ax/config.toml or ~/.ax/.

    Gateway-managed agents should not use this. Use `ax gateway local ...`
    instead so Gateway owns the token boundary and audit trail.
    """
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
        typer.echo(
            "No token configured. Gateway-managed agents should use "
            "`ax gateway local ... --workdir <path>`; users should log into "
            "Gateway with `ax gateway login`.",
            err=True,
        )
        raise typer.Exit(1)
    if len(token) > 10:
        masked = token[:6] + "..." + token[-4:]
    else:
        masked = token[:2] + "..." + token[-2:]
    typer.echo(masked)
