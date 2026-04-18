"""ax bootstrap-agent — one-shot scoped agent + PAT + workspace setup.

Collapses the 15-step manual flow documented in
``shared/state/axctl-friction-2026-04-17.md §0`` into a single command:

    axctl bootstrap-agent axolotl \\
        --space-id ed81ae98-50cb-4268-b986-1b9fe76df742 \\
        --description "Playful ax-cli helper" \\
        --model codex:gpt-5.4 \\
        --audience both \\
        --save-to /home/ax-agent/agents/axolotl \\
        --profile next-axolotl

What it does, in order:

1. Require a user PAT (``axp_u_``). Agent PATs can't create agents.
2. Print the effective-config line so operators don't silently target
   the wrong environment.
3. POST ``/api/v1/agents`` with ``X-Space-Id`` — the creation path that
   actually works on prod. Body carries ``description``/``model`` when
   provided.
4. If the agent already exists in the target space and ``--allow-existing``
   is set, reuse it; otherwise abort.
5. Optionally update ``bio``/``specialization`` via the legacy
   ``/api/v1/agents/manage/{name}`` PUT (the one that IS proxied).
6. Mint an agent-bound PAT. Try ``/credentials/agent-pat`` first (canonical
   per the ax-operator skill); on an HTML/404/405 response fall back to
   ``POST /api/v1/keys`` with ``bound_agent_id``, ``allowed_agent_ids``,
   ``audience``, and prod-compatible scopes (``api:read``/``api:write``).
7. Write workspace config: ``{save_to}/.ax/config.toml`` plus a ``token``
   file at mode 0600. Optionally create a named profile.
8. Verify with a ``GET /auth/me`` using the new PAT and print the resolved
   ``allowed_spaces`` so the caller sees containment worked.

Every mutating step logs a one-liner; failures bail loudly with the source
of the token being used (no more "Invalid credential" without a file path).
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import typer

from ..config import (
    _resolve_user_env,
    _user_config_path,
    get_user_client,
    resolve_user_base_url,
    resolve_user_token,
)
from ..output import JSON_OPTION, console, handle_error, print_json

# Scopes the backend /api/v1/keys endpoint actually accepts on prod (see
# axctl-friction-2026-04-17 §4). Other scope vocabularies get silently rejected.
DEFAULT_KEY_SCOPES: list[str] = ["api:read", "api:write"]


# ── Dataclasses + helpers ────────────────────────────────────────────────


@dataclass
class BootstrapResult:
    """What bootstrap_agent produces. Shape is frozen for --json output."""

    agent_id: str
    agent_name: str
    space_id: str
    base_url: str
    token_path: Optional[str]
    config_path: Optional[str]
    profile_name: Optional[str]
    pat_source: str  # "mgmt" | "keys_fallback"
    allowed_spaces: list[dict]

    def as_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "space_id": self.space_id,
            "base_url": self.base_url,
            "token_path": self.token_path,
            "config_path": self.config_path,
            "profile_name": self.profile_name,
            "pat_source": self.pat_source,
            "allowed_spaces": self.allowed_spaces,
        }


def _effective_config_line() -> str:
    """Same one-liner as commands.agents._effective_config_line — kept here
    to avoid a circular import and stay local."""
    base_url = resolve_user_base_url()
    user_env = _resolve_user_env() or "default"
    user_cfg_path = _user_config_path()
    source = str(user_cfg_path) if user_cfg_path.exists() else "(none)"
    return f"[dim]base_url={base_url}  user_env={user_env}  source={source}[/dim]"


def _is_route_miss(exc: httpx.HTTPStatusError) -> bool:
    """Management routes sometimes get caught by the frontend proxy on prod,
    returning HTML or 404/405. Detect so we can fall back."""
    r = exc.response
    ct = r.headers.get("content-type", "")
    if "text/html" in ct or r.text.lstrip().startswith("<!"):
        return True
    return r.status_code in {404, 405}


def _find_agent_in_space(client, name: str, space_id: str) -> Optional[dict]:
    """Return the agent dict if it already exists in the target space, else None.

    Narrow exception handling: only a clean 200 with an empty/filtered list
    counts as "not found". Auth failures (401/403), server errors (5xx),
    and network errors must propagate so the user sees them instead of the
    command silently proceeding to re-create an agent that already exists.
    See axolotl's review of PR #67 for the original repro.
    """
    headers = {"X-Space-Id": space_id}
    r = client._http.get("/api/v1/agents", params={"space_id": space_id}, headers=headers)
    if r.status_code == 404:
        # The space doesn't exist or the caller isn't a member — that's
        # "agent definitely not there", and downstream create will give a
        # cleaner error on the POST.
        return None
    r.raise_for_status()
    payload = client._parse_json(r)
    agents = payload if isinstance(payload, list) else payload.get("agents", [])
    return next((a for a in agents if a.get("name", "").lower() == name.lower()), None)


def _create_agent_in_space(client, *, name: str, space_id: str, description: str | None, model: str | None) -> dict:
    """POST /api/v1/agents with X-Space-Id. This is the creation path proven
    to route through the ALB on prod (POST survives; PATCH/PUT to the same
    prefix don't — see avatar-day PR)."""
    body: dict = {"name": name}
    if description is not None:
        body["description"] = description
    if model is not None:
        body["model"] = model
    if space_id:
        body["space_id"] = space_id
    headers = {"X-Space-Id": space_id} if space_id else None
    r = client._http.post("/api/v1/agents", json=body, headers=headers)
    r.raise_for_status()
    return client._parse_json(r)


def _polish_metadata(
    client,
    *,
    name: str,
    bio: str | None,
    specialization: str | None,
    system_prompt: str | None,
) -> None:
    """PUT /api/v1/agents/manage/{name} for the fields the POST body ignores.

    Skipped silently when nothing to update — this path isn't mandatory.
    """
    fields: dict = {}
    if bio is not None:
        fields["bio"] = bio
    if specialization is not None:
        fields["specialization"] = specialization
    if system_prompt is not None:
        fields["system_prompt"] = system_prompt
    if not fields:
        return
    client.update_agent(name, **fields)


def _mint_agent_pat(
    client,
    *,
    agent_id: str,
    agent_name: str,
    audience: str,
    expires_in_days: int,
    pat_name: str,
    space_id: str,
) -> tuple[str, str]:
    """Mint an agent-bound PAT, preferring the canonical mgmt path, falling
    back to /api/v1/keys when the former isn't routed.

    Returns (token, source) where source is 'mgmt' or 'keys_fallback'.
    """
    # Try canonical path first — works on dev and any env that proxies /credentials/*.
    try:
        data = client.mgmt_issue_agent_pat(
            agent_id,
            name=pat_name,
            expires_in_days=expires_in_days,
            audience=audience,
        )
        token = data.get("token") or data.get("access_token") or ""
        if token:
            return token, "mgmt"
    except httpx.HTTPStatusError as exc:
        if not _is_route_miss(exc):
            raise
    except Exception:
        # Best-effort fallback on any transport exception — we're about to
        # try the other path anyway.
        pass

    # Fallback: /api/v1/keys. Prod-compatible. Ensures the PAT is space-locked
    # and agent-locked so containment survives without the mgmt endpoint.
    data = client.create_key(
        pat_name,
        allowed_agent_ids=[agent_id],
        bound_agent_id=agent_id,
        audience=audience,
        scopes=DEFAULT_KEY_SCOPES,
        space_id=space_id,
    )
    token = data.get("token") or data.get("access_token") or ""
    if not token:
        raise RuntimeError("Mint succeeded but no token field in response")
    return token, "keys_fallback"


def _write_workspace(
    save_to: str,
    *,
    base_url: str,
    agent_name: str,
    agent_id: str,
    space_id: str,
    token: str,
) -> tuple[Path, Path]:
    """Write {save_to}/.ax/{token,config.toml}. Returns (token_path, config_path)."""
    save_dir = Path(save_to).expanduser().resolve()
    ax_dir = save_dir / ".ax" if save_dir.name != ".ax" else save_dir
    ax_dir.mkdir(parents=True, exist_ok=True)

    token_path = ax_dir / "token"
    token_path.write_text(token)
    token_path.chmod(0o600)

    config_path = ax_dir / "config.toml"
    config_content = (
        f'base_url = "{base_url}"\n'
        f'agent_name = "{agent_name}"\n'
        f'agent_id = "{agent_id}"\n'
        f'space_id = "{space_id}"\n'
        f'token_file = "{token_path}"\n'
        f'principal_type = "agent"\n'
    )
    config_path.write_text(config_content)
    config_path.chmod(0o600)
    return token_path, config_path


def _create_profile(
    profile_name: str,
    *,
    base_url: str,
    agent_name: str,
    token_path: Path,
) -> None:
    """Delegate to the same profile writer mint.py uses, for compat with
    ``ax profile verify`` / ``ax profile env``."""
    from .profile import _profile_path, _token_sha256, _workdir_hash, _write_toml

    profile_data = {
        "name": profile_name,
        "base_url": base_url,
        "agent_name": agent_name,
        "token_file": str(token_path.resolve()),
        "token_sha256": _token_sha256(str(token_path)),
        "host_binding": socket.gethostname(),
        "workdir_hash": _workdir_hash(),
        "workdir_path": str(Path.cwd().resolve()),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write_toml(_profile_path(profile_name), profile_data)


def _verify_with_new_token(
    base_url: str,
    token: str,
    agent_name: str,
    agent_id: str,
    space_id: str,
) -> list[dict]:
    """Call /auth/me with the freshly minted PAT (in agent mode) and return
    the resolved ``allowed_spaces`` list so the caller sees containment."""
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Agent-Name": agent_name,
        "X-Agent-Id": agent_id,
        "X-Space-Id": space_id,
    }
    with httpx.Client(base_url=base_url, timeout=10.0) as hc:
        r = hc.get("/auth/me", headers=headers)
        r.raise_for_status()
        data = r.json()
    bound = data.get("bound_agent") or {}
    return bound.get("allowed_spaces") or []


# ── Command ──────────────────────────────────────────────────────────────


def bootstrap_agent(
    name: str = typer.Argument(..., help="Agent name (will be created if missing)."),
    space_id: str = typer.Option(..., "--space-id", help="Target space UUID (required)."),
    description: Optional[str] = typer.Option(None, "--description", "-d", help="Short description."),
    bio: Optional[str] = typer.Option(None, "--bio", "-b", help="Longer bio line."),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="LLM model identifier."),
    specialization: Optional[str] = typer.Option(None, "--specialization", help="Specialization."),
    system_prompt: Optional[str] = typer.Option(None, "--system-prompt", help="System prompt."),
    audience: str = typer.Option(
        "both",
        "--audience",
        help="PAT audience: cli, mcp, or both.",
    ),
    expires_days: int = typer.Option(90, "--expires", help="PAT lifetime in days."),
    save_to: Optional[str] = typer.Option(
        None,
        "--save-to",
        help="Directory to write .ax/config.toml and .ax/token (0600). Parent created if needed.",
    ),
    profile_name: Optional[str] = typer.Option(
        None,
        "--profile",
        help="After minting, create a named profile for use with `ax profile use`.",
    ),
    allow_existing: bool = typer.Option(
        False,
        "--allow-existing",
        help="Reuse the agent if it already exists in the target space (default: abort).",
    ),
    env_name: Optional[str] = typer.Option(
        None,
        "--env",
        help="Use a named user-login environment created with `axctl login --env`.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the plan without touching the API or the filesystem.",
    ),
    as_json: bool = JSON_OPTION,
):
    """Stand up a scoped agent + PAT + workspace in one command.

    Exists because the manual sequence is 15 steps across three APIs and
    two scope vocabularies. This command collapses it and also patches the
    three most common footguns: silent user-env override, the PATCH-vs-PUT
    routing gap on prod, and the ``/credentials/agent-pat`` proxy miss.
    """

    def status(msg: str) -> None:
        if not as_json:
            console.print(msg)

    if env_name:
        os.environ["AX_USER_ENV"] = env_name

    status(_effective_config_line())

    # 1) User PAT gate
    token = resolve_user_token()
    if not token:
        console.print("[red]No user token found.[/red] Run `axctl login` first.")
        raise typer.Exit(1)
    if token.startswith("axp_a_"):
        console.print(
            "[red]Cannot bootstrap with an agent PAT.[/red] "
            "Need a user PAT (axp_u_) to create agents and mint credentials."
        )
        raise typer.Exit(1)
    if not token.startswith("axp_u_"):
        console.print(f"[yellow]Warning:[/yellow] token prefix '{token[:6]}' is not a recognized user PAT. Proceeding.")

    client = get_user_client()
    base_url = client.base_url

    if dry_run:
        plan = {
            "would_create_or_reuse": {"name": name, "space_id": space_id, "description": description, "model": model},
            "would_polish": {"bio": bio, "specialization": specialization, "system_prompt": system_prompt},
            "would_mint_pat": {"audience": audience, "expires_days": expires_days},
            "would_write": {"save_to": save_to, "profile": profile_name},
            "base_url": base_url,
        }
        if as_json:
            print_json(plan)
        else:
            console.print("[yellow]--dry-run:[/yellow] not calling the API.")
            console.print(plan)
        raise typer.Exit(0)

    # 2) Find or create the agent in the target space
    existing = _find_agent_in_space(client, name, space_id)
    if existing:
        if not allow_existing:
            console.print(
                f"[red]Agent '{name}' already exists in space {space_id[:8]}…[/red] "
                f"Pass --allow-existing to reuse, or pick another name."
            )
            raise typer.Exit(2)
        status(f"[yellow]Reusing existing agent[/yellow] {name} ({existing.get('id', '?')[:8]}…)")
        agent_id = existing["id"]
    else:
        status(f"[cyan]Creating agent[/cyan] {name} in space {space_id[:8]}…")
        try:
            created = _create_agent_in_space(client, name=name, space_id=space_id, description=description, model=model)
        except httpx.HTTPStatusError as e:
            handle_error(e)
            raise typer.Exit(1)
        agent_id = created.get("id") or ""
        if not agent_id:
            console.print("[red]Agent creation returned no id.[/red]")
            raise typer.Exit(1)
        status(f"[green]Created[/green] id={agent_id}")

    # 3) Polish metadata via the proxied manage path (optional)
    try:
        _polish_metadata(client, name=name, bio=bio, specialization=specialization, system_prompt=system_prompt)
    except httpx.HTTPStatusError as e:
        console.print(f"[yellow]Metadata polish failed[/yellow] (non-fatal): {e.response.status_code}")

    # 4) Mint the PAT with fallback
    pat_label = f"{name}-runtime"
    status(f"[cyan]Minting agent-bound PAT[/cyan] '{pat_label}' (audience={audience}, expires={expires_days}d)")
    try:
        new_token, pat_source = _mint_agent_pat(
            client,
            agent_id=agent_id,
            agent_name=name,
            audience=audience,
            expires_in_days=expires_days,
            pat_name=pat_label,
            space_id=space_id,
        )
    except httpx.HTTPStatusError as e:
        handle_error(e)
        raise typer.Exit(1)
    status(f"[green]Minted[/green] via {pat_source}")

    # 5) Write workspace
    token_path: Optional[Path] = None
    config_path: Optional[Path] = None
    if save_to:
        token_path, config_path = _write_workspace(
            save_to,
            base_url=base_url,
            agent_name=name,
            agent_id=agent_id,
            space_id=space_id,
            token=new_token,
        )
        status(f"[green]Wrote[/green] {config_path} (0600)")
        status(f"[green]Wrote[/green] {token_path} (0600)")

    # 6) Named profile (optional; needs a token file)
    if profile_name:
        if token_path is None:
            # Put the token in ~/.ax/<name>_token so profile_verify can find it.
            default_dir = Path.home() / ".ax"
            default_dir.mkdir(parents=True, exist_ok=True)
            token_path = default_dir / f"{name}_token"
            token_path.write_text(new_token)
            token_path.chmod(0o600)
        _create_profile(
            profile_name,
            base_url=base_url,
            agent_name=name,
            token_path=token_path,
        )
        status(f"[green]Profile[/green] {profile_name} ready (try: `axctl profile verify {profile_name}`)")

    # 7) Verify with the fresh PAT
    try:
        allowed = _verify_with_new_token(
            base_url=base_url,
            token=new_token,
            agent_name=name,
            agent_id=agent_id,
            space_id=space_id,
        )
    except httpx.HTTPStatusError as e:
        console.print(
            f"[yellow]Verify failed[/yellow]: {e.response.status_code} — token minted but /auth/me refused it."
        )
        allowed = []
    status(f"[green]Verified[/green] allowed_spaces={[s.get('name') or s.get('space_id') for s in allowed]}")

    result = BootstrapResult(
        agent_id=agent_id,
        agent_name=name,
        space_id=space_id,
        base_url=base_url,
        token_path=str(token_path) if token_path else None,
        config_path=str(config_path) if config_path else None,
        profile_name=profile_name,
        pat_source=pat_source,
        allowed_spaces=allowed,
    )
    if as_json:
        print_json(result.as_dict())
    else:
        console.print(
            f"\n[green bold]Done.[/green bold] Agent {name} is live in space "
            f"{space_id[:8]}…  Next: `tmux new -s {name}` + launcher, or "
            f"`axctl profile use {profile_name}` if you passed --profile."
        )
