"""ax agents — agent listing, creation, and management."""

import base64
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import typer

from ..config import (
    _resolve_user_env,
    _user_config_path,
    get_client,
    resolve_agent_name,
    resolve_base_url,
    resolve_gateway_config,
    resolve_space_id,
)
from ..output import JSON_OPTION, console, err_console, handle_error, print_json, print_kv, print_table
from .handoff import _wait_for_handoff_reply

# Backend caps avatar_url at 512 chars (see ax-backend app/api/v1/agents.py
# `AgentCreate.avatar_url: Field(max_length=512)`). Anything longer is silently
# rejected as a 500, so the CLI fails fast with a helpful message instead.
AVATAR_URL_MAX_LENGTH = 512


def _effective_config_line() -> str:
    """One-liner describing the resolved environment, for mutating commands.

    Returned as a string so tests can assert on the format. Callers should
    emit it via :func:`_print_effective_config_line` (which routes to
    stderr) so the preamble never contaminates ``--json`` stdout or any
    piped consumer of the command's output. See
    shared/state/axctl-friction-2026-04-17.md §2.
    """
    base_url = resolve_base_url()
    user_env = _resolve_user_env() or "default"
    user_cfg_path = _user_config_path()
    source = str(user_cfg_path) if user_cfg_path.exists() else "(none)"
    return f"[dim]base_url={base_url}  user_env={user_env}  source={source}[/dim]"


def _print_effective_config_line() -> None:
    """Print the effective-config preamble to **stderr** so stdout stays
    clean for ``--json`` parsers and pipe consumers."""
    err_console.print(_effective_config_line())


def _build_avatar_data_uri_from_file(path: str) -> str:
    """Read an SVG/image file and return a base64 data URI."""
    data = Path(path).read_bytes()
    suffix = Path(path).suffix.lower().lstrip(".")
    mime_map = {
        "svg": "image/svg+xml",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
    }
    mime = mime_map.get(suffix, "application/octet-stream")
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _check_avatar_url_length(avatar_url: str) -> None:
    """Fail fast with a helpful message if avatar_url exceeds the backend cap."""
    if len(avatar_url) > AVATAR_URL_MAX_LENGTH:
        console.print(
            f"[red]avatar_url is {len(avatar_url)} chars; backend caps at "
            f"{AVATAR_URL_MAX_LENGTH}.[/red] Shrink the SVG or host it and pass "
            f"an https:// URL. A raw base64 SVG must be ≤ "
            f"~{int(AVATAR_URL_MAX_LENGTH * 0.7)} bytes before encoding."
        )
        raise typer.Exit(1)


app = typer.Typer(name="agents", help="Agent management", no_args_is_help=True)


def _agent_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("agents", "items", "results"):
        items = payload.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _agent_name_candidates(agent: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("id", "name", "username", "handle", "agent_name", "display_name"):
        value = agent.get(key)
        if isinstance(value, str) and value.strip():
            values.add(value.strip().lower().removeprefix("@"))
    return values


def _find_agent(agents: list[dict[str, Any]], identifier: str) -> dict[str, Any] | None:
    target = identifier.strip().lower().removeprefix("@")
    return next((agent for agent in agents if target in _agent_name_candidates(agent)), None)


def _agent_mention_name(agent: dict[str, Any], fallback: str) -> str:
    for key in ("handle", "username", "agent_name", "name", "display_name", "id"):
        value = agent.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().removeprefix("@")
    return fallback.strip().removeprefix("@")


def _agent_control_state(agent: dict[str, Any]) -> dict[str, Any]:
    control = agent.get("control")
    return control if isinstance(control, dict) else {}


def _agent_control_status(agent: dict[str, Any]) -> str:
    control = _agent_control_state(agent)
    if control.get("is_disabled"):
        return "disabled"
    if control.get("no_reply"):
        return "no_reply"
    return "active"


def _agent_control_reason(agent: dict[str, Any]) -> str:
    control = _agent_control_state(agent)
    if control.get("is_disabled"):
        return str(control.get("disabled_reason") or "Kill switch enabled")
    if control.get("no_reply"):
        return str(control.get("no_reply_reason") or "Agent will not reply")
    return ""


def _agent_is_blocked(agent: dict[str, Any]) -> bool:
    return _agent_control_status(agent) != "active"


def _agent_mesh_role(agent: dict[str, Any]) -> str:
    name = str(agent.get("name") or "").lower()
    origin = str(agent.get("origin") or "").lower()
    agent_type = str(agent.get("agent_type") or "").lower()
    specialization = str(agent.get("specialization") or "").lower()
    description = str(agent.get("description") or "").lower()

    if origin == "space_agent" or agent_type == "space_agent":
        return "space_agent"
    if "supervisor" in name or "supervisor" in specialization or "tech lead" in description:
        return "supervisor_candidate"
    if "sentinel" in name:
        return "domain_sentinel"
    if agent_type == "on_demand":
        return "on_demand_worker"
    return "worker"


def _inferred_contact_mode(agent: dict[str, Any]) -> str:
    origin = str(agent.get("origin") or "").lower()
    agent_type = str(agent.get("agent_type") or "").lower()
    if origin == "space_agent" or agent_type == "space_agent":
        return "space_agent"
    if agent_type == "on_demand":
        return "on_demand"
    return "unknown"


def _recommended_contact(contact_mode: str, mesh_role: str) -> str:
    if contact_mode == "event_listener":
        return "handoff_or_send_wait"
    if contact_mode == "space_agent":
        return "product_request"
    if contact_mode == "on_demand":
        return "task_or_manual_check"
    if mesh_role == "supervisor_candidate":
        return "restore_listener_then_handoff"
    return "ping_then_handoff"


def _probe_agent_contact(
    client,
    *,
    space_id: str,
    target: dict[str, Any],
    timeout: int,
    current_agent_name: str,
) -> dict[str, Any]:
    agent_name = _agent_mention_name(target, str(target.get("name") or "agent"))
    token = f"ping:{uuid.uuid4().hex[:8]}"
    content = (
        f"@{agent_name} Contact-mode ping from axctl. "
        f"Please reply with `{token}` if this mention reached a live listener."
    )
    started_at = time.time()

    sent_data = client.send_message(space_id, content)
    sent = sent_data.get("message", sent_data)
    sent_message_id = str(sent.get("id") or sent_data.get("id") or "")
    reply = None
    if sent_message_id and timeout > 0:
        reply = _wait_for_handoff_reply(
            client,
            space_id=space_id,
            agent_name=agent_name,
            sent_message_id=sent_message_id,
            token=token,
            current_agent_name=current_agent_name,
            started_at=started_at,
            timeout=timeout,
            require_completion=True,
        )

    return {
        "sent_message_id": sent_message_id,
        "ping_token": token,
        "listener_status": "replied" if reply else "no_reply",
        "contact_mode": "event_listener" if reply else "unknown_or_not_listening",
        "reply": reply,
    }


def _discover_agent_row(agent: dict[str, Any], probe: dict[str, Any] | None = None) -> dict[str, Any]:
    mesh_role = _agent_mesh_role(agent)
    control_status = _agent_control_status(agent)
    control_reason = _agent_control_reason(agent)
    if control_status != "active":
        contact_mode = "blocked_by_control"
        listener_status = control_status
    else:
        contact_mode = probe["contact_mode"] if probe else _inferred_contact_mode(agent)
        listener_status = probe["listener_status"] if probe else "not_probed"
    warning = ""
    if mesh_role == "supervisor_candidate" and contact_mode != "event_listener":
        warning = "supervisor_candidate_not_live"
    if control_status != "active":
        warning = "agent_control_blocks_delivery"
    return {
        "name": _agent_mention_name(agent, str(agent.get("name") or "agent")),
        "agent_id": agent.get("id"),
        "origin": agent.get("origin"),
        "agent_type": agent.get("agent_type"),
        "roster_status": agent.get("status"),
        "control_status": control_status,
        "control_reason": control_reason,
        "mesh_role": mesh_role,
        "listener_status": listener_status,
        "contact_mode": contact_mode,
        "recommended_contact": "reenable_before_contact"
        if control_status != "active"
        else _recommended_contact(contact_mode, mesh_role),
        "sent_message_id": probe.get("sent_message_id") if probe else None,
        "ping_token": probe.get("ping_token") if probe else None,
        "warning": warning,
    }


@app.command("list")
def list_agents(
    space_id: str = typer.Option(None, "--space-id", help="Override default space"),
    limit: int = typer.Option(500, "--limit", help="Max agents to return"),
    availability: bool = typer.Option(
        False, "--availability", help="Include resolved AVAIL-CONTRACT v4 fields per row"
    ),
    filter_: str = typer.Option(
        None,
        "--filter",
        help="Filter (with --availability): available_now | gateway_connected | cloud_agent | disabled | recently_active",
    ),
    as_json: bool = JSON_OPTION,
):
    """List agents in the current space.

    With ``--availability``, calls ``/api/v1/agents/availability`` (the
    AVAIL-CONTRACT-001 bulk endpoint) and renders the resolved ``agent_state``
    DTO per row: badge_state, badge_label, connection_path, expected_response,
    confidence, last_seen. Falls back gracefully to the legacy ``/agents`` shape
    if ``/availability`` returns 404.
    """
    gateway_cfg = resolve_gateway_config()
    if gateway_cfg:
        from .messages import _gateway_local_call

        if availability:
            try:
                data = _gateway_local_call(
                    gateway_cfg=gateway_cfg,
                    method="list_agents_availability",
                    args={"space_id": space_id, "filter_": filter_},
                    space_id=space_id,
                )
                agents = _normalize_availability_rows(data)
            except typer.BadParameter:
                fallback = _gateway_local_call(
                    gateway_cfg=gateway_cfg,
                    method="list_agents",
                    args={"space_id": space_id, "limit": limit},
                    space_id=space_id,
                )
                agents = fallback if isinstance(fallback, list) else fallback.get("agents", [])
                for row in agents:
                    row.setdefault("_legacy", True)
        else:
            if filter_:
                raise typer.BadParameter("--filter requires --availability")
            data = _gateway_local_call(
                gateway_cfg=gateway_cfg,
                method="list_agents",
                args={"space_id": space_id, "limit": limit},
                space_id=space_id,
            )
            agents = data if isinstance(data, list) else data.get("agents", [])
    else:
        client = get_client()
        sid = resolve_space_id(client, explicit=space_id)
        if availability:
            try:
                data = client.list_agents_availability(space_id=sid, filter_=filter_)
                agents = _normalize_availability_rows(data)
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 404:
                    handle_error(e)
                # Fallback: backend hasn't shipped /availability yet.
                try:
                    fallback = client.list_agents(space_id=sid, limit=limit)
                except httpx.HTTPStatusError as e2:
                    handle_error(e2)
                agents = fallback if isinstance(fallback, list) else fallback.get("agents", [])
                for row in agents:
                    row.setdefault("_legacy", True)
        else:
            if filter_:
                raise typer.BadParameter("--filter requires --availability")
            try:
                data = client.list_agents(space_id=sid, limit=limit)
            except httpx.HTTPStatusError as e:
                handle_error(e)
            agents = data if isinstance(data, list) else data.get("agents", [])

    if as_json:
        print_json(agents)
        return

    if availability:
        rows = []
        for a in agents:
            rows.append(
                {
                    "name": a.get("name") or a.get("agent_name") or "",
                    "badge": a.get("badge_label") or _legacy_badge(a),
                    "path": _short_path(a.get("connection_path")),
                    "expected": a.get("expected_response") or "—",
                    "confidence": a.get("confidence") or a.get("presence_confidence") or "—",
                    "last_seen": a.get("last_seen_at") or a.get("last_seen") or a.get("last_active") or "—",
                }
            )
        print_table(
            ["Name", "Badge", "Path", "Expected", "Confidence", "Last seen"],
            rows,
            keys=["name", "badge", "path", "expected", "confidence", "last_seen"],
        )
    else:
        rows = []
        for agent in agents:
            row = dict(agent)
            row["control_status"] = _agent_control_status(agent)
            row["control_reason"] = _agent_control_reason(agent)
            rows.append(row)
        print_table(
            ["ID", "Name", "Status", "Control", "Reason"],
            rows,
            keys=["id", "name", "status", "control_status", "control_reason"],
        )


def _normalize_availability_rows(payload) -> list[dict]:
    """Unwrap the availability bulk response into a list of flat rows.

    The bulk endpoint returns either a list of agent_state envelopes or a
    dict like ``{agents: [...], availability: [...]}`` per the spec — we
    accept both shapes and unwrap each item's ``agent_state`` sub-object
    if present so downstream rendering sees the v4 fields at top level.
    """
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("agents") or payload.get("availability") or payload.get("items") or []
    else:
        return []
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if "agent_state" in item:
            row = dict(item.get("agent_state") or {})
            if "raw_presence" in item:
                row["_raw_presence"] = item["raw_presence"]
            if "control" in item:
                row["_control"] = item["control"]
        else:
            row = dict(item)
        rows.append(row)
    return rows


def _short_path(connection_path: str | None) -> str:
    """Map connection_path enum to compact column label."""
    return {
        "gateway_managed": "Gateway",
        "mcp_only": "Cloud",
        "direct_cli": "CLI",
        "direct_sse": "SSE",
        "unknown": "—",
    }.get(connection_path or "", "—")


def _legacy_badge(row: dict) -> str:
    """Synthesize a coarse badge label when the backend hasn't shipped v4 fields yet."""
    if row.get("_legacy"):
        # Pure legacy /agents response — no presence info at all on the row.
        return row.get("status") or "—"
    presence = (row.get("presence") or "").lower()
    if presence == "online":
        return "Live"
    if presence == "offline":
        return "Offline"
    return presence or "—"


@app.command("ping")
def ping_agent(
    agent: str = typer.Argument(..., help="Agent name, @handle, or UUID"),
    timeout: int = typer.Option(30, "--timeout", "-t", help="Seconds to wait for a reply"),
    space_id: str = typer.Option(None, "--space-id", help="Override default space"),
    as_json: bool = JSON_OPTION,
):
    """Probe whether an agent is currently listening for mention events."""
    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)

    try:
        agents_data = client.list_agents(space_id=sid, limit=500)
    except httpx.HTTPStatusError as exc:
        handle_error(exc)
    target = _find_agent(_agent_items(agents_data), agent)
    if not target:
        typer.echo(f"Error: No visible agent found for '{agent}'.", err=True)
        raise typer.Exit(1)

    agent_name = _agent_mention_name(target, agent)
    control_status = _agent_control_status(target)
    control_reason = _agent_control_reason(target)
    if _agent_is_blocked(target):
        probe = {
            "sent_message_id": None,
            "ping_token": None,
            "listener_status": control_status,
            "contact_mode": "blocked_by_control",
            "reply": None,
        }
    else:
        try:
            probe = _probe_agent_contact(
                client,
                space_id=sid,
                target=target,
                timeout=timeout,
                current_agent_name=resolve_agent_name(client=client) or "",
            )
        except httpx.HTTPStatusError as exc:
            handle_error(exc)

    result = {
        "agent": agent_name,
        "agent_id": target.get("id"),
        "origin": target.get("origin"),
        "agent_type": target.get("agent_type"),
        "roster_status": target.get("status"),
        "control_status": control_status,
        "control_reason": control_reason,
        **probe,
    }

    if as_json:
        print_json(result)
        return

    if probe["reply"]:
        console.print(f"[green]@{agent_name} replied.[/green] contact_mode=event_listener")
    else:
        console.print(
            f"[yellow]No @{agent_name} reply within {timeout}s.[/yellow] contact_mode=unknown_or_not_listening"
        )
    print_kv(
        {
            "agent_id": result["agent_id"],
            "origin": result["origin"],
            "agent_type": result["agent_type"],
            "roster_status": result["roster_status"],
            "sent_message_id": result["sent_message_id"],
            "ping_token": result["ping_token"],
        }
    )


@app.command("discover")
def discover_agents(
    agents: list[str] = typer.Argument(None, help="Optional agent names, @handles, or UUIDs to inspect"),
    ping: bool = typer.Option(False, "--ping/--no-ping", help="Send ping probes to classify live listeners"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="Seconds to wait per ping when --ping is enabled"),
    space_id: str = typer.Option(None, "--space-id", help="Override default space"),
    limit: int = typer.Option(500, "--limit", help="Max roster agents to inspect"),
    as_json: bool = JSON_OPTION,
):
    """Discover agent mesh roles, listener state, and safe contact method."""
    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)
    try:
        agents_data = client.list_agents(space_id=sid, limit=limit)
    except httpx.HTTPStatusError as exc:
        handle_error(exc)

    roster = _agent_items(agents_data)
    selected: list[dict[str, Any]] = []
    if agents:
        for identifier in agents:
            match = _find_agent(roster, identifier)
            if not match:
                typer.echo(f"Error: No visible agent found for '{identifier}'.", err=True)
                raise typer.Exit(1)
            selected.append(match)
    else:
        selected = roster

    current_agent_name = resolve_agent_name(client=client) or ""
    rows: list[dict[str, Any]] = []
    for target in selected:
        probe = None
        if ping and not _agent_is_blocked(target):
            try:
                probe = _probe_agent_contact(
                    client,
                    space_id=sid,
                    target=target,
                    timeout=timeout,
                    current_agent_name=current_agent_name,
                )
            except httpx.HTTPStatusError as exc:
                handle_error(exc)
        rows.append(_discover_agent_row(target, probe))

    summary = {
        "total": len(rows),
        "event_listeners": sum(1 for row in rows if row["contact_mode"] == "event_listener"),
        "unknown_or_not_listening": sum(1 for row in rows if row["contact_mode"] == "unknown_or_not_listening"),
        "supervisor_candidates": sum(1 for row in rows if row["mesh_role"] == "supervisor_candidate"),
        "supervisor_candidates_not_live": sum(1 for row in rows if row["warning"] == "supervisor_candidate_not_live"),
        "blocked_by_control": sum(1 for row in rows if row["contact_mode"] == "blocked_by_control"),
        "pinged": ping,
    }
    result = {"space_id": sid, "summary": summary, "agents": rows}

    if as_json:
        print_json(result)
        return

    print_table(
        ["Name", "Role", "Roster", "Listener", "Contact Mode", "Recommended", "Warning"],
        rows,
        keys=[
            "name",
            "mesh_role",
            "roster_status",
            "listener_status",
            "contact_mode",
            "recommended_contact",
            "warning",
        ],
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
        # Try management API first (exchange-based auth),
        # fall back to legacy /api/v1/agents if it returns HTML.
        if hasattr(client, "_exchanger") and client._exchanger:
            try:
                data = client.mgmt_create_agent(
                    name,
                    description=description,
                    system_prompt=system_prompt,
                    model=model,
                    space_id=space_id,
                )
            except httpx.HTTPStatusError:
                data = client.create_agent(
                    name,
                    description=description,
                    system_prompt=system_prompt,
                    model=model,
                    space_id=space_id,
                    enable_cloud_agent=cloud,
                    can_manage_agents=can_manage_agents,
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
    avatar_url: str = typer.Option(
        None,
        "--avatar-url",
        help="Set agent avatar (data URI or https URL). ≤ 512 chars.",
    ),
    avatar_file: str = typer.Option(
        None,
        "--avatar-file",
        help="Read avatar from a file (svg/png/jpg/gif/webp) and set it. Mutually exclusive with --avatar-url.",
    ),
    as_json: bool = JSON_OPTION,
):
    """Update an agent's metadata.

    Examples:
        ax agents update backend_sentinel --type sentinel --model claude-sonnet-4-6
        ax agents update anvil --bio "Infra and ops" --specialization "server management"
        ax agents update axolotl --avatar-file notes/axolotl.svg
        ax agents update axolotl --avatar-url "data:image/svg+xml;base64,..."
    """
    if avatar_url is not None and avatar_file is not None:
        console.print("[red]--avatar-url and --avatar-file are mutually exclusive.[/red]")
        raise typer.Exit(1)

    if avatar_file is not None:
        avatar_url = _build_avatar_data_uri_from_file(avatar_file)

    if avatar_url is not None:
        _check_avatar_url_length(avatar_url)

    _print_effective_config_line()

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
    if avatar_url is not None:
        fields["avatar_url"] = avatar_url

    if not fields:
        typer.echo(
            "Nothing to update. Use --type, --model, --bio, --description, --status, --avatar-url, --avatar-file, etc.",
            err=True,
        )
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


@app.command("check")
def check(
    name_or_id: str = typer.Argument(..., help="Agent name or UUID"),
    as_json: bool = JSON_OPTION,
):
    """Check single-agent presence + AVAIL-CONTRACT availability.

    Forward-compat consumer of the AVAIL-CONTRACT-001 resolved DTO. Today
    the backend returns a basic presence shape (``presence``, ``responsive``,
    ``last_active``); when backend ships the rich ``agent_state`` DTO with
    ``expected_response`` / ``badge_state`` / ``connection_path`` /
    ``pre_send_warning``, the same CLI command renders the new fields
    transparently — no flag flip needed.
    """
    client = get_client()
    try:
        record = client.get_agent_presence(name_or_id)
    except httpx.HTTPStatusError as exc:
        handle_error(exc)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    if as_json:
        print_json(record)
        return

    name = record.get("name") or name_or_id
    presence = record.get("presence", "unknown")
    responsive = record.get("responsive")
    last_active = record.get("last_active") or "—"
    agent_type = record.get("agent_type") or "—"

    # Forward-compat AVAIL-CONTRACT v4 fields (rendered when backend provides them)
    expected_response = record.get("expected_response")
    badge_state = record.get("badge_state")
    badge_label = record.get("badge_label")
    connection_path = record.get("connection_path")
    confidence = record.get("confidence") or record.get("presence_confidence")
    unavailable_reason = record.get("unavailable_reason")
    status_explanation = record.get("status_explanation")
    pre_send_warning = record.get("pre_send_warning")

    # Banner: prefer rich badge if available, else fall back to basic presence.
    if badge_label:
        color = (
            "green"
            if badge_state == "live"
            else (
                "yellow"
                if badge_state == "routable_delayed"
                else (
                    "blue"
                    if badge_state == "queued_only"
                    else ("red" if badge_state in ("blocked", "offline") else "dim")
                )
            )
        )
        console.print(f"[bold {color}]{badge_label}[/bold {color}]  @{name}")
    elif presence == "online":
        console.print(f"[bold green]ONLINE[/bold green]  @{name}")
    else:
        console.print(f"[bold dim]OFFLINE[/bold dim]  @{name}")

    rows = [{"field": "name", "value": name}]
    rows.append({"field": "presence", "value": presence})
    if responsive is not None:
        rows.append({"field": "responsive", "value": str(responsive)})
    rows.append({"field": "last_active", "value": last_active})
    rows.append({"field": "agent_type", "value": agent_type})

    # Forward-compat fields (only render when present)
    for field, value in (
        ("expected_response", expected_response),
        ("badge_state", badge_state),
        ("connection_path", connection_path),
        ("confidence", confidence),
        ("unavailable_reason", unavailable_reason),
    ):
        if value is not None:
            rows.append({"field": field, "value": str(value)})

    print_table(["Field", "Value"], rows, keys=["field", "value"])

    if status_explanation:
        console.print()
        console.print(f"[dim]{status_explanation}[/dim]")

    if pre_send_warning and isinstance(pre_send_warning, dict):
        severity = pre_send_warning.get("severity", "info")
        title = pre_send_warning.get("title", "")
        body = pre_send_warning.get("body", "")
        color = {"error": "red", "warning": "yellow", "info": "cyan"}.get(severity, "dim")
        console.print()
        console.print(f"[bold {color}]{title}[/bold {color}]")
        if body:
            console.print(body)


placement_app = typer.Typer(
    name="placement",
    help="Agent space-placement management (per GATEWAY-PLACEMENT-POLICY-001)",
    no_args_is_help=True,
)
app.add_typer(placement_app, name="placement")


@placement_app.command("get")
def placement_get(
    name_or_id: str = typer.Argument(..., help="Agent name or UUID"),
    as_json: bool = JSON_OPTION,
):
    """Show an agent's current space placement.

    Today renders the basic placement shape (``space_id``, ``pinned``,
    ``allowed_spaces`` when present). When backend implements the full
    GATEWAY-PLACEMENT-POLICY-001 machinery (``policy_kind`` /
    ``placement_state`` / ``policy_revision``), those fields surface
    transparently — same forward-compat pattern as ``ax agents check``.
    """
    client = get_client()
    try:
        record = client.get_agent_placement(name_or_id)
    except httpx.HTTPStatusError as exc:
        handle_error(exc)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    if as_json:
        print_json(record)
        return

    name = record.get("name") or name_or_id
    space_id = record.get("space_id") or "—"
    pinned = record.get("pinned")
    pinned_str = "yes" if pinned else ("no" if pinned is False else "—")
    allowed = record.get("allowed_spaces")

    console.print(f"[bold]@{name}[/bold] placement")
    rows = [
        {"field": "space_id", "value": space_id},
        {"field": "pinned", "value": pinned_str},
    ]
    if allowed:
        rows.append({"field": "allowed_spaces", "value": ", ".join(str(s) for s in allowed)})

    # Forward-compat: render these only when backend provides them
    placement = record.get("placement")
    if isinstance(placement, dict):
        for k in ("policy_kind", "current_space", "current_space_set_by", "policy_revision"):
            v = placement.get(k)
            if v is not None:
                rows.append({"field": f"placement.{k}", "value": str(v)})
    placement_state = record.get("placement_state")
    if placement_state:
        rows.append({"field": "placement_state", "value": str(placement_state)})

    print_table(["Field", "Value"], rows, keys=["field", "value"])


@placement_app.command("set")
def placement_set(
    name_or_id: str = typer.Argument(..., help="Agent name or UUID"),
    space_id: str = typer.Option(..., "--space-id", help="Target space UUID (or short prefix)"),
    pinned: bool = typer.Option(False, "--pinned/--no-pinned", help="Lock the agent to this space"),
    as_json: bool = JSON_OPTION,
):
    """Set an agent's default space + pinned status.

    Calls POST ``/api/v1/agents/{id}/placement`` with ``{space_id, pinned}``.
    Requires ownership of the agent + membership in the target space.

    When the agent is Gateway-managed, the placement change propagates
    to the runtime per GATEWAY-PLACEMENT-POLICY-001's transition flow.
    For direct-mode agents, the change applies to the agent record but
    the running listener may need a restart to pick up the new space.
    """
    client = get_client()
    try:
        result = client.set_agent_placement(name_or_id, space_id=space_id, pinned=pinned)
    except httpx.HTTPStatusError as exc:
        handle_error(exc)

    if as_json:
        print_json(result)
        return

    record = result.get("agent", result) if isinstance(result, dict) else {}
    name = record.get("name") or name_or_id
    new_space = record.get("space_id") or space_id
    pinned_str = "pinned" if record.get("pinned") else "unpinned"
    console.print(f"[green]Updated[/green] @{name} → space={new_space} ({pinned_str})")


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
        _check_avatar_url_length(data_uri)
        _print_effective_config_line()
        try:
            # Find the agent by name
            agents_data = client.list_agents()
            agents_list = agents_data if isinstance(agents_data, list) else agents_data.get("agents", [])
            target = next((a for a in agents_list if a.get("name", "").lower() == agent.lower()), None)
            if not target:
                console.print(f"[red]Agent '{agent}' not found[/red]")
                raise typer.Exit(1)
            # Update avatar_url. Use PUT — the ALB on prod proxies PUT/GET/POST
            # but not PATCH on /api/v1/agents/{id} (see friction-2026-04-17 §7).
            # The backend PUT handler accepts avatar_url the same way PATCH does.
            r = client._http.put(f"/api/v1/agents/{target['id']}", json={"avatar_url": data_uri})
            r.raise_for_status()
            console.print(f"[green]Avatar set for @{agent}[/green]")
        except httpx.HTTPStatusError as e:
            handle_error(e)
    elif as_json:
        import json

        print(json.dumps({"name": agent, "svg": svg, "data_uri": avatar_data_uri(agent, agent_type, size)}))
    else:
        print(svg)
