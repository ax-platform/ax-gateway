"""ax gateway — local Gateway control plane."""

from __future__ import annotations

import getpass
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import tomllib
import uuid
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import typer
from rich import box
from rich.columns import Columns
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .. import gateway as gateway_core
from ..client import AxClient
from ..commands import auth as auth_cmd
from ..commands.bootstrap import (
    _create_agent_in_space,
    _find_agent_in_space,
    _mint_agent_pat,
    _polish_metadata,
)
from ..config import resolve_space_id, resolve_user_base_url, resolve_user_token
from ..gateway import (
    GatewayDaemon,
    _is_passive_runtime,
    active_gateway_pid,
    active_gateway_pids,
    active_gateway_ui_pid,
    active_gateway_ui_pids,
    agent_dir,
    agent_token_path,
    annotate_runtime_health,
    apply_entry_current_space,
    approve_gateway_approval,
    archive_stale_gateway_approvals,
    clear_gateway_ui_state,
    daemon_log_path,
    daemon_status,
    deny_gateway_approval,
    ensure_gateway_identity_binding,
    ensure_local_asset_binding,
    evaluate_runtime_attestation,
    find_agent_entry,
    find_agent_entry_by_ref,
    gateway_dir,
    gateway_environment,
    get_gateway_approval,
    hermes_setup_status,
    infer_asset_descriptor,
    issue_local_session,
    list_gateway_approvals,
    load_agent_pending_messages,
    load_gateway_managed_agent_token,
    load_gateway_registry,
    load_gateway_session,
    load_recent_gateway_activity,
    ollama_setup_status,
    record_gateway_activity,
    remove_agent_entry,
    save_agent_pending_messages,
    save_gateway_registry,
    save_gateway_session,
    ui_log_path,
    ui_status,
    upsert_agent_entry,
    verify_local_session_token,
    write_gateway_ui_state,
)
from ..gateway_runtime_types import (
    agent_template_definition,
    agent_template_list,
    runtime_type_definition,
    runtime_type_list,
)
from ..output import JSON_OPTION, console, err_console, print_json, print_table

app = typer.Typer(name="gateway", help="Run the local Gateway control plane", no_args_is_help=True)
agents_app = typer.Typer(name="agents", help="Manage Gateway-controlled agents", no_args_is_help=True)
spaces_app = typer.Typer(name="spaces", help="Manage Gateway current space", no_args_is_help=True)
approvals_app = typer.Typer(name="approvals", help="Review and decide Gateway approval requests", no_args_is_help=True)
runtime_app = typer.Typer(
    name="runtime", help="Install and inspect runtime templates (Hermes, etc.)", no_args_is_help=True
)
local_app = typer.Typer(name="local", help="Connect local pass-through agents to Gateway", no_args_is_help=True)
_ATTACHED_SESSION_PROCESSES: list[subprocess.Popen[bytes]] = []
app.add_typer(agents_app, name="agents")
app.add_typer(spaces_app, name="spaces")
app.add_typer(approvals_app, name="approvals")
app.add_typer(runtime_app, name="runtime")
app.add_typer(local_app, name="local")

_STATE_STYLES = {
    "running": "green",
    "starting": "cyan",
    "reconnecting": "yellow",
    "stale": "yellow",
    "error": "red",
    "stopped": "dim",
}
_PRESENCE_STYLES = {
    "IDLE": "green",
    "QUEUED": "cyan",
    "WORKING": "green",
    "BLOCKED": "yellow",
    "STALE": "yellow",
    "OFFLINE": "dim",
    "ERROR": "red",
}
_CONFIDENCE_STYLES = {
    "HIGH": "green",
    "MEDIUM": "cyan",
    "LOW": "yellow",
    "BLOCKED": "red",
}
_PRESENCE_ORDER = {
    "ERROR": 0,
    "BLOCKED": 1,
    "WORKING": 2,
    "QUEUED": 3,
    "STALE": 4,
    "OFFLINE": 5,
    "IDLE": 6,
}

_UNSET = object()


def _resolve_gateway_login_token(explicit_token: str | None) -> str:
    if explicit_token and explicit_token.strip():
        return auth_cmd._resolve_login_token(explicit_token)
    existing = resolve_user_token()
    if existing:
        err_console.print("[cyan]Using existing axctl user login for Gateway bootstrap.[/cyan]")
        return existing
    return auth_cmd._resolve_login_token(None)


def _load_gateway_user_client() -> AxClient:
    session = load_gateway_session()
    if not session:
        err_console.print("[red]Gateway is not logged in.[/red] Run `ax gateway login` first.")
        raise typer.Exit(1)
    token = str(session.get("token") or "")
    if not token:
        err_console.print("[red]Gateway session is missing its bootstrap token.[/red]")
        raise typer.Exit(1)
    if not token.startswith("axp_u_"):
        err_console.print("[red]Gateway bootstrap currently requires a user PAT (axp_u_).[/red]")
        raise typer.Exit(1)
    return AxClient(base_url=str(session.get("base_url") or auth_cmd.DEFAULT_LOGIN_BASE_URL), token=token)


def _load_gateway_session_or_exit() -> dict:
    session = load_gateway_session()
    if not session:
        err_console.print("[red]Gateway is not logged in.[/red] Run `ax gateway login` first.")
        raise typer.Exit(1)
    return session


def _save_agent_token(name: str, token: str) -> Path:
    token_path = agent_token_path(name)
    token_path.write_text(token.strip() + "\n")
    token_path.chmod(0o600)
    return token_path


def _load_managed_agent_or_exit(name: str) -> dict:
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
        raise typer.Exit(1)
    return entry


def _registry_ref_for_agent(registry: dict, target: dict) -> str | None:
    target_name = str(target.get("name") or "").lower()
    target_install_id = str(target.get("install_id") or "")
    for index, entry in enumerate(registry.get("agents", []), start=1):
        if (
            entry is target
            or (target_name and str(entry.get("name") or "").lower() == target_name)
            or (target_install_id and str(entry.get("install_id") or "") == target_install_id)
        ):
            return f"#{index}"
    return None


def _with_registry_refs(registry: dict, agent: dict) -> dict:
    annotated = dict(agent)
    ref = _registry_ref_for_agent(registry, agent)
    if ref:
        annotated["registry_ref"] = ref
        annotated["registry_index"] = int(ref.lstrip("#"))
    install_id = str(annotated.get("install_id") or "")
    if install_id:
        annotated["registry_code"] = install_id[:8]
    return annotated


def _load_managed_agent_client(entry: dict) -> AxClient:
    try:
        token = load_gateway_managed_agent_token(entry)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    return AxClient(
        base_url=str(entry.get("base_url") or ""),
        token=token,
        agent_name=str(entry.get("name") or ""),
        agent_id=str(entry.get("agent_id") or "") or None,
    )


def _local_process_fingerprint(
    *,
    agent_name: str,
    cwd: str | None = None,
    pid: int | None = None,
    exe_path: str | None = None,
) -> dict:
    resolved_pid = int(pid or os.getpid())
    resolved_cwd = str(Path(cwd or os.getcwd()).expanduser().resolve())
    resolved_exe = str(Path(exe_path or sys.executable).expanduser())
    fingerprint = {
        "agent_name": agent_name,
        "pid": resolved_pid,
        "parent_pid": os.getppid() if resolved_pid == os.getpid() else None,
        "cwd": resolved_cwd,
        "exe_path": resolved_exe,
        "user": getpass.getuser(),
        "platform": sys.platform,
    }
    try:
        fingerprint["exe_sha256"] = gateway_core._file_sha256(Path(resolved_exe))  # type: ignore[attr-defined]
    except Exception:
        fingerprint["exe_sha256"] = None
    return fingerprint


def _local_trust_signature(agent_name: str, fingerprint: dict) -> str:
    payload = {
        "agent_name": agent_name,
        "exe_path": str(fingerprint.get("exe_path") or ""),
        "cwd": str(fingerprint.get("cwd") or ""),
        "user": str(fingerprint.get("user") or ""),
    }
    return gateway_core._payload_hash(payload)  # type: ignore[attr-defined]


def _local_origin_signature(fingerprint: dict) -> str:
    """Stable origin key that intentionally excludes the mutable agent handle."""
    payload = {
        "exe_path": str(fingerprint.get("exe_path") or ""),
        "cwd": str(fingerprint.get("cwd") or ""),
        "user": str(fingerprint.get("user") or ""),
    }
    return gateway_core._payload_hash(payload)  # type: ignore[attr-defined]


def _find_local_origin_collision(registry: dict, *, fingerprint: dict, requested_name: str) -> dict | None:
    origin_signature = _local_origin_signature(fingerprint)
    requested_normalized = requested_name.strip().lower()
    for candidate in registry.get("agents") or []:
        candidate_name = str(candidate.get("name") or "").strip()
        if not candidate_name or candidate_name.lower() == requested_normalized:
            continue
        candidate_fingerprint = candidate.get("local_fingerprint")
        if not isinstance(candidate_fingerprint, dict):
            continue
        if _local_origin_signature(candidate_fingerprint) == origin_signature:
            return candidate
    return None


def _local_fingerprint_verification(fingerprint: dict) -> dict:
    """Best-effort OS cross-check for the self-reported local fingerprint."""
    pid = str(fingerprint.get("pid") or "").strip()
    if not pid or not pid.isdigit():
        return {"status": "unverified", "reason": "missing_pid"}
    proc_root = Path("/proc") / pid
    if not proc_root.exists():
        return {"status": "unavailable", "reason": "procfs_unavailable"}
    observed: dict[str, str] = {}
    try:
        observed["exe_path"] = str((proc_root / "exe").resolve())
    except OSError:
        pass
    try:
        observed["cwd"] = str((proc_root / "cwd").resolve())
    except OSError:
        pass
    mismatches = []
    for key in ("exe_path", "cwd"):
        reported = str(fingerprint.get(key) or "")
        if observed.get(key) and reported and observed[key] != reported:
            mismatches.append({"field": key, "reported": reported, "observed": observed[key]})
    if mismatches:
        return {"status": "mismatch", "reason": "fingerprint_mismatch", "observed": observed, "mismatches": mismatches}
    return {"status": "verified", "observed": observed}


def _connect_local_pass_through_agent(
    *,
    agent_name: str | None = None,
    registry_ref: str | None = None,
    fingerprint: dict,
    space_id: str | None = None,
    auto_create: bool = True,
) -> dict:
    requested_name = str(agent_name or "").strip()
    requested_ref = str(registry_ref or "").strip()
    if not requested_name and not requested_ref:
        raise ValueError("Local agent name or registry ref is required.")
    registry = load_gateway_registry()
    entry = find_agent_entry_by_ref(registry, requested_ref) if requested_ref else None
    if requested_ref and entry is None:
        raise LookupError(f"Gateway registry agent not found: {requested_ref}")
    if entry is not None and str(entry.get("template_id") or "").strip() not in {"", "pass_through"}:
        raise ValueError("registry_ref_not_attachable")
    normalized_name = str(entry.get("name") if entry else requested_name).strip()
    if not normalized_name:
        raise ValueError("Local agent name is required.")
    verification = _local_fingerprint_verification(fingerprint)
    if verification.get("status") == "mismatch":
        record_gateway_activity(
            "local_connect_fingerprint_mismatch",
            agent_name=normalized_name,
            fingerprint=fingerprint,
            verification=verification,
        )
        raise ValueError("fingerprint_mismatch")

    if entry is None:
        collision = _find_local_origin_collision(
            registry,
            fingerprint=fingerprint,
            requested_name=normalized_name,
        )
        if collision is not None:
            existing_name = str(collision.get("name") or "").strip()
            raise ValueError(
                "Gateway identity mismatch: "
                f"this local origin is already registered as @{existing_name}. "
                "Use that repo-local .ax/config.toml identity, reconnect by registry ref, "
                "or remove/rename the existing registry row before requesting a new agent name."
            )

    if entry is None:
        entry = find_agent_entry(registry, normalized_name)
    if entry is None:
        if not auto_create:
            raise LookupError(f"Managed agent not found: {normalized_name}")
        entry = _register_managed_agent(
            name=normalized_name,
            template_id="pass_through",
            workdir=str(fingerprint.get("cwd") or "").strip() or None,
            space_id=str(space_id or "").strip() or None,
            start=True,
        )
        registry = load_gateway_registry()
        entry = find_agent_entry(registry, normalized_name) or entry
    elif not space_id:
        _hydrate_entry_space_from_database(registry, entry)

    entry["template_id"] = entry.get("template_id") or "pass_through"
    entry["template_label"] = entry.get("template_label") or "Pass-through"
    entry["runtime_type"] = entry.get("runtime_type") or "inbox"
    if space_id:
        entry["space_id"] = str(space_id).strip()
    entry["workdir"] = str(fingerprint.get("cwd") or entry.get("workdir") or "").strip() or None
    entry["local_connection_mode"] = "pass_through"
    entry["local_auth_mode"] = "gateway-session"
    entry["credential_source"] = "gateway"
    entry["local_fingerprint"] = dict(fingerprint)
    entry["local_trust_signature"] = _local_trust_signature(normalized_name, fingerprint)
    entry["local_fingerprint_verification"] = verification
    entry["last_local_connect_at"] = datetime.now(timezone.utc).isoformat()
    if requested_ref:
        entry["last_local_registry_ref"] = requested_ref
    entry.update(evaluate_runtime_attestation(registry, entry))
    save_gateway_registry(registry)

    approved = (
        str(entry.get("approval_state") or "").lower() in {"not_required", "approved"}
        and str(entry.get("attestation_state") or "").lower() == "verified"
    )
    payload = {
        "status": "approved" if approved else "pending",
        "agent": _with_registry_refs(registry, annotate_runtime_health(entry, registry=registry)),
        "approval_id": entry.get("approval_id"),
        "registry_ref": _registry_ref_for_agent(registry, entry),
        "fingerprint": fingerprint,
        "fingerprint_verification": verification,
    }
    if entry.get("approval_id"):
        try:
            approval = get_gateway_approval(str(entry["approval_id"]))
            payload["approval"] = {
                "approval_id": approval.get("approval_id"),
                "approval_kind": approval.get("approval_kind"),
                "action": approval.get("action"),
                "risk": approval.get("risk"),
                "resource": approval.get("resource"),
                "reason": approval.get("reason"),
                "requested_at": approval.get("requested_at"),
                "status": approval.get("status"),
            }
        except LookupError:
            pass
    if approved:
        session_payload = issue_local_session(registry, entry, fingerprint=fingerprint)
        save_gateway_registry(registry)
        payload["session_token"] = session_payload["session_token"]
        payload["expires_at"] = session_payload["session"]["expires_at"]
    record_gateway_activity(
        "local_connect_requested",
        entry=entry,
        status=payload["status"],
        approval_id=entry.get("approval_id"),
        fingerprint_signature=entry.get("local_trust_signature"),
    )
    return payload


def _send_local_session_message(*, session_token: str, body: dict) -> dict:
    registry = load_gateway_registry()
    session = verify_local_session_token(registry, session_token)
    entry = find_agent_entry(registry, str(session.get("agent_name") or ""))
    if not entry:
        raise LookupError("Local session agent is no longer registered.")
    annotated = annotate_runtime_health(entry, registry=registry)
    if str(annotated.get("approval_state") or "").lower() not in {"not_required", "approved"}:
        raise ValueError("Local session agent is not approved.")
    if not str(body.get("space_id") or "").strip():
        _hydrate_entry_space_from_database(registry, entry)
    space_id = str(
        body.get("space_id")
        or entry.get("active_space_id")
        or entry.get("space_id")
        or entry.get("default_space_id")
        or ""
    ).strip()
    content = str(body.get("content") or "").strip()
    if not space_id:
        raise ValueError("space_id is required.")
    if not content:
        raise ValueError("content is required.")
    client = _load_managed_agent_client(entry)
    metadata = {
        **(body.get("metadata") if isinstance(body.get("metadata"), dict) else {}),
        "gateway_local_session_id": session.get("session_id"),
        "gateway_pass_through_agent": entry.get("name"),
        "gateway_pass_through_agent_id": entry.get("agent_id"),
        "gateway_pass_through_fingerprint_signature": session.get("fingerprint_signature"),
    }
    parent_id = str(body.get("parent_id") or "").strip()
    payload = client.send_message(
        space_id,
        content,
        agent_id=str(entry.get("agent_id") or "") or None,
        channel=str(body.get("channel") or "main"),
        parent_id=parent_id or None,
        metadata=metadata,
        message_type=str(body.get("message_type") or "text"),
    )
    record_gateway_activity("local_message_sent", entry=entry, message_id=(payload.get("message") or {}).get("id"))
    return {"agent": entry.get("name"), "message": payload, "session": session}


def _local_session_inbox(
    *,
    session_token: str,
    limit: int = 20,
    channel: str = "main",
    space_id: str | None = None,
    unread_only: bool = True,
    mark_read: bool = True,
) -> dict:
    registry = load_gateway_registry()
    session = verify_local_session_token(registry, session_token)
    entry = find_agent_entry(registry, str(session.get("agent_name") or ""))
    if not entry:
        raise LookupError("Local session agent is no longer registered.")
    annotated = annotate_runtime_health(entry, registry=registry)
    if str(annotated.get("approval_state") or "").lower() not in {"not_required", "approved"}:
        raise ValueError("Local session agent is not approved.")

    selected_space = str(space_id or entry.get("space_id") or "").strip() or None
    client = _load_managed_agent_client(entry)
    data = client.list_messages(
        limit=limit,
        channel=channel,
        space_id=selected_space,
        agent_id=str(entry.get("agent_id") or "") or None,
        unread_only=unread_only,
        mark_read=mark_read,
    )
    messages = data if isinstance(data, list) else data.get("messages", [])
    local_marked_read_count = 0
    if mark_read:
        agent_name = str(entry.get("name") or "")
        pending_items = load_agent_pending_messages(agent_name)
        # The local queue powers the unread badge. Once the inbox has been
        # checked with mark_read enabled, stale local notifications should not
        # keep the Gateway UI saying there is new mail.
        remaining: list[dict] = []
        local_marked_read_count = len(pending_items) - len(remaining)
        save_agent_pending_messages(agent_name, remaining)
        registry = load_gateway_registry()
        stored = find_agent_entry(registry, agent_name) or entry
        stored["backlog_depth"] = len(remaining)
        stored["queue_depth"] = len(remaining)
        stored["current_status"] = "queued" if remaining else None
        stored["current_activity"] = (
            gateway_core._gateway_pickup_activity(str(stored.get("runtime_type") or ""), len(remaining))[:240]
            if remaining
            else None
        )
        if remaining:
            last_pending = remaining[-1]
            stored["last_received_message_id"] = last_pending.get("message_id")
            stored["last_work_received_at"] = (
                last_pending.get("queued_at") or last_pending.get("created_at") or stored.get("last_work_received_at")
            )
        else:
            stored["last_received_message_id"] = None
        save_gateway_registry(registry)
    record_gateway_activity(
        "local_inbox_polled",
        entry=entry,
        message_count=len(messages),
        mark_read=mark_read,
        local_marked_read_count=local_marked_read_count,
        session_id=session.get("session_id"),
    )
    return {
        "agent": entry.get("name"),
        "messages": messages,
        "unread_count": data.get("unread_count") if isinstance(data, dict) else None,
        "marked_read_count": (
            data.get("marked_read_count", local_marked_read_count)
            if isinstance(data, dict)
            else local_marked_read_count
        ),
        "session": session,
    }


def _normalize_runtime_type(runtime_type: str) -> str:
    try:
        return str(runtime_type_definition(runtime_type)["id"])
    except KeyError as exc:
        raise ValueError(
            "Unsupported runtime type. Use echo, exec, hermes_sentinel, sentinel_cli, claude_code_channel, or inbox."
        ) from exc


def _validate_runtime_registration(runtime_type: str, exec_cmd: str | None) -> None:
    definition = runtime_type_definition(runtime_type)
    required = set(definition.get("requires") or [])
    if "exec_command" in required and not exec_cmd:
        raise ValueError("Exec runtimes require --exec.")
    if "exec_command" not in required and exec_cmd:
        raise ValueError("This runtime does not accept --exec.")


def _normalize_timeout_seconds(timeout_seconds: int | None) -> int | None:
    if timeout_seconds is None:
        return None
    try:
        normalized = int(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("Timeout must be a whole number of seconds.") from exc
    if normalized < 1:
        raise ValueError("Timeout must be at least 1 second.")
    return normalized


def _agent_row_space_ids(registry: dict) -> set[str]:
    return {
        str(item.get("space_id") or "").strip()
        for item in registry.get("agents", [])
        if isinstance(item, dict) and str(item.get("space_id") or "").strip()
    }


def _space_list_from_response(raw: object) -> list[dict]:
    items = raw.get("spaces", raw) if isinstance(raw, dict) else raw
    return [item for item in (items or []) if isinstance(item, dict)]


def _space_name_for_id(client: AxClient, space_id: str) -> str | None:
    try:
        for item in _space_list_from_response(client.list_spaces()):
            if auth_cmd._candidate_space_id(item) == space_id:
                return str(item.get("name") or item.get("slug") or space_id)
    except Exception:
        return None
    return None


def _resolve_gateway_agent_home_space(
    *,
    client: AxClient,
    session: dict,
    registry: dict,
    explicit_space_id: str | None = None,
) -> str:
    explicit = str(explicit_space_id or "").strip()
    if explicit:
        return explicit
    session_space = str(session.get("space_id") or "").strip()
    if session_space:
        return session_space

    row_spaces = _agent_row_space_ids(registry)
    if len(row_spaces) == 1:
        return next(iter(row_spaces))

    try:
        selected = auth_cmd._select_login_space(_space_list_from_response(client.list_spaces()))
        selected_id = auth_cmd._candidate_space_id(selected or {})
        if selected_id:
            return selected_id
    except Exception:
        pass

    if len(row_spaces) > 1:
        raise ValueError(
            "Multiple agent spaces are present. Pick a home space once with --space-id, "
            "or move an existing agent row to the intended space."
        )
    raise ValueError(
        "No agent home space could be inferred. Pick a home space once with --space-id; "
        "after the agent row exists, Gateway will use the row's space_id."
    )


def _agent_space_id_from_backend_record(agent: dict) -> str | None:
    """Return the backend-owned current/default space for an agent row.

    Prefer the current row placement (`space_id`) over defaults so a Gateway
    local client that omits --space-id follows the database after a user moves
    the agent between spaces.
    """
    raw_current = agent.get("current_space")
    current_space_id = ""
    if isinstance(raw_current, dict):
        current_space_id = str(raw_current.get("space_id") or raw_current.get("id") or "").strip()
    elif raw_current:
        current_space_id = str(raw_current).strip()
    return (
        current_space_id
        or str(agent.get("active_space_id") or "").strip()
        or str(agent.get("space_id") or "").strip()
        or str(agent.get("default_space_id") or "").strip()
        or None
    )


def _agent_space_name_from_backend_record(agent: dict, space_id: str | None) -> str | None:
    raw_current = agent.get("current_space")
    if isinstance(raw_current, dict):
        current_id = str(raw_current.get("space_id") or raw_current.get("id") or "").strip()
        if not space_id or current_id == space_id:
            return str(raw_current.get("name") or raw_current.get("space_name") or "").strip() or None
    return (
        str(agent.get("space_name") or agent.get("active_space_name") or agent.get("default_space_name") or "").strip()
        or None
    )


def _backend_agent_record(client: AxClient, name: str) -> dict | None:
    try:
        agents_data = client.list_agents()
    except Exception:
        return None
    agents = agents_data if isinstance(agents_data, list) else agents_data.get("agents", [])
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        if str(agent.get("name") or "") != name:
            continue
        return agent
    return None


def _existing_agent_home_space(client: AxClient, name: str) -> str | None:
    agent = _backend_agent_record(client, name)
    if not agent:
        return None
    return _agent_space_id_from_backend_record(agent)


def _hydrate_entry_space_from_database(registry: dict, entry: dict) -> str | None:
    """Refresh an existing registry entry's space from the backend agent row."""
    name = str(entry.get("name") or "").strip()
    if not name:
        return None
    try:
        agent = _backend_agent_record(_load_gateway_user_client(), name)
    except Exception:
        return None
    if not agent:
        return None
    space_id = _agent_space_id_from_backend_record(agent)
    if not space_id:
        return None
    space_name = _agent_space_name_from_backend_record(agent, space_id)
    apply_entry_current_space(entry, space_id, space_name=space_name, make_default=False)
    if str(agent.get("default_space_id") or "").strip():
        entry["default_space_id"] = str(agent.get("default_space_id") or "").strip()
    if str(agent.get("id") or agent.get("agent_id") or "").strip():
        entry["agent_id"] = str(agent.get("id") or agent.get("agent_id") or "").strip()
    save_gateway_registry(registry)
    return space_id


def _register_managed_agent(
    *,
    name: str,
    runtime_type: str | None = None,
    template_id: str | None = None,
    exec_cmd: str | None = None,
    workdir: str | None = None,
    ollama_model: str | None = None,
    space_id: str | None = None,
    audience: str = "both",
    description: str | None = None,
    model: str | None = None,
    timeout_seconds: int | None = None,
    start: bool = True,
) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Managed agent name is required.")
    template = None
    explicit_workdir = str(workdir or "").strip() or None
    if template_id:
        try:
            template = agent_template_definition(template_id)
        except KeyError as exc:
            raise ValueError(f"Unknown template: {template_id}") from exc
        if not bool(template.get("launchable", True)):
            raise ValueError(f"Template {template['label']} is not launchable yet.")
        defaults = template.get("defaults") or {}
        runtime_type = runtime_type or str(defaults.get("runtime_type") or "")
        exec_cmd = exec_cmd or (str(defaults.get("exec_command") or "").strip() or None)
        workdir = workdir or (str(defaults.get("workdir") or "").strip() or None)
        if "start" in defaults:
            start = bool(defaults.get("start"))
    runtime_type = runtime_type or "echo"
    runtime_type = _normalize_runtime_type(runtime_type)
    normalized_ollama_model = str(ollama_model or "").strip() or None
    template_effective_id = str(template.get("id") if template else "").strip().lower()
    if normalized_ollama_model and template_effective_id != "ollama":
        raise ValueError("--ollama-model is only supported with the Ollama template.")
    if template_effective_id == "ollama" and not normalized_ollama_model:
        normalized_ollama_model = str(ollama_setup_status().get("recommended_model") or "").strip() or None
    if template_effective_id in {"hermes", "sentinel_cli", "claude_code_channel"} and not explicit_workdir:
        raise ValueError(
            f"Template {template['label']} requires --workdir so Gateway can bind the agent to its runtime folder."
        )
    _validate_runtime_registration(runtime_type, exec_cmd)
    timeout_effective = _normalize_timeout_seconds(timeout_seconds)

    client = _load_gateway_user_client()
    session = _load_gateway_session_or_exit()
    registry = load_gateway_registry()
    existing_home_space = _existing_agent_home_space(client, name) if not space_id else None
    selected_space = _resolve_gateway_agent_home_space(
        client=client,
        session=session,
        registry=registry,
        explicit_space_id=space_id or existing_home_space,
    )
    existing = _find_agent_in_space(client, name, selected_space)
    if existing:
        agent = existing
        if description or model:
            client.update_agent(name, **{k: v for k, v in {"description": description, "model": model}.items() if v})
    else:
        agent = _create_agent_in_space(
            client,
            name=name,
            space_id=selected_space,
            description=description,
            model=model,
        )
    _polish_metadata(client, name=name, bio=None, specialization=None, system_prompt=None)

    agent_id = str(agent.get("id") or agent.get("agent_id") or "")
    token, pat_source = _mint_agent_pat(
        client,
        agent_id=agent_id,
        agent_name=name,
        audience=audience,
        expires_in_days=90,
        pat_name=f"gateway-{name}",
        space_id=selected_space,
    )
    token_file = _save_agent_token(name, token)

    requires_approval = bool((template or {}).get("requires_approval", False))
    entry_payload = {
        "name": name,
        "template_id": template.get("id") if template else None,
        "template_label": template.get("label") if template else None,
        "agent_id": agent_id,
        "space_id": selected_space,
        "base_url": session["base_url"],
        "runtime_type": runtime_type,
        "exec_command": exec_cmd,
        "workdir": workdir,
        "ollama_model": normalized_ollama_model,
        "timeout_seconds": timeout_effective,
        "token_file": str(token_file),
        "desired_state": "running" if start else "stopped",
        "effective_state": "stopped",
        "transport": "gateway",
        "credential_source": "gateway",
        "last_error": None,
        "backlog_depth": 0,
        "processed_count": 0,
        "dropped_count": 0,
        "pat_source": pat_source,
        "requires_approval": requires_approval,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    if requires_approval:
        entry_payload["install_id"] = str(uuid.uuid4())
    entry = upsert_agent_entry(registry, entry_payload)
    if not requires_approval:
        ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)
    ensure_gateway_identity_binding(registry, entry, session=session, created_via="cli")
    entry.update(evaluate_runtime_attestation(registry, entry))
    _write_agent_workspace_config(entry)
    hermes_status = hermes_setup_status(entry)
    if not hermes_status.get("ready", True):
        entry["effective_state"] = "error"
        entry["last_error"] = str(
            hermes_status.get("detail") or hermes_status.get("summary") or "Hermes setup is incomplete."
        )
        entry["current_activity"] = str(hermes_status.get("summary") or "Hermes setup is incomplete.")
    elif hermes_status.get("resolved_path"):
        entry["hermes_repo_path"] = str(hermes_status["resolved_path"])
    save_gateway_registry(registry)
    record_gateway_activity(
        "managed_agent_added",
        entry=entry,
        space_id=selected_space,
        token_file=str(token_file),
    )
    return annotate_runtime_health(entry, registry=registry)


def _agent_workspace_context_text(entry: dict, *, workdir: str) -> str:
    name = str(entry.get("name") or "agent").strip()
    template = str(entry.get("template_id") or entry.get("runtime_type") or "gateway").strip()
    runtime = str(entry.get("runtime_type") or "gateway").strip()
    return f"""# aX Agent Context

You are `@{name}`, an agent connected to the aX multi-user, multi-agent network through the local Gateway.

Identity and runtime:

- Agent name: `@{name}`
- Agent type: `{template}`
- Runtime: `{runtime}`
- Runtime folder: `{workdir}`
- Gateway URL: `http://127.0.0.1:8765`

How to use aX from this folder:

```bash
ax gateway local connect --workdir .
ax gateway local inbox --workdir .
ax gateway local send --workdir . "@agent_name message"
```

Guidelines:

- Use the Gateway CLI from this folder for aX messages, inbox checks, tasks, and context.
- Do not ask the user for a PAT and do not store user tokens in this folder.
- If Gateway says approval is required, tell the user to open `http://127.0.0.1:8765` and approve the pending binding.
- Treat aX as your shared agent network: messages may come from users, service accounts, or other agents.
- Keep replies concise unless the task needs detail, and surface useful progress through the runtime when possible.
- Keep self-description updates, preferences, avatar metadata, and capability notes aligned with Gateway-backed agent settings as those commands become available.
"""


def _agent_workspace_readme_text(entry: dict, *, workdir: str) -> str:
    name = str(entry.get("name") or "agent").strip()
    template = str(entry.get("template_id") or entry.get("runtime_type") or "gateway").strip()
    return f"""# aX Gateway Agent

This folder is registered with the local aX Gateway as `@{name}`.

- Agent type: `{template}`
- Runtime folder: `{workdir}`
- Gateway URL: `http://127.0.0.1:8765`

Read `.ax/AGENT_CONTEXT.md` first. It explains your aX identity and the Gateway CLI path.

Use the Gateway CLI from this folder when you need platform context:

```bash
ax gateway local connect --workdir .
ax gateway local inbox --workdir .
ax gateway local send --workdir . "@agent_name message"
```

Do not add a user PAT here. Gateway owns credential minting and the local
fingerprint binding for this agent. Keep self-description updates, preferences,
avatar metadata, and capability notes in Gateway-backed agent settings as those
commands become available.
"""


def _write_agent_context_hint(path: Path, *, agent_name: str, context_path: Path) -> None:
    if path.exists():
        return
    path.write_text(
        "\n".join(
            [
                f"# {agent_name} on aX",
                "",
                "This workspace is connected to aX through the local Gateway.",
                f"Read `{context_path}` before using aX tools.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_agent_workspace_config(entry: dict) -> None:
    template = str(entry.get("template_id") or "").strip().lower()
    runtime = str(entry.get("runtime_type") or "").strip().lower()
    if template not in {"hermes", "sentinel_cli", "claude_code_channel"} and runtime not in {
        "hermes_sentinel",
        "sentinel_cli",
        "claude_code_channel",
    }:
        return
    workdir = str(entry.get("workdir") or "").strip()
    name = str(entry.get("name") or "").strip()
    if not workdir or not name:
        return
    root = Path(workdir).expanduser().resolve()
    config_dir = root / ".ax"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(
        _gateway_local_config_text(agent_name=name, gateway_url="http://127.0.0.1:8765", workdir=str(root))
    )
    (config_dir / "config.toml").chmod(0o600)
    (config_dir / "README.md").write_text(_agent_workspace_readme_text(entry, workdir=str(root)))
    context_path = config_dir / "AGENT_CONTEXT.md"
    context_path.write_text(_agent_workspace_context_text(entry, workdir=str(root)), encoding="utf-8")
    _write_agent_context_hint(root / "AGENTS.md", agent_name=name, context_path=Path(".ax") / "AGENT_CONTEXT.md")
    if template == "claude_code_channel" or runtime == "claude_code_channel":
        _write_agent_context_hint(root / "CLAUDE.md", agent_name=name, context_path=Path(".ax") / "AGENT_CONTEXT.md")


def _update_managed_agent(
    *,
    name: str,
    template_id: str | None = None,
    runtime_type: str | None = None,
    exec_cmd: str | object = _UNSET,
    workdir: str | object = _UNSET,
    ollama_model: str | object = _UNSET,
    description: str | None = None,
    model: str | None = None,
    timeout_seconds: int | object = _UNSET,
    desired_state: str | None = None,
) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Managed agent name is required.")

    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")

    template = None
    if template_id:
        try:
            template = agent_template_definition(template_id)
        except KeyError as exc:
            raise ValueError(f"Unknown template: {template_id}") from exc
        if not bool(template.get("launchable", True)):
            raise ValueError(f"Template {template['label']} is not launchable yet.")

    runtime_candidate = (
        runtime_type or (template.get("defaults") or {}).get("runtime_type") if template else runtime_type
    )
    runtime_effective = str(runtime_candidate or entry.get("runtime_type") or "echo")
    runtime_effective = _normalize_runtime_type(runtime_effective)
    template_effective_id = str(template.get("id") if template else entry.get("template_id") or "").strip().lower()

    if template:
        defaults = template.get("defaults") or {}
        exec_effective = (
            str(exec_cmd).strip() or None
            if exec_cmd is not _UNSET
            else (str(defaults.get("exec_command") or "").strip() or None)
        )
        workdir_effective = (
            str(workdir).strip() or None
            if workdir is not _UNSET
            else (str(defaults.get("workdir") or "").strip() or None)
        )
    else:
        exec_effective = (
            str(entry.get("exec_command") or "").strip() or None
            if exec_cmd is _UNSET
            else (str(exec_cmd).strip() or None)
        )
        workdir_effective = (
            str(entry.get("workdir") or "").strip() or None if workdir is _UNSET else (str(workdir).strip() or None)
        )

    if ollama_model is _UNSET:
        ollama_model_effective = str(entry.get("ollama_model") or "").strip() or None
    else:
        ollama_model_effective = str(ollama_model).strip() or None
    if ollama_model_effective and template_effective_id != "ollama":
        raise ValueError("--ollama-model is only supported with the Ollama template.")
    if template_effective_id == "ollama" and ollama_model is _UNSET and not ollama_model_effective:
        ollama_model_effective = str(ollama_setup_status().get("recommended_model") or "").strip() or None

    _validate_runtime_registration(runtime_effective, exec_effective)

    if desired_state is not None:
        normalized_desired = desired_state.lower().strip()
        if normalized_desired not in {"running", "stopped"}:
            raise ValueError("Desired state must be running or stopped.")
        entry["desired_state"] = normalized_desired
    if timeout_seconds is not _UNSET:
        entry["timeout_seconds"] = _normalize_timeout_seconds(timeout_seconds)  # type: ignore[arg-type]

    session = _load_gateway_session_or_exit()
    if description or model:
        client = _load_gateway_user_client()
        client.update_agent(name, **{k: v for k, v in {"description": description, "model": model}.items() if v})

    if template:
        entry["template_id"] = template.get("id")
        entry["template_label"] = template.get("label")
    entry["runtime_type"] = runtime_effective
    entry["exec_command"] = exec_effective
    entry["workdir"] = workdir_effective
    if template_effective_id == "ollama":
        entry["ollama_model"] = ollama_model_effective
    else:
        entry.pop("ollama_model", None)
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    entry.setdefault("transport", "gateway")
    entry.setdefault("credential_source", "gateway")

    if template and template.get("id") != "hermes":
        entry.pop("hermes_repo_path", None)

    ensure_gateway_identity_binding(registry, entry, session=session)
    ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True, replace_existing=True)
    entry.update(evaluate_runtime_attestation(registry, entry))
    _write_agent_workspace_config(entry)
    hermes_status = hermes_setup_status(entry)
    if not hermes_status.get("ready", True):
        entry["effective_state"] = "error"
        entry["last_error"] = str(
            hermes_status.get("detail") or hermes_status.get("summary") or "Hermes setup is incomplete."
        )
        entry["current_activity"] = str(hermes_status.get("summary") or "Hermes setup is incomplete.")
    elif hermes_status.get("resolved_path"):
        entry["hermes_repo_path"] = str(hermes_status["resolved_path"])

    save_gateway_registry(registry)
    record_gateway_activity(
        "managed_agent_updated",
        entry=entry,
        template_id=entry.get("template_id"),
        runtime_type=runtime_effective,
        workdir=workdir_effective,
        exec_command=exec_effective,
        desired_state=entry.get("desired_state"),
        timeout_seconds=entry.get("timeout_seconds"),
    )
    return annotate_runtime_health(entry, registry=registry)


def _set_managed_agent_desired_state(name: str, desired_state: str) -> dict:
    desired_state = desired_state.lower().strip()
    if desired_state not in {"running", "stopped"}:
        raise ValueError("Desired state must be running or stopped.")
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    entry["desired_state"] = desired_state
    save_gateway_registry(registry)
    event = "managed_agent_desired_running" if desired_state == "running" else "managed_agent_desired_stopped"
    record_gateway_activity(event, entry=entry)
    return annotate_runtime_health(entry, registry=registry)


def _remove_managed_agent(name: str) -> dict:
    registry = load_gateway_registry()
    entry = remove_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    save_gateway_registry(registry)
    archive_stale_gateway_approvals()
    token_file_value = str(entry.get("token_file") or "").strip()
    token_file = Path(token_file_value) if token_file_value else None
    if token_file and token_file.is_file():
        token_file.unlink()
    record_gateway_activity("managed_agent_removed", entry=entry)
    return entry


def _reject_managed_agent_approval(name: str) -> dict:
    detail = _agent_detail_payload(name, activity_limit=1)
    if detail is None:
        raise LookupError(f"Managed agent not found: {name}")
    agent = detail.get("agent") or {}
    approval_id = str(agent.get("approval_id") or "").strip()
    if not approval_id:
        raise ValueError(f"@{name} does not have a pending Gateway approval.")
    approval = get_gateway_approval(approval_id)
    rejected = deny_gateway_approval(approval_id)
    removed = None
    if (
        str(approval.get("status") or "").lower() == "pending"
        and str(approval.get("approval_kind") or "") == "new_binding"
    ):
        try:
            removed = _remove_managed_agent(name)
        except LookupError:
            removed = None
    return {
        "approval": rejected,
        "removed_agent": removed,
        "removed": removed is not None,
    }


def _identity_space_send_guard(entry: dict, *, explicit_space_id: str | None = None) -> dict:
    registry = load_gateway_registry()
    stored = find_agent_entry(registry, str(entry.get("name") or "")) or entry
    ensure_gateway_identity_binding(registry, stored, session=load_gateway_session())
    snapshot = annotate_runtime_health(stored, registry=registry, explicit_space_id=explicit_space_id)
    save_gateway_registry(registry)
    if str(snapshot.get("confidence") or "").upper() == "BLOCKED":
        reason = str(snapshot.get("confidence_reason") or "blocked")
        detail = str(snapshot.get("confidence_detail") or "Gateway blocked this action.")
        raise ValueError(f"{detail} ({reason})")
    return snapshot


def _sync_passive_queue_after_manual_send(
    *,
    entry: dict,
    handled_message_id: str | None,
    reply_message_id: str | None,
    reply_preview: str | None,
) -> None:
    runtime_type = str(entry.get("runtime_type") or "").lower()
    if runtime_type not in {"inbox", "passive", "monitor"}:
        return

    pending_items = gateway_core.remove_agent_pending_message(str(entry.get("name") or ""), handled_message_id)
    registry = load_gateway_registry()
    stored = find_agent_entry(registry, str(entry.get("name") or "")) or entry
    backlog_depth = len(pending_items)
    last_pending = pending_items[-1] if pending_items else {}

    if handled_message_id:
        stored["processed_count"] = int(stored.get("processed_count") or 0) + 1
        stored["last_work_completed_at"] = datetime.now(timezone.utc).isoformat()

    stored["backlog_depth"] = backlog_depth
    stored["current_status"] = "queued" if backlog_depth > 0 else None
    stored["current_activity"] = (
        gateway_core._gateway_pickup_activity(runtime_type, backlog_depth)[:240] if backlog_depth > 0 else None
    )
    stored["last_reply_message_id"] = reply_message_id or stored.get("last_reply_message_id")
    stored["last_reply_preview"] = reply_preview or stored.get("last_reply_preview")
    if last_pending:
        stored["last_received_message_id"] = last_pending.get("message_id")
        stored["last_work_received_at"] = (
            last_pending.get("queued_at") or last_pending.get("created_at") or stored.get("last_work_received_at")
        )
    elif handled_message_id:
        stored["last_received_message_id"] = None
        stored["last_work_received_at"] = None

    save_gateway_registry(registry)
    if handled_message_id:
        record_gateway_activity(
            "manual_queue_acknowledged",
            entry=stored,
            message_id=handled_message_id,
            reply_message_id=reply_message_id,
            backlog_depth=backlog_depth,
        )


def _send_from_managed_agent(
    *,
    name: str,
    content: str,
    to: str | None = None,
    parent_id: str | None = None,
    space_id: str | None = None,
    sent_via: str = "gateway_cli",
    metadata_extra: dict[str, object] | None = None,
) -> dict:
    if not content.strip():
        raise ValueError("Message content is required.")
    entry = _load_managed_agent_or_exit(name)
    if str(entry.get("desired_state") or "").strip().lower() == "stopped":
        raise ValueError(f"@{name} is stopped. Start it before it can send.")
    snapshot = _identity_space_send_guard(entry, explicit_space_id=space_id)
    client = _load_managed_agent_client(entry)
    selected_space_id = str(space_id or snapshot.get("active_space_id") or entry.get("space_id") or "")
    if not selected_space_id:
        raise ValueError(f"Managed agent is missing a space id: @{name}")

    message_content = content.strip()
    mention = str(to or "").strip().lstrip("@")
    if mention:
        prefix = f"@{mention}"
        if not message_content.startswith(prefix):
            message_content = f"{prefix} {message_content}".strip()

    metadata = {
        "control_plane": "gateway",
        "gateway": {
            "managed": True,
            "agent_name": entry.get("name"),
            "agent_id": entry.get("agent_id"),
            "runtime_type": entry.get("runtime_type"),
            "transport": entry.get("transport", "gateway"),
            "credential_source": entry.get("credential_source", "gateway"),
            "sent_via": sent_via,
        },
    }
    if metadata_extra:
        gateway_meta = metadata["gateway"]
        if isinstance(gateway_meta, dict):
            gateway_meta.update(metadata_extra)
    result = client.send_message(
        selected_space_id,
        message_content,
        agent_id=str(entry.get("agent_id") or "") or None,
        parent_id=parent_id or None,
        metadata=metadata,
    )
    payload = result.get("message", result) if isinstance(result, dict) else result
    if isinstance(payload, dict):
        record_gateway_activity(
            "manual_message_sent",
            entry=entry,
            message_id=payload.get("id"),
            reply_preview=message_content[:120] or None,
        )
        _sync_passive_queue_after_manual_send(
            entry=entry,
            handled_message_id=parent_id,
            reply_message_id=str(payload.get("id") or "") or None,
            reply_preview=message_content[:120] or None,
        )
    return {"agent": entry.get("name"), "message": payload, "content": message_content}


def _gateway_test_sender_name(space_id: str) -> str:
    normalized = "".join(ch for ch in str(space_id or "") if ch.isalnum()).lower()
    suffix = normalized[:8] or "default"
    return f"switchboard-{suffix}"


def _space_cache_with(space_rows: object, space_id: str, *, name: str | None = None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    if isinstance(space_rows, list):
        for item in space_rows:
            if isinstance(item, dict):
                item_space_id = str(item.get("space_id") or item.get("id") or "").strip()
                item_name = str(item.get("name") or item.get("space_name") or item_space_id)
                is_default = bool(item.get("is_default", False))
            else:
                item_space_id = str(item or "").strip()
                item_name = item_space_id
                is_default = False
            if not item_space_id or item_space_id in seen:
                continue
            seen.add(item_space_id)
            rows.append({"space_id": item_space_id, "name": item_name, "is_default": is_default})
    if space_id and space_id not in seen:
        rows.append({"space_id": space_id, "name": name or space_id, "is_default": not rows})
    return rows


def _ensure_gateway_test_sender(target_entry: dict) -> dict:
    target_space = str(target_entry.get("space_id") or "").strip()
    if not target_space:
        raise ValueError("Managed agent is missing a space id for Gateway test delivery.")
    sender_name = _gateway_test_sender_name(target_space)
    registry = load_gateway_registry()
    existing = find_agent_entry(registry, sender_name)
    if existing:
        return annotate_runtime_health(existing, registry=registry)
    return _register_managed_agent(
        name=sender_name,
        template_id="inbox",
        space_id=target_space,
        description="Gateway-managed passive sender for agent-authored tests.",
        start=True,
    )


def _status_payload(*, activity_limit: int = 10) -> dict:
    daemon = daemon_status()
    ui = ui_status()
    session = load_gateway_session()
    registry = daemon["registry"]
    agents = [
        _with_registry_refs(registry, annotate_runtime_health(agent, registry=registry))
        for agent in registry.get("agents", [])
    ]
    approvals = list_gateway_approvals()
    pending_approvals = [item for item in approvals if str(item.get("status") or "") == "pending"]
    live_agents = [a for a in agents if str(a.get("mode") or "") == "LIVE"]
    on_demand_agents = [a for a in agents if str(a.get("mode") or "") == "ON-DEMAND"]
    inbox_agents = [a for a in agents if str(a.get("mode") or "") == "INBOX"]
    connected_agents = [a for a in agents if bool(a.get("connected"))]
    stale_agents = [a for a in agents if str(a.get("presence") or "") == "STALE"]
    offline_agents = [a for a in agents if str(a.get("presence") or "") == "OFFLINE"]
    errored_agents = [a for a in agents if str(a.get("presence") or "") == "ERROR"]
    low_confidence_agents = [a for a in agents if str(a.get("confidence") or "") in {"LOW", "BLOCKED"}]
    blocked_agents = [a for a in agents if str(a.get("confidence") or "") == "BLOCKED"]
    gateway = dict(registry.get("gateway", {}))
    if not daemon["running"]:
        gateway["effective_state"] = "stopped"
        gateway["pid"] = None
    # Active space fallback. The gateway session sometimes ships without a
    # space_id (older sessions, sessions minted before we resolved the user's
    # default workspace). Without this, the operator overview shows Space=—
    # even though every managed agent has a space_id. Pick the most-used space
    # across agents as the implicit active space for display.
    fallback_space_id: str | None = None
    if not (session and session.get("space_id")):
        space_counts: dict[str, int] = {}
        for agent in agents:
            sid = str(agent.get("space_id") or "").strip()
            if not sid:
                continue
            space_counts[sid] = space_counts.get(sid, 0) + 1
        if space_counts:
            fallback_space_id = max(space_counts.items(), key=lambda item: item[1])[0]

    payload = {
        "gateway_dir": str(gateway_dir()),
        "gateway_environment": gateway_environment(),
        "connected": bool(session),
        "base_url": session.get("base_url") if session else None,
        "space_id": (session.get("space_id") if session else None) or fallback_space_id,
        "space_name": session.get("space_name") if session else None,
        "user": session.get("username") if session else None,
        "daemon": {
            "running": daemon["running"],
            "pid": daemon["pid"],
        },
        "ui": {
            "running": ui["running"],
            "pid": ui["pid"],
            "host": ui["host"],
            "port": ui["port"],
            "url": ui["url"],
            "log_path": ui["log_path"],
        },
        "gateway": gateway,
        "agents": agents,
        "approvals": approvals,
        "recent_activity": load_recent_gateway_activity(limit=activity_limit),
        "summary": {
            "managed_agents": len(agents),
            "live_agents": len(live_agents),
            "on_demand_agents": len(on_demand_agents),
            "inbox_agents": len(inbox_agents),
            "connected_agents": len(connected_agents),
            "stale_agents": len(stale_agents),
            "offline_agents": len(offline_agents),
            "errored_agents": len(errored_agents),
            "low_confidence_agents": len(low_confidence_agents),
            "blocked_agents": len(blocked_agents),
            "pending_approvals": len(pending_approvals),
        },
    }
    alerts = _gateway_alerts(payload)
    payload["alerts"] = alerts
    payload["summary"]["alert_count"] = len(alerts)
    return payload


def _gateway_alerts(payload: dict, *, limit: int = 6) -> list[dict]:
    alerts: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def push(severity: str, title: str, detail: str, *, agent_name: str | None = None) -> None:
        key = (severity, title, agent_name or "")
        if key in seen:
            return
        seen.add(key)
        alerts.append(
            {
                "severity": severity,
                "title": title,
                "detail": detail,
                "agent_name": agent_name,
            }
        )

    if not payload.get("connected"):
        push("error", "Gateway is not logged in", "Run `ax gateway login` to bootstrap the local control plane.")
    elif not payload.get("daemon", {}).get("running"):
        push(
            "error",
            "Gateway daemon is stopped",
            "Start it with `uv run ax gateway start` or relaunch the local service.",
        )

    if not payload.get("ui", {}).get("running"):
        push(
            "warning", "Gateway UI is stopped", "Start it with `uv run ax gateway start` to launch the local dashboard."
        )

    for agent in payload.get("agents", []):
        name = str(agent.get("name") or "")
        presence = str(agent.get("presence") or "").upper()
        approval_state = str(agent.get("approval_state") or "").lower()
        attestation_state = str(agent.get("attestation_state") or "").lower()
        preview = str(agent.get("last_reply_preview") or "")
        lowered_preview = preview.lower()
        setup_error_preview = (
            preview.startswith("(stderr:")
            or " repo not found" in lowered_preview
            or lowered_preview.startswith("ollama bridge failed:")
        )
        if approval_state == "pending":
            detail = str(agent.get("confidence_detail") or "Gateway needs approval before this runtime can be trusted.")
            push("warning", f"@{name} needs Gateway approval", detail, agent_name=name)
        elif approval_state == "rejected" or attestation_state == "blocked":
            detail = str(agent.get("confidence_detail") or "Gateway blocked this runtime.")
            push("error", f"@{name} is blocked by Gateway", detail, agent_name=name)
        elif attestation_state == "drifted":
            detail = str(agent.get("confidence_detail") or "Runtime changed since approval and needs review.")
            push("warning", f"@{name} changed since approval", detail, agent_name=name)
        elif presence == "BLOCKED":
            detail = str(
                agent.get("confidence_detail")
                or "Gateway blocked this runtime until identity, space, or approval state is fixed."
            )
            push("error", f"@{name} is blocked", detail, agent_name=name)
        elif presence == "ERROR":
            if setup_error_preview:
                push("error", f"@{name} has a runtime setup error", preview[:180], agent_name=name)
            else:
                detail = str(agent.get("confidence_detail") or agent.get("last_error") or "Runtime reported an error.")
                push("error", f"@{name} hit an error", detail, agent_name=name)
        elif presence == "STALE":
            detail = f"No heartbeat for {_format_age(agent.get('last_seen_age_seconds'))}."
            push("warning", f"@{name} looks stale", detail, agent_name=name)
        elif presence == "OFFLINE" and str(agent.get("mode") or "") == "LIVE":
            detail = str(
                agent.get("confidence_detail")
                or "Expected a live runtime, but Gateway does not currently have a working path."
            )
            push("warning", f"@{name} is offline", detail, agent_name=name)
        if setup_error_preview and presence != "ERROR":
            push("error", f"@{name} has a runtime setup error", preview[:180], agent_name=name)
        if int(agent.get("backlog_depth") or 0) > 0 and presence in {"OFFLINE", "ERROR", "STALE"}:
            detail = f"{agent.get('backlog_depth')} queued item(s) may be stuck until the agent is healthy."
            push("warning", f"@{name} has queued work", detail, agent_name=name)

    for item in reversed(payload.get("recent_activity", [])):
        event = str(item.get("event") or "")
        if event == "gateway_start_blocked":
            existing = item.get("existing_pid") or item.get("existing_pids")
            push("warning", "Another Gateway instance is already running", f"Existing process: {existing}.")
        elif event in {"listener_error", "listener_timeout"}:
            agent_name = str(item.get("agent_name") or "")
            detail = str(item.get("error") or "Listener lost contact and is reconnecting.")
            push("warning", f"@{agent_name} had a listener interruption", detail, agent_name=agent_name or None)
        if len(alerts) >= limit:
            break

    return alerts[:limit]


def _runtime_types_payload() -> dict:
    return {"runtime_types": runtime_type_list(), "count": len(runtime_type_list())}


def _annotate_template_taxonomy(definition: dict) -> dict:
    enriched = dict(definition)
    descriptor = infer_asset_descriptor(
        {
            "template_id": definition.get("id"),
            "template_label": definition.get("label"),
            "runtime_type": definition.get("runtime_type"),
            "telemetry_shape": definition.get("telemetry_shape"),
            "asset_class": definition.get("asset_class"),
            "intake_model": definition.get("intake_model"),
            "worker_model": definition.get("worker_model"),
            "trigger_sources": definition.get("trigger_sources"),
            "return_paths": definition.get("return_paths"),
            "tags": definition.get("tags"),
            "capabilities": definition.get("capabilities"),
            "constraints": definition.get("constraints"),
            "addressable": definition.get("addressable"),
            "messageable": definition.get("messageable"),
            "schedulable": definition.get("schedulable"),
            "externally_triggered": definition.get("externally_triggered"),
        }
    )
    enriched.update(
        {
            "asset_class": descriptor["asset_class"],
            "intake_model": descriptor["intake_model"],
            "worker_model": descriptor.get("worker_model"),
            "trigger_sources": descriptor["trigger_sources"],
            "return_paths": descriptor["return_paths"],
            "telemetry_shape": descriptor["telemetry_shape"],
            "asset_type_label": descriptor["type_label"],
            "output_label": descriptor["output_label"],
            "asset_descriptor": descriptor,
        }
    )
    return enriched


# ── Runtime install (GATEWAY-RUNTIME-AUTOSETUP-001) ────────────────────────
#
# Hardcoded allowlist of runtimes the gateway can install on the operator's
# behalf. Per the spec security section: clone URL is NEVER taken from the
# request body — it comes from this dict by template_id. Adding a new runtime
# requires a code-reviewable PR. Targets must resolve under Path.home() (with
# realpath() so symlinks can't escape the home tree). pip install runs inside
# a venv at <target>/.venv, never against the system Python.

_RUNTIME_INSTALL_RECIPES: dict[str, dict] = {
    "hermes": {
        "clone_url": "https://github.com/NousResearch/hermes-agent",
        "target_relative": "hermes-agent",
        "verify_template_id": "hermes",
        "install_steps": ("clone", "venv", "pip_install", "verify"),
    },
}


def _resolve_install_target(template_id: str, override: str | None = None) -> Path:
    recipe = _RUNTIME_INSTALL_RECIPES.get(template_id)
    if recipe is None:
        raise ValueError(f"unknown runtime template: {template_id!r}")
    if override:
        candidate = Path(override).expanduser().resolve()
    else:
        candidate = (Path.home() / recipe["target_relative"]).resolve()
    home_resolved = Path.home().resolve()
    try:
        candidate.relative_to(home_resolved)
    except ValueError as exc:
        raise ValueError(f"refusing to install outside home tree: {candidate} (home={home_resolved})") from exc
    return candidate


def _proc_error_msg(exc: subprocess.CalledProcessError) -> str:
    """Best-effort error string from a subprocess failure.

    `python -m venv` writes its "ensurepip not available, apt install python3-venv"
    hint to stdout, not stderr. Reading only `exc.stderr` swallowed the actionable
    error in the AUTOSETUP demo dry-run. Use both streams; fall back to exit code.
    """
    stderr = (exc.stderr or "").strip()
    stdout = (exc.stdout or "").strip()
    parts: list[str] = []
    if stderr:
        parts.append(stderr)
    if stdout and stdout != stderr:
        parts.append(stdout)
    if not parts:
        parts.append(f"exit {exc.returncode}")
    return " | ".join(parts)[:500]


def _venv_module_unavailable_reason() -> str | None:
    """Return an actionable error string if stdlib venv can't create environments.

    On Debian/Ubuntu, `python3 -m venv` fails when the `python3-venv` package
    is missing — but the failure mode is "exits 1, prints hint to stdout" which
    is easy to miss. Probe `ensurepip` directly so we can fail fast with a clean
    message before running git clone.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import ensurepip"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"could not probe Python venv module: {exc}"
    if result.returncode != 0:
        return (
            "stdlib venv unavailable (ensurepip missing). "
            "On Debian/Ubuntu: `apt install python3.12-venv` (or matching python3-venv for your interpreter)."
        )
    return None


def _install_runtime_payload(
    template_id,
    *,
    target_override=None,
    operator_session=None,
):
    """Run the install recipe for ``template_id`` and return a structured result.

    Per AUTOSETUP-001 §"Security model":
    - Operator-only auth: caller MUST pass an ``operator_session`` (truthy).
      The HTTP route checks via ``load_gateway_session()`` before calling.
    - Hardcoded allowlist: ``template_id`` must be in ``_RUNTIME_INSTALL_RECIPES``.
    - User-writable target only: enforced via ``_resolve_install_target``
      (uses ``realpath`` to close the symlink trap).
    - No system Python: pip runs inside ``<target>/.venv``.
    - Cleanup on failure: any partial directory we created is removed.

    Returns a dict of shape ``{ready, summary, target, steps}`` where ``steps``
    is a chronological list of ``{step, status, detail}`` records (synchronous
    today; SSE streaming variant is a follow-up).
    """
    if not operator_session:
        raise PermissionError("install requires an active gateway operator session")
    template_id = str(template_id or "").strip().lower()
    recipe = _RUNTIME_INSTALL_RECIPES.get(template_id)
    if recipe is None:
        raise ValueError(f"unknown runtime template: {template_id!r}")

    target = _resolve_install_target(template_id, override=target_override)
    steps: list[dict[str, str]] = []
    we_created_target = False

    def _log(step: str, status: str, detail: str = "") -> None:
        steps.append({"step": step, "status": status, "detail": detail})

    def _cleanup() -> None:
        if we_created_target and target.exists():
            try:
                import shutil

                shutil.rmtree(target)
                _log("cleanup", "ok", f"removed partial install at {target}")
            except Exception as exc:  # noqa: BLE001
                _log("cleanup", "warn", f"could not remove {target}: {exc}")

    # Step: clone
    if "clone" in recipe["install_steps"]:
        clone_url = recipe["clone_url"]
        if target.exists():
            _log("clone", "skipped", f"target already exists at {target}")
        else:
            _log("clone", "running", f"cloning {clone_url} → {target}")
            we_created_target = True
            try:
                subprocess.run(
                    ["git", "clone", "--depth", "1", clone_url, str(target)],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                _log("clone", "ok", f"cloned to {target}")
            except subprocess.CalledProcessError as exc:
                _cleanup()
                _log("clone", "error", f"git clone failed: {_proc_error_msg(exc)}")
                return {"ready": False, "summary": "clone failed", "target": str(target), "steps": steps}
            except subprocess.TimeoutExpired:
                _cleanup()
                _log("clone", "error", "git clone timed out after 600s")
                return {"ready": False, "summary": "clone timed out", "target": str(target), "steps": steps}

    # Step: venv
    venv_dir = target / ".venv"
    if "venv" in recipe["install_steps"]:
        if venv_dir.exists():
            _log("venv", "skipped", f"venv already at {venv_dir}")
        else:
            preflight = _venv_module_unavailable_reason()
            if preflight:
                _cleanup()
                _log("venv", "error", preflight)
                return {"ready": False, "summary": "venv prerequisite missing", "target": str(target), "steps": steps}
            _log("venv", "running", f"creating venv at {venv_dir}")
            try:
                subprocess.run(
                    [sys.executable, "-m", "venv", str(venv_dir)],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                _log("venv", "ok", str(venv_dir))
            except subprocess.CalledProcessError as exc:
                _cleanup()
                _log("venv", "error", f"venv create failed: {_proc_error_msg(exc)}")
                return {"ready": False, "summary": "venv create failed", "target": str(target), "steps": steps}

    # Step: pip install
    if "pip_install" in recipe["install_steps"]:
        venv_pip = venv_dir / "bin" / "pip"
        if not venv_pip.exists():
            _log("pip_install", "skipped", f"no pip at {venv_pip}")
        elif not (target / "pyproject.toml").exists() and not (target / "setup.py").exists():
            _log("pip_install", "skipped", "no pyproject.toml or setup.py at target")
        else:
            _log("pip_install", "running", f"installing {target} into venv")
            try:
                subprocess.run(
                    [str(venv_pip), "install", "-e", str(target)],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                _log("pip_install", "ok", "")
            except subprocess.CalledProcessError as exc:
                # Don't cleanup — the clone is valuable even if pip failed
                _log("pip_install", "warn", f"pip install -e failed (non-fatal): {_proc_error_msg(exc)}")

    # Step: verify (re-run setup_status check)
    if "verify" in recipe["install_steps"]:
        verify_template = recipe.get("verify_template_id", template_id)
        try:
            from ..gateway import hermes_setup_status

            status = hermes_setup_status({"template_id": verify_template})
            ready = bool(status.get("ready"))
            _log("verify", "ok" if ready else "error", str(status.get("summary") or ""))
        except Exception as exc:  # noqa: BLE001
            _log("verify", "error", f"verify failed: {exc}")
            return {"ready": False, "summary": "verify failed", "target": str(target), "steps": steps}

    return {
        "ready": True,
        "summary": f"{template_id} installed at {target}",
        "target": str(target),
        "steps": steps,
    }


def _agent_templates_payload() -> dict:
    templates = [_annotate_template_taxonomy(item) for item in agent_template_list()]
    ollama_status = ollama_setup_status()
    for item in templates:
        template_id = str(item.get("id") or "").strip().lower()
        if template_id == "ollama":
            defaults = dict(item.get("defaults") or {})
            recommended_model = str(ollama_status.get("recommended_model") or "").strip() or None
            if recommended_model and not str(defaults.get("ollama_model") or "").strip():
                defaults["ollama_model"] = recommended_model
            item["defaults"] = defaults
            item["ollama_server_reachable"] = bool(ollama_status.get("server_reachable"))
            item["ollama_available_models"] = list(ollama_status.get("available_models") or [])
            item["ollama_local_models"] = list(ollama_status.get("local_models") or [])
            item["ollama_recommended_model"] = recommended_model
            item["ollama_summary"] = str(ollama_status.get("summary") or "")
        elif template_id == "hermes":
            hermes_status = hermes_setup_status({"template_id": "hermes"})
            item["hermes_ready"] = bool(hermes_status.get("ready"))
            item["hermes_resolved_path"] = hermes_status.get("resolved_path")
            item["hermes_expected_path"] = hermes_status.get("expected_path")
            item["hermes_summary"] = str(hermes_status.get("summary") or "")
            item["hermes_detail"] = str(hermes_status.get("detail") or hermes_status.get("summary") or "")
            # We don't ship a canonical clone URL — operators may use a private
            # fork. Surface the env var the gateway honors instead.
            item["hermes_fix_command"] = "export HERMES_REPO_PATH=/path/to/your/hermes-agent"
    return {"templates": templates, "count": len(templates)}


def _agent_detail_payload(name: str, *, activity_limit: int = 12) -> dict | None:
    payload = _status_payload(activity_limit=activity_limit)
    entry = next((agent for agent in payload["agents"] if str(agent.get("name") or "").lower() == name.lower()), None)
    if not entry:
        return None
    activity = load_recent_gateway_activity(limit=activity_limit, agent_name=name)
    return {
        "gateway": {
            "connected": payload["connected"],
            "base_url": payload["base_url"],
            "space_id": payload["space_id"],
            "daemon": payload["daemon"],
        },
        "agent": entry,
        "recent_activity": activity,
    }


def _approval_rows_payload(*, status: str | None = None, include_archived: bool = False) -> dict:
    approvals = list_gateway_approvals(status=status, include_archived=include_archived)
    return {
        "approvals": approvals,
        "count": len(approvals),
        "pending": len([item for item in approvals if str(item.get("status") or "") == "pending"]),
    }


def _approval_detail_payload(approval_id: str) -> dict:
    approval = get_gateway_approval(approval_id)
    return {"approval": approval}


def _recommended_test_message(entry: dict) -> str:
    template_id = str(entry.get("template_id") or "").strip()
    if template_id:
        try:
            template = agent_template_definition(template_id)
            message = str(template.get("recommended_test_message") or "").strip()
            if message:
                return message
        except KeyError:
            pass
    runtime_type = str(entry.get("runtime_type") or "").lower()
    if runtime_type == "echo":
        return "gateway test ping"
    if runtime_type == "inbox":
        return "Queue this test job, mark it received, and do not reply inline."
    return "Reply with exactly: Gateway test OK."


def _send_gateway_test_to_managed_agent(
    name: str,
    *,
    content: str | None = None,
    author: str = "agent",
    sender_agent: str | None = None,
    allow_self_fallback: bool = True,
) -> dict:
    entry = _load_managed_agent_or_exit(name)
    if str(entry.get("desired_state") or "").strip().lower() == "stopped":
        raise ValueError(f"@{name} is stopped. Start it before sending a test.")
    registry = load_gateway_registry()
    stored = find_agent_entry(registry, str(entry.get("name") or "")) or entry
    ensure_gateway_identity_binding(registry, stored, session=load_gateway_session())
    snapshot = annotate_runtime_health(stored, registry=registry)
    save_gateway_registry(registry)
    reachability = str(snapshot.get("reachability") or "").strip().lower()
    if reachability == "attach_required":
        workdir = str(snapshot.get("workdir") or stored.get("workdir") or "").strip()
        suffix = f" Start Claude Code from {workdir}." if workdir else " Start Claude Code first."
        raise ValueError(f"@{name} is stopped and cannot receive messages yet.{suffix}")
    space_id = str(snapshot.get("active_space_id") or stored.get("space_id") or entry.get("space_id") or "")
    if not space_id:
        raise ValueError(f"Managed agent is missing a space id: @{name}")

    prompt = (content or "").strip() or _recommended_test_message(entry)
    target = str(entry.get("name") or "").lstrip("@")
    normalized_author = str(author or "agent").strip().lower()
    if normalized_author not in {"agent", "user"}:
        raise ValueError("Gateway test author must be one of: agent, user.")

    sender_name = None
    if normalized_author == "agent":
        target_for_sender = {**stored, "space_id": space_id}
        sender_error: str | None = None
        if sender_agent:
            sender_name = str(sender_agent).strip()
        else:
            try:
                sender_name = str(_ensure_gateway_test_sender(target_for_sender).get("name") or "")
            except Exception as exc:  # noqa: BLE001
                sender_error = str(exc)
                if not allow_self_fallback:
                    raise ValueError(f"Gateway service sender is not ready for @{target}: {sender_error}") from exc
                sender_name = target
        if not sender_name:
            raise ValueError("Gateway could not resolve a managed sender for the test message.")
        result = _send_from_managed_agent(
            name=sender_name,
            content=prompt,
            to=target,
            space_id=space_id,
            sent_via="gateway_test",
            metadata_extra={
                "managed_target": True,
                "target_agent_name": stored.get("name"),
                "target_agent_id": stored.get("agent_id"),
                "target_template": stored.get("template_id"),
                "target_runtime_type": stored.get("runtime_type"),
                "test_author": "agent",
                "test_sender_fallback": "self" if sender_error else None,
                "test_sender_error": sender_error,
            },
        )
        payload = result.get("message", result) if isinstance(result, dict) else result
        message_content = str(result.get("content") or f"@{target} {prompt}".strip())
    else:
        client = _load_gateway_user_client()
        message_content = f"@{target} {prompt}".strip()
        metadata = {
            "control_plane": "gateway",
            "gateway": {
                "managed_target": True,
                "target_agent_name": stored.get("name"),
                "target_agent_id": stored.get("agent_id"),
                "target_template": stored.get("template_id"),
                "target_runtime_type": stored.get("runtime_type"),
                "sent_via": "gateway_test",
                "test_author": "user",
            },
        }
        result = client.send_message(space_id, message_content, metadata=metadata)
        payload = result.get("message", result) if isinstance(result, dict) else result

    if isinstance(payload, dict):
        record_gateway_activity(
            "gateway_test_sent",
            entry=entry,
            message_id=payload.get("id"),
            reply_preview=message_content[:120] or None,
            sender_agent_name=sender_name,
            test_author=normalized_author,
        )
    return {
        "target_agent": entry.get("name"),
        "sender_agent": sender_name,
        "author": normalized_author,
        "message": payload,
        "content": message_content,
        "recommended_prompt": prompt,
    }


def _doctor_result_status(checks: list[dict]) -> str:
    statuses = {str(item.get("status") or "").strip().lower() for item in checks}
    if "failed" in statuses:
        return "failed"
    if "warning" in statuses:
        return "warning"
    return "passed"


def _doctor_summary(checks: list[dict], status: str) -> str:
    failures = [
        str(item.get("detail") or item.get("name") or "").strip()
        for item in checks
        if str(item.get("status") or "").strip().lower() == "failed"
    ]
    warnings = [
        str(item.get("detail") or item.get("name") or "").strip()
        for item in checks
        if str(item.get("status") or "").strip().lower() == "warning"
    ]
    if status == "failed" and failures:
        return failures[0]
    if status == "warning" and warnings:
        return warnings[0]
    return "Gateway path looks healthy."


def _store_doctor_result(name: str, result: dict[str, object]) -> dict:
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    completed_at = str(result.get("completed_at") or datetime.now(timezone.utc).isoformat())
    entry["last_doctor_result"] = result
    entry["last_doctor_at"] = completed_at
    if str(result.get("status") or "").lower() != "failed":
        entry["last_successful_doctor_at"] = completed_at
    save_gateway_registry(registry)
    record_gateway_activity(
        "doctor_completed",
        entry=entry,
        activity_message=str(result.get("summary") or ""),
        error=None if str(result.get("status") or "").lower() != "failed" else str(result.get("summary") or ""),
    )
    return annotate_runtime_health(entry, registry=registry)


def _run_gateway_doctor(name: str, *, send_test: bool = False) -> dict:
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    ensure_gateway_identity_binding(registry, entry, session=load_gateway_session(), verify_spaces=False)
    snapshot = annotate_runtime_health(entry, registry=registry)
    checks: list[dict[str, str]] = []
    asset_class = str(snapshot.get("asset_class") or "")
    intake_model = str(snapshot.get("intake_model") or "")
    return_paths = [str(item) for item in (snapshot.get("return_paths") or []) if str(item)]

    def add_check(check_name: str, status: str, detail: str) -> None:
        checks.append({"name": check_name, "status": status, "detail": detail})

    def has_check(check_name: str) -> bool:
        return any(str(item.get("name") or "") == check_name for item in checks)

    session = load_gateway_session()
    add_check(
        "gateway_auth",
        "passed" if session else "failed",
        "Gateway bootstrap session is present." if session else "Gateway is not logged in.",
    )

    identity_status = str(snapshot.get("identity_status") or "").lower()
    if identity_status == "verified":
        add_check(
            "identity_binding",
            "passed",
            f"Gateway is acting as {snapshot.get('acting_agent_name') or entry.get('name')}.",
        )
    elif identity_status == "bootstrap_only":
        add_check(
            "identity_binding",
            "failed",
            "Gateway would need to use a bootstrap credential for an agent-authored action.",
        )
    else:
        add_check(
            "identity_binding",
            "failed",
            str(snapshot.get("confidence_detail") or "Gateway does not have a valid acting identity binding."),
        )

    environment_status = str(snapshot.get("environment_status") or "").lower()
    if environment_status == "environment_allowed":
        add_check(
            "environment_binding",
            "passed",
            f"Requested environment matches {snapshot.get('environment_label') or snapshot.get('base_url') or entry.get('base_url')}.",
        )
    elif environment_status == "environment_mismatch":
        add_check(
            "environment_binding",
            "failed",
            str(snapshot.get("confidence_detail") or "Requested environment does not match the bound environment."),
        )
    else:
        add_check("environment_binding", "warning", "Gateway could not fully verify the bound environment.")

    allowed_spaces = snapshot.get("allowed_spaces") if isinstance(snapshot.get("allowed_spaces"), list) else []
    if allowed_spaces:
        add_check("allowed_spaces", "passed", f"Gateway resolved {len(allowed_spaces)} allowed space(s).")
    else:
        add_check("allowed_spaces", "warning", "Gateway does not have a cached allowed-space list yet.")

    space_status = str(snapshot.get("space_status") or "").lower()
    if space_status == "active_allowed":
        add_check(
            "space_binding",
            "passed",
            f"Active space is {snapshot.get('active_space_name') or snapshot.get('active_space_id')}.",
        )
    elif space_status == "no_active_space":
        add_check("space_binding", "failed", "Gateway does not have an active space selected for this asset.")
    elif space_status == "active_not_allowed":
        add_check(
            "space_binding",
            "failed",
            str(snapshot.get("confidence_detail") or "Active space is not allowed for this identity."),
        )
    else:
        add_check("space_binding", "warning", "Gateway could not fully verify the active space.")

    attestation_state = str(snapshot.get("attestation_state") or "").lower()
    approval_state = str(snapshot.get("approval_state") or "").lower()
    if approval_state == "pending":
        add_check(
            "binding_approval",
            "warning",
            str(snapshot.get("confidence_detail") or "Gateway needs approval before trusting this runtime binding."),
        )
    elif approval_state == "rejected" or attestation_state == "blocked":
        add_check(
            "binding_approval",
            "failed",
            str(snapshot.get("confidence_detail") or "Gateway blocked this runtime binding."),
        )
    elif attestation_state == "drifted":
        add_check(
            "binding_attestation",
            "failed",
            str(snapshot.get("confidence_detail") or "Runtime binding drifted from its approved launch spec."),
        )
    elif attestation_state == "verified":
        add_check("binding_attestation", "passed", "Runtime matches the approved local binding.")

    token_file = Path(str(entry.get("token_file") or "")).expanduser()
    if token_file.exists() and token_file.read_text().strip():
        add_check("agent_token", "passed", "Managed agent token file is present.")
    else:
        add_check("agent_token", "failed", f"Managed agent token is missing or empty at {token_file}.")

    if asset_class == "background_worker" or intake_model == "queue_accept":
        probe = agent_dir(name) / ".doctor-queue-check"
        try:
            probe.write_text("ok\n")
            probe.unlink(missing_ok=True)
            add_check("queue_writable", "passed", "Gateway queue is writable.")
        except OSError as exc:
            add_check("queue_writable", "failed", f"Gateway queue is not writable: {exc}")
        if bool(snapshot.get("connected")):
            add_check("worker_attached", "passed", "A queue worker is attached.")
        else:
            add_check("worker_attached", "warning", "Queue writable; no worker currently attached.")
        if "summary_post" in return_paths:
            add_check("summary_path", "passed", "Gateway is configured to post a summary after queued work completes.")
    else:
        exec_command = str(entry.get("exec_command") or "").strip()
        runtime_type = str(entry.get("runtime_type") or "").strip().lower()
        if intake_model == "live_listener":
            if snapshot.get("activation") == "attach_only":
                if str(snapshot.get("reachability") or "") == "attach_required":
                    add_check("claude_code_session", "warning", "Start Claude Code before sending.")
                elif bool(snapshot.get("connected")):
                    add_check("claude_code_session", "passed", "Claude Code is connected to Gateway.")
                else:
                    add_check("claude_code_session", "failed", "Gateway does not currently have Claude Code running.")
            elif runtime_type != "echo":
                if exec_command:
                    add_check("runtime_launch", "passed", "Gateway has a launch command for this runtime.")
                else:
                    add_check("runtime_launch", "failed", "Gateway does not have a launch command for this runtime.")
        elif intake_model == "launch_on_send":
            if runtime_type == "echo" or exec_command:
                add_check("launch_ready", "passed", "Gateway can launch this runtime when work arrives.")
            else:
                add_check(
                    "launch_ready", "failed", "Gateway does not have a launch command for this on-demand runtime."
                )
        elif intake_model == "scheduled_run":
            add_check(
                "schedule_ready",
                "warning",
                "Scheduled asset support is taxonomy-defined but not fully implemented in Gateway yet.",
            )
        elif intake_model == "event_triggered":
            add_check(
                "event_source",
                "warning",
                "Alert-driven asset support is taxonomy-defined but not fully implemented in Gateway yet.",
            )
        elif asset_class == "service_proxy":
            if exec_command:
                add_check("runtime_launch", "passed", "Gateway has a launch command for this runtime.")
            else:
                add_check("runtime_launch", "failed", "Gateway does not have a launch command for this runtime.")

    template_id = str(entry.get("template_id") or "").strip().lower()
    if template_id == "hermes":
        hermes_status = hermes_setup_status(entry)
        if hermes_status.get("ready", True):
            add_check("hermes_repo", "passed", str(hermes_status.get("summary") or "Hermes checkout found."))
        else:
            add_check("hermes_repo", "failed", str(hermes_status.get("summary") or "Hermes checkout not found."))
    elif template_id == "ollama":
        ollama_model = str(entry.get("ollama_model") or "").strip()
        ollama_status = ollama_setup_status(preferred_model=ollama_model or None)
        if bool(ollama_status.get("server_reachable")):
            add_check("ollama_server", "passed", str(ollama_status.get("summary") or "Ollama server is reachable."))
        else:
            add_check("ollama_server", "failed", str(ollama_status.get("summary") or "Ollama server is not reachable."))
        if ollama_model:
            if bool(ollama_status.get("preferred_model_available")):
                add_check("ollama_model", "passed", f"Gateway will launch Ollama with model {ollama_model}.")
            else:
                add_check("ollama_model", "failed", f"Configured Ollama model is not installed: {ollama_model}.")
        else:
            recommended_model = str(ollama_status.get("recommended_model") or "").strip()
            if recommended_model:
                add_check(
                    "ollama_model", "passed", f"Gateway will use the recommended local model {recommended_model}."
                )
            else:
                add_check("ollama_model", "warning", "No Ollama model is selected yet.")
        add_check("launch_path", "passed", "Gateway can launch the Ollama bridge on send.")

    if str(snapshot.get("mode") or "") == "LIVE":
        if str(snapshot.get("presence") or "") == "IDLE":
            add_check("live_path", "passed", "Live listener is connected.")
        elif str(snapshot.get("reachability") or "") == "attach_required":
            add_check("live_path", "warning", "Start Claude Code before sending.")
        elif str(snapshot.get("presence") or "") in {"STALE", "OFFLINE"}:
            add_check("live_path", "failed", str(snapshot.get("confidence_detail") or _reachability_copy(snapshot)))
    elif str(snapshot.get("mode") or "") == "ON-DEMAND" and not has_check("launch_ready"):
        add_check("launch_ready", "passed", "Gateway can launch this runtime on send.")

    if send_test:
        try:
            sent = _send_gateway_test_to_managed_agent(name)
            message_id = None
            if isinstance(sent.get("message"), dict):
                message_id = sent["message"].get("id")
            add_check("test_send", "passed", f"Gateway test message sent{f' ({message_id})' if message_id else ''}.")
        except Exception as exc:
            add_check("test_send", "failed", f"Gateway test send failed: {exc}")

    status = _doctor_result_status(checks)
    completed_at = datetime.now(timezone.utc).isoformat()
    result = {
        "status": status,
        "completed_at": completed_at,
        "checks": checks,
        "summary": _doctor_summary(checks, status),
    }
    annotated = _store_doctor_result(name, result)
    return {
        "name": name,
        "status": status,
        "completed_at": completed_at,
        "summary": result["summary"],
        "checks": checks,
        "agent": annotated,
    }


def _parse_iso8601(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_seconds(value: object) -> int | None:
    parsed = _parse_iso8601(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()))


def _format_age(seconds: object) -> str:
    if seconds is None:
        return "-"
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "-"
    if total < 60:
        return f"{total}s"
    minutes, seconds = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours:02d}h"


def _format_timestamp(value: object) -> str:
    return _format_age(_age_seconds(value))


def _state_text(state: object) -> Text:
    label = str(state or "unknown").lower()
    style = _STATE_STYLES.get(label, "white")
    return Text(f"● {label}", style=style)


def _presence_text(presence: object) -> Text:
    label = str(presence or "OFFLINE").upper()
    style = _PRESENCE_STYLES.get(label, "white")
    return Text(label, style=style)


def _confidence_text(confidence: object) -> Text:
    label = str(confidence or "MEDIUM").upper()
    style = _CONFIDENCE_STYLES.get(label, "white")
    return Text(label, style=style)


def _mode_text(mode: object) -> Text:
    label = str(mode or "ON-DEMAND").upper()
    style = {
        "LIVE": "green",
        "ON-DEMAND": "cyan",
        "INBOX": "blue",
    }.get(label, "white")
    return Text(label, style=style)


def _reply_text(reply: object) -> Text:
    label = str(reply or "REPLY").upper()
    style = {
        "REPLY": "green",
        "SUMMARY": "yellow",
        "SILENT": "dim",
    }.get(label, "white")
    return Text(label, style=style)


def _reachability_copy(agent: dict) -> str:
    reachability = str(agent.get("reachability") or "unavailable")
    mode = str(agent.get("mode") or "")
    if reachability == "live_now":
        return "Live listener ready to claim work."
    if reachability == "queue_available":
        return "Gateway can safely queue work now."
    if reachability == "launch_available":
        return "Gateway can launch this runtime on send."
    if reachability == "attach_required":
        return "Start Claude Code before sending."
    if mode == "INBOX":
        return "Queue path is unavailable."
    return "Gateway does not currently have a working path."


def _agent_template_label(agent: dict) -> str:
    return str(agent.get("template_label") or agent.get("runtime_type") or "-")


def _agent_type_label(agent: dict) -> str:
    return str(agent.get("asset_type_label") or "Connected Asset")


def _agent_output_label(agent: dict) -> str:
    return str(agent.get("output_label") or agent.get("reply") or "Reply")


def _metric_panel(label: str, value: object, *, tone: str = "cyan", subtitle: str | None = None) -> Panel:
    body = Text()
    body.append(str(value), style=f"bold {tone}")
    body.append(f"\n{label}", style="dim")
    if subtitle:
        body.append(f"\n{subtitle}", style="dim")
    return Panel(body, border_style=tone, padding=(1, 2))


def _sorted_agents(agents: list[dict]) -> list[dict]:
    return sorted(
        agents,
        key=lambda agent: (
            _PRESENCE_ORDER.get(str(agent.get("presence") or "").upper(), 99),
            str(agent.get("name") or "").lower(),
        ),
    )


def _render_gateway_overview(payload: dict) -> Panel:
    gateway = payload.get("gateway") or {}
    ui = payload.get("ui") or {}
    grid = Table.grid(expand=True, padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column(ratio=2)
    grid.add_column(style="bold")
    grid.add_column(ratio=2)
    grid.add_row(
        "Gateway",
        str(gateway.get("gateway_id") or "-")[:8],
        "Daemon",
        "running" if payload["daemon"]["running"] else "stopped",
    )
    grid.add_row("User", str(payload.get("user") or "-"), "Base URL", str(payload.get("base_url") or "-"))
    space_label = str(payload.get("space_name") or payload.get("space_id") or "-")
    grid.add_row("Space", space_label, "Environment", str(payload.get("gateway_environment") or "default"))
    grid.add_row("PID", str(payload["daemon"].get("pid") or "-"), "State Dir", str(payload.get("gateway_dir") or "-"))
    grid.add_row("UI", str(ui.get("url") or "-"), "UI PID", str(ui.get("pid") or "-"))
    grid.add_row(
        "Session",
        "connected" if payload.get("connected") else "disconnected",
        "Last Reconcile",
        _format_timestamp(gateway.get("last_reconcile_at")),
    )
    return Panel(grid, title="Gateway Overview", border_style="cyan")


def _render_agent_table(agents: list[dict]) -> Table:
    table = Table(expand=True, box=box.SIMPLE_HEAVY)
    table.add_column("Agent", style="bold")
    table.add_column("Type")
    table.add_column("Mode")
    table.add_column("Presence")
    table.add_column("Output")
    table.add_column("Confidence")
    table.add_column("Acting As")
    table.add_column("Current Space")
    table.add_column("Queue", justify="right")
    table.add_column("Seen", justify="right")
    table.add_column("Activity", overflow="fold")
    if not agents:
        table.add_row(
            "No managed agents",
            "-",
            Text("ON-DEMAND", style="dim"),
            Text("OFFLINE", style="dim"),
            Text("Reply", style="dim"),
            Text("MEDIUM", style="dim"),
            "-",
            "-",
            "0",
            "-",
            "-",
        )
        return table
    for agent in _sorted_agents(agents):
        activity = str(
            agent.get("current_activity")
            or agent.get("confidence_detail")
            or agent.get("current_tool")
            or agent.get("last_reply_preview")
            or "-"
        )
        table.add_row(
            f"@{agent.get('name')}",
            _agent_type_label(agent),
            _mode_text(agent.get("mode")),
            _presence_text(agent.get("presence")),
            Text(
                _agent_output_label(agent),
                style="green" if str(agent.get("output_label") or "").lower() == "reply" else "yellow",
            ),
            _confidence_text(agent.get("confidence")),
            str(agent.get("acting_agent_name") or agent.get("name") or "-"),
            str(agent.get("active_space_name") or agent.get("active_space_id") or agent.get("space_id") or "-"),
            str(agent.get("backlog_depth") or 0),
            _format_age(agent.get("last_seen_age_seconds")),
            activity,
        )
    return table


def _render_activity_table(activity: list[dict]) -> Table:
    table = Table(expand=True, box=box.SIMPLE_HEAVY)
    table.add_column("When", justify="right", no_wrap=True)
    table.add_column("Event", no_wrap=True)
    table.add_column("Agent", no_wrap=True)
    table.add_column("Detail", overflow="fold")
    if not activity:
        table.add_row("-", "idle", "-", "No activity yet")
        return table
    for item in activity:
        detail = (
            item.get("activity_message")
            or item.get("reply_preview")
            or item.get("tool_name")
            or item.get("error")
            or item.get("message_id")
            or "-"
        )
        agent_name = item.get("agent_name")
        table.add_row(
            _format_timestamp(item.get("ts")),
            str(item.get("event") or "-"),
            f"@{agent_name}" if agent_name else "-",
            str(detail),
        )
    return table


def _render_alert_table(alerts: list[dict]) -> Table:
    table = Table(expand=True, box=box.SIMPLE_HEAVY)
    table.add_column("Level", no_wrap=True)
    table.add_column("Alert", no_wrap=True)
    table.add_column("Agent", no_wrap=True)
    table.add_column("Detail", overflow="fold")
    if not alerts:
        table.add_row("info", "No active alerts", "-", "Gateway looks healthy.")
        return table
    for item in alerts:
        severity = str(item.get("severity") or "info").lower()
        style = {"error": "red", "warning": "yellow", "info": "cyan"}.get(severity, "white")
        agent_name = str(item.get("agent_name") or "")
        table.add_row(
            Text(severity, style=style),
            str(item.get("title") or "-"),
            f"@{agent_name}" if agent_name else "-",
            str(item.get("detail") or "-"),
        )
    return table


def _render_gateway_dashboard(payload: dict) -> Group:
    agents = payload.get("agents", [])
    summary = payload.get("summary", {})
    queue_depth = sum(int(agent.get("backlog_depth") or 0) for agent in agents)
    metrics = Columns(
        [
            _metric_panel("managed agents", summary.get("managed_agents", 0), tone="cyan"),
            _metric_panel("live", summary.get("live_agents", 0), tone="green"),
            _metric_panel("on-demand", summary.get("on_demand_agents", 0), tone="blue"),
            _metric_panel("inbox", summary.get("inbox_agents", 0), tone="cyan"),
            _metric_panel("pending approvals", summary.get("pending_approvals", 0), tone="yellow"),
            _metric_panel("low confidence", summary.get("low_confidence_agents", 0), tone="yellow"),
            _metric_panel("blocked", summary.get("blocked_agents", 0), tone="red"),
            _metric_panel("queue depth", queue_depth, tone="blue"),
        ],
        expand=True,
        equal=True,
    )
    return Group(
        _render_gateway_overview(payload),
        metrics,
        Panel(_render_alert_table(payload.get("alerts", [])), title="Alerts", border_style="red"),
        Panel(_render_agent_table(agents), title="Managed Agents", border_style="green"),
        Panel(
            _render_activity_table(payload.get("recent_activity", [])), title="Recent Activity", border_style="magenta"
        ),
    )


def _spaces_payload() -> dict:
    client = _load_gateway_user_client()
    raw = client.list_spaces()
    items = raw.get("spaces", raw) if isinstance(raw, dict) else raw
    spaces: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        space_id = str(item.get("id") or item.get("space_id") or "").strip()
        if not space_id:
            continue
        spaces.append(
            {
                "id": space_id,
                "name": str(item.get("name") or item.get("space_name") or space_id),
                "slug": str(item.get("slug") or "").strip() or None,
            }
        )
    session = load_gateway_session() or {}
    return {
        "spaces": spaces,
        "active_space_id": str(session.get("space_id") or "").strip() or None,
        "active_space_name": str(session.get("space_name") or "").strip() or None,
    }


def _move_managed_agent_space(name: str, new_space_id: str) -> dict:
    name = name.strip()
    new_space_id = new_space_id.strip()
    if not name:
        raise ValueError("Managed agent name is required.")
    if not new_space_id:
        raise ValueError("Target space_id is required.")
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    if bool(entry.get("pinned")):
        raise ValueError(f"@{name} is pinned to its current space. Unlock it before moving.")
    if str(entry.get("space_id") or "").strip() == new_space_id:
        apply_entry_current_space(entry, new_space_id)
        ensure_gateway_identity_binding(registry, entry, session=load_gateway_session())
        save_gateway_registry(registry)
        return annotate_runtime_health(entry, registry=registry)
    client = _load_gateway_user_client()
    identifier = str(entry.get("agent_id") or name)
    try:
        client.set_agent_placement(identifier, space_id=new_space_id, pinned=bool(entry.get("pinned")))
    except AttributeError:
        try:
            client.update_agent(identifier, space_id=new_space_id)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Backend rejected move: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Backend rejected move: {exc}") from exc
    # Re-read the canonical record from backend — gateway local registry is a view,
    # never the source of truth.
    backend_space_id = new_space_id
    backend_allowed_spaces: list[dict[str, object]] | None = None
    read_back_methods = [
        method
        for method in (getattr(client, "get_agent_placement", None), getattr(client, "get_agent", None))
        if callable(method)
    ]
    for read_back in read_back_methods:
        try:
            record = read_back(identifier)
            if isinstance(record, dict) and isinstance(record.get("_record"), dict):
                record = record["_record"]
            elif isinstance(record, dict):
                record = record.get("agent", record)
            if not isinstance(record, dict):
                continue
            canonical = str(
                record.get("space_id") or record.get("current_space") or record.get("default_space_id") or ""
            ).strip()
            if canonical:
                backend_space_id = canonical
            allowed = record.get("allowed_spaces")
            if isinstance(allowed, list):
                backend_allowed_spaces = [
                    item
                    if isinstance(item, dict)
                    else {"space_id": str(item), "name": str(item), "is_default": str(item) == backend_space_id}
                    for item in allowed
                    if item
                ]
            break
        except Exception:  # noqa: BLE001
            # Resync best-effort; the placement write already succeeded.
            continue
    previous_space_id = str(entry.get("space_id") or "").strip() or None
    if backend_allowed_spaces is not None:
        entry["allowed_spaces"] = backend_allowed_spaces
    apply_entry_current_space(entry, backend_space_id)
    ensure_gateway_identity_binding(registry, entry, session=load_gateway_session())
    # Capture the rebind marker BEFORE writing the registry so the wait below
    # is guaranteed to see only post-move runtime/listener events.
    rebind_marker = datetime.now(timezone.utc).isoformat()
    save_gateway_registry(registry)
    record_gateway_activity(
        "managed_agent_moved_space",
        entry=entry,
        new_space_id=backend_space_id,
        requested_space_id=new_space_id,
        previous_space_id=previous_space_id,
    )
    if backend_space_id != new_space_id:
        # Backend coerced the move (likely allowed_spaces enforcement). Surface to operator
        # logs so backend_sentinel can pick it up if it indicates a quarantine gap.
        record_gateway_activity(
            "managed_agent_move_coerced",
            entry=entry,
            requested_space_id=new_space_id,
            applied_space_id=backend_space_id,
        )
    # Wait for the daemon to finish the rebind before returning. The daemon
    # is a separate process polling the registry every ~1s; once it sees
    # space_id changed it stops the old runtime and starts a new one.
    # Without this wait, a follow-up POST /api/agents/<name>/test can land
    # on the new switchboard before the new SSE listener has connected,
    # stranding the message. Listener-backed runtimes are not ready at
    # runtime_started; wait for listener_connected so an immediate test send
    # does not race the new SSE connection. Cap at 5s — if no listener event
    # appears we still return with the refreshed registry state.
    # Skip when no daemon is running (e.g. tests, offline operator) since
    # nothing will produce the rebind events we are waiting on.
    if previous_space_id and previous_space_id != backend_space_id and active_gateway_pid() is not None:
        runtime_type = entry.get("runtime_type")
        ready_events = {"runtime_started"} if _is_passive_runtime(runtime_type) else {"listener_connected"}
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            recent = load_recent_gateway_activity(limit=20, agent_name=name)
            if any((event.get("ts") or "") > rebind_marker and event.get("event") in ready_events for event in recent):
                break
            time.sleep(0.2)
    return annotate_runtime_health(entry, registry=registry)


def _ack_managed_agent_message(
    name: str,
    *,
    message_id: str,
    reply_id: str | None = None,
    reply_preview: str | None = None,
) -> dict:
    """Pass-through ack: agent reports it processed message_id and optionally
    sent reply_id. Updates local registry's reply timestamps + counters, drops
    the message from the pending queue, fires reply_sent activity event so
    the simple-gateway drawer surfaces 'Replied · just now' on the row.
    """
    name = name.strip()
    if not name:
        raise ValueError("Managed agent name is required.")
    message_id = (message_id or "").strip()
    if not message_id:
        raise ValueError("message_id is required.")
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    now_iso = datetime.now(timezone.utc).isoformat()
    # Drop from pending queue (best-effort; the agent may have already cleaned
    # it up locally).
    items = load_agent_pending_messages(name)
    remaining = [item for item in items if str(item.get("message_id") or "") != message_id]
    if len(remaining) != len(items):
        save_agent_pending_messages(name, remaining)
    # Update registry entry so the row's last-action label and counters reflect
    # the reply that just went out via the agent's PAT.
    entry["last_work_completed_at"] = now_iso
    entry["last_reply_at"] = now_iso
    entry["last_received_message_id"] = message_id
    if reply_id:
        entry["last_reply_message_id"] = reply_id
    if reply_preview:
        entry["last_reply_preview"] = reply_preview[:240]
    entry["processed_count"] = int(entry.get("processed_count") or 0) + 1
    save_gateway_registry(registry)
    record_gateway_activity(
        "reply_sent",
        entry=entry,
        message_id=message_id,
        reply_message_id=reply_id,
        reply_preview=reply_preview,
    )
    return annotate_runtime_health(entry, registry=registry)


def _set_managed_agent_pin(name: str, pinned: bool) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Managed agent name is required.")
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    entry["pinned"] = bool(pinned)
    save_gateway_registry(registry)
    record_gateway_activity(
        "managed_agent_pinned" if pinned else "managed_agent_unpinned",
        entry=entry,
    )
    return annotate_runtime_health(entry, registry=registry)


def _render_gateway_ui_page(*, refresh_ms: int) -> str:
    template = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ax gateway ui</title>
  <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
  <style>
    :root {
      --bg: #081018;
      --panel: #0e1a24;
      --panel-2: #111f2b;
      --line: #1d3342;
      --text: #e7f7ff;
      --muted: #93afbf;
      --cyan: #47e7ff;
      --green: #53f977;
      --yellow: #f1d45f;
      --red: #ff6e6e;
      --blue: #5c98ff;
      --magenta: #ff5fe6;
      --shadow: 0 24px 80px rgba(0, 0, 0, 0.35);
      --radius: 20px;
      --radius-sm: 14px;
      --mono: "SFMono-Regular", "Menlo", "Monaco", "Consolas", monospace;
      --sans: "Avenir Next", "Segoe UI", "Helvetica Neue", sans-serif;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at top left, rgba(71, 231, 255, 0.18), transparent 32%),
        radial-gradient(circle at top right, rgba(92, 152, 255, 0.16), transparent 28%),
        linear-gradient(180deg, #071019 0%, #0b131c 100%);
      color: var(--text);
      font-family: var(--sans);
    }

    .shell {
      width: min(1400px, calc(100vw - 32px));
      margin: 20px auto 40px;
      display: grid;
      gap: 16px;
    }

    .panel {
      background: linear-gradient(180deg, rgba(14, 26, 36, 0.96), rgba(10, 21, 29, 0.96));
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      overflow: hidden;
    }

    .panel-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 18px 22px 0;
      font-family: var(--mono);
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--cyan);
      font-size: 13px;
    }

    .header-actions {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .panel-body {
      padding: 18px 22px 22px;
    }

    .hero {
      display: grid;
      grid-template-columns: 1.25fr 1fr;
      gap: 16px;
    }

    .hero-copy h1 {
      margin: 0 0 10px;
      font-size: clamp(28px, 3.3vw, 52px);
      line-height: 0.95;
      letter-spacing: -0.03em;
    }

    .hero-copy p {
      margin: 0;
      max-width: 44rem;
      color: var(--muted);
      line-height: 1.55;
      font-size: 15px;
    }

    .hero-meta {
      display: grid;
      gap: 12px;
      align-content: start;
    }

    .meta-chip {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--line);
      background: rgba(6, 17, 24, 0.6);
      font-family: var(--mono);
      font-size: 13px;
    }

    .metrics {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 16px;
    }

    .metric {
      padding: 18px;
      border-radius: var(--radius);
      border: 1px solid var(--line);
      background: rgba(8, 19, 27, 0.78);
    }

    .metric strong {
      display: block;
      font-size: 34px;
      margin-bottom: 4px;
      font-family: var(--mono);
    }

    .metric span {
      color: var(--muted);
      font-size: 14px;
    }

    .metric.cyan strong { color: var(--cyan); }
    .metric.green strong { color: var(--green); }
    .metric.yellow strong { color: var(--yellow); }
    .metric.red strong { color: var(--red); }
    .metric.blue strong { color: var(--blue); }

    .metric.red span,
    .metric.yellow span {
      color: var(--text);
    }

    .dashboard {
      display: grid;
      grid-template-columns: minmax(0, 1.3fr) minmax(360px, 0.9fr);
      gap: 16px;
    }

    .alerts-list {
      display: grid;
      gap: 12px;
    }

    .alert-card {
      padding: 14px 16px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(8, 19, 27, 0.7);
    }

    .alert-card.warning {
      border-color: rgba(241, 212, 95, 0.45);
      background: rgba(241, 212, 95, 0.08);
    }

    .alert-card.error {
      border-color: rgba(255, 110, 110, 0.45);
      background: rgba(255, 110, 110, 0.08);
    }

    .alert-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 6px;
      font-family: var(--mono);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .alert-body {
      color: var(--muted);
      line-height: 1.5;
      font-size: 14px;
    }

    .control-grid {
      display: grid;
      grid-template-columns: minmax(280px, 0.95fr) minmax(0, 1.05fr);
      gap: 16px;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }

    th {
      text-align: left;
      padding: 0 0 10px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      border-bottom: 1px solid var(--line);
    }

    td {
      padding: 12px 0;
      border-bottom: 1px solid rgba(29, 51, 66, 0.45);
      vertical-align: top;
    }

    tbody tr:last-child td {
      border-bottom: none;
    }

    .agent-button {
      width: 100%;
      border: 1px solid transparent;
      background: transparent;
      color: inherit;
      text-align: left;
      padding: 10px 12px;
      border-radius: 12px;
      transition: background 0.15s ease, border-color 0.15s ease, transform 0.15s ease;
      cursor: pointer;
    }

    .agent-button:hover,
    .agent-button.is-active {
      background: rgba(71, 231, 255, 0.08);
      border-color: rgba(71, 231, 255, 0.35);
      transform: translateY(-1px);
    }

    .agent-name {
      font-family: var(--mono);
      font-weight: 700;
      margin-bottom: 4px;
    }

    .agent-meta,
    .caption,
    .detail-list dd,
    .event-detail {
      color: var(--muted);
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 7px 11px;
      border-radius: 999px;
      border: 1px solid currentColor;
      font-family: var(--mono);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .status-live,
    .status-idle,
    .status-reply,
    .status-high { color: var(--green); }
    .status-on-demand,
    .status-queued,
    .status-medium { color: var(--cyan); }
    .status-inbox { color: var(--blue); }
    .status-summary,
    .status-blocked,
    .status-stale,
    .status-low { color: var(--yellow); }
    .status-error,
    .status-blocked { color: var(--red); }
    .status-offline,
    .status-silent { color: var(--muted); }

    .detail-card {
      display: grid;
      gap: 16px;
    }

    .action-row,
    .form-grid {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }

    .form-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }

    .control-group {
      display: grid;
      gap: 8px;
    }

    label {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }

    input,
    select,
    textarea,
    button {
      width: 100%;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: rgba(8, 19, 27, 0.9);
      color: var(--text);
      font: inherit;
      padding: 12px 14px;
    }

    textarea {
      min-height: 96px;
      resize: vertical;
    }

    button {
      width: auto;
      cursor: pointer;
      font-family: var(--mono);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      transition: transform 0.15s ease, border-color 0.15s ease, background 0.15s ease;
    }

    button:hover {
      transform: translateY(-1px);
      border-color: rgba(71, 231, 255, 0.35);
      background: rgba(71, 231, 255, 0.08);
    }

    button.danger:hover {
      border-color: rgba(255, 110, 110, 0.35);
      background: rgba(255, 110, 110, 0.08);
    }

    button.ghost {
      background: transparent;
      border-color: rgba(71, 231, 255, 0.22);
      color: var(--muted);
    }

    .flash {
      min-height: 24px;
      color: var(--muted);
      font-size: 13px;
    }

    .flash.error {
      color: var(--red);
    }

    .flash.success {
      color: var(--green);
    }

    .flash.warning {
      color: var(--yellow);
    }

    .runtime-info {
      display: grid;
      gap: 12px;
      padding: 14px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(8, 19, 27, 0.58);
    }

    .runtime-info h3 {
      margin: 0;
      font-size: 16px;
      font-family: var(--mono);
    }

    .runtime-info p {
      margin: 0;
      color: var(--muted);
      line-height: 1.5;
      font-size: 14px;
    }

    .runtime-info summary {
      cursor: pointer;
      font-family: var(--mono);
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--text);
      list-style: none;
    }

    .runtime-info summary::-webkit-details-marker {
      display: none;
    }

    .signal-grid {
      display: grid;
      gap: 10px;
    }

    .signal-grid div {
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: rgba(6, 17, 24, 0.55);
    }

    .signal-grid strong {
      display: block;
      margin-bottom: 6px;
      font-family: var(--mono);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--cyan);
    }

    .detail-list {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px 20px;
      margin: 0;
    }

    .detail-list div {
      padding: 12px 14px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(8, 19, 27, 0.58);
    }

    .detail-list dt {
      margin: 0 0 6px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }

    .detail-list dd {
      margin: 0;
      line-height: 1.45;
      word-break: break-word;
    }

    .event-list {
      display: grid;
      gap: 10px;
    }

    .event-item {
      padding: 12px 14px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(8, 19, 27, 0.58);
    }

    .event-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 6px;
      font-family: var(--mono);
      font-size: 12px;
      color: var(--text);
    }

    .event-detail {
      font-size: 14px;
      line-height: 1.45;
    }

    .copyable-block {
      position: relative;
    }

    .copyable-block pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font: inherit;
      color: inherit;
    }

    .empty {
      padding: 18px;
      border-radius: 14px;
      border: 1px dashed var(--line);
      color: var(--muted);
      text-align: center;
    }

    .footer-note {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      color: var(--muted);
      font-size: 13px;
    }

    .footer-note code {
      font-family: var(--mono);
      color: var(--text);
    }

    .badge {
      display: inline-block;
      padding: 6px 9px;
      border-radius: 999px;
      background: rgba(71, 231, 255, 0.08);
      color: var(--cyan);
      border: 1px solid rgba(71, 231, 255, 0.22);
      font-family: var(--mono);
      font-size: 12px;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }

    @media (max-width: 1100px) {
      .hero,
      .dashboard {
        grid-template-columns: 1fr;
      }

      .metrics {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }

    @media (max-width: 720px) {
      .shell {
        width: min(100vw - 16px, 100%);
        margin: 8px auto 24px;
      }

      .metrics,
      .detail-list {
        grid-template-columns: 1fr;
      }

      .panel-header,
      .panel-body {
        padding-left: 16px;
        padding-right: 16px;
      }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="panel">
      <div class="panel-body hero">
        <div class="hero-copy">
          <div class="badge"><a href="/" style="color:inherit; text-decoration:none;">← Back to quick view</a> · Gateway Control Plane · Agent Operated</div>
          <h1>One local Gateway. Every agent in one place.</h1>
          <p>
            This dashboard is served locally by <code>ax gateway ui</code> and reads the
            same Gateway state model as the terminal watch view. The browser is a human
            view over the same local control plane that setup agents use through the CLI
            and local API instead of maintaining separate logic.
          </p>
        </div>
      <div id="overview" class="hero-meta"></div>
      </div>
    </section>

    <section id="metrics" class="metrics"></section>

    <section class="panel">
      <div class="panel-header">
        <span>Alerts</span>
        <span id="alert-summary" class="caption">loading…</span>
      </div>
      <div id="alerts-feed" class="panel-body">
        <div class="empty">Waiting for Gateway alerts…</div>
      </div>
    </section>

    <section class="control-grid">
      <section class="panel">
        <div class="panel-header">
          <span>Gateway Agent Setup</span>
          <span id="setup-mode-chip" class="caption">agent skill · create</span>
        </div>
        <div class="panel-body">
          <form id="add-agent-form" class="detail-card">
            <p class="caption">
              This form mirrors the <code>gateway-agent-setup</code> skill. Agents and humans
              should use the same Gateway-native setup, doctor, and update flow.
            </p>
            <div class="form-grid">
              <div class="control-group">
                <label for="agent-name">Name</label>
                <input id="agent-name" name="name" placeholder="hermes-bot" required />
              </div>
              <div class="control-group">
                <label for="agent-type">Agent Type</label>
                <select id="agent-type" name="template_id">
                </select>
              </div>
            </div>
            <div id="runtime-help" class="runtime-info">
              <h3>Loading agent type…</h3>
            </div>
            <details id="advanced-launch" class="runtime-info" style="display:none;">
              <summary>Advanced launch settings</summary>
              <p>
                Most setups should leave this alone. These fields exist so we can override
                the default launch command while debugging or building new adapters.
              </p>
              <div class="form-grid">
                <div class="control-group" id="exec-command-group">
                  <label for="agent-exec">Command Override</label>
                  <input id="agent-exec" name="exec_command" placeholder="python3 examples/hermes_sentinel/hermes_bridge.py" />
                </div>
                <div class="control-group" id="workdir-group">
                  <label for="agent-workdir">Working Directory Override</label>
                  <input id="agent-workdir" name="workdir" placeholder="/absolute/path/to/workdir" />
                </div>
                <div class="control-group" id="ollama-model-group" style="display:none;">
                  <label for="agent-ollama-model">Ollama Model</label>
                  <input id="agent-ollama-model" name="ollama_model" list="ollama-model-options" placeholder="gemma4:latest" />
                  <datalist id="ollama-model-options"></datalist>
                  <div id="ollama-model-caption" class="caption"></div>
                </div>
              </div>
            </details>
            <div class="action-row">
              <button id="add-agent-submit" type="submit">Add Agent</button>
              <button id="add-agent-cancel" type="button" class="ghost" style="display:none;">Cancel Edit</button>
            </div>
            <div id="add-agent-flash" class="flash"></div>
          </form>
        </div>
      </section>

      <section class="panel">
        <div class="panel-header">
          <span>Custom Message</span>
          <span id="quick-send-chip" class="caption">splunk · datadog · cron · manual</span>
        </div>
        <div class="panel-body">
          <form id="send-form" class="detail-card">
            <p class="caption">
              Use <strong>Send Agent Test</strong> for the standard validation path.
              Use this form for custom payloads, alerts, and scheduled-job style messages.
            </p>
            <div class="form-grid">
              <div class="control-group">
                <label for="send-to">To</label>
                <input id="send-to" name="to" placeholder="codex" />
              </div>
              <div class="control-group">
                <label for="send-parent-id">Parent ID</label>
                <input id="send-parent-id" name="parent_id" placeholder="optional thread parent" />
              </div>
            </div>
            <div class="control-group">
              <label for="send-content">Message</label>
              <textarea id="send-content" name="content" placeholder="Send a custom payload through Gateway: Datadog alert, Splunk event, cron reminder, or manual task"></textarea>
            </div>
            <div class="action-row">
              <button type="submit">Send Custom Message</button>
            </div>
            <div id="send-flash" class="flash"></div>
          </form>
        </div>
      </section>
    </section>

    <section class="dashboard">
      <section class="panel">
        <div class="panel-header">
          <span>Managed Agents</span>
          <span id="managed-summary" class="caption">loading…</span>
        </div>
        <div class="panel-body">
          <table>
            <thead>
              <tr>
                <th>Agent</th>
                <th>Type</th>
                <th>Mode</th>
                <th>Presence</th>
                <th>Output</th>
                <th>Confidence</th>
                <th>Queue</th>
                <th>Seen</th>
                <th>Activity</th>
              </tr>
            </thead>
            <tbody id="agent-rows">
              <tr><td colspan="9"><div class="empty">Waiting for Gateway state…</div></td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="panel">
        <div class="panel-header">
          <span>Agent Drill-In</span>
          <div class="header-actions">
            <button id="refresh-toggle" type="button" class="ghost">Pause Refresh</button>
            <span id="selected-agent-chip" class="caption">select an agent</span>
          </div>
        </div>
        <div id="agent-detail" class="panel-body">
          <div class="empty">Choose a managed agent to inspect live detail.</div>
        </div>
      </section>
    </section>

    <section class="panel">
      <div class="panel-header">
        <span>Recent Activity</span>
        <span class="caption">auto-refresh every __REFRESH_MS__ ms</span>
      </div>
      <div id="activity-feed" class="panel-body">
        <div class="empty">Waiting for activity…</div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-body footer-note">
        <span>Local status API: <code>/api/status</code> and <code>/api/agents/&lt;name&gt;</code></span>
        <span>Setup skill: <code>skills/gateway-agent-setup/SKILL.md</code> · Terminal parity: <code>uv run ax gateway watch</code></span>
      </div>
    </section>
  </main>

  <script>
    const refreshMs = __REFRESH_MS__;
    let selectedAgent = null;
    let agentTemplates = [];
    let autoRefreshPaused = false;
    let setupMode = "create";
    let setupTarget = null;

    async function apiRequest(path, options = {}) {
      const response = await fetch(path, {
        cache: "no-store",
        headers: { "Content-Type": "application/json", ...(options.headers || {}) },
        ...options,
      });
      const isJson = (response.headers.get("Content-Type") || "").includes("application/json");
      const payload = isJson ? await response.json() : null;
      if (!response.ok) {
        throw new Error(payload?.error || `request failed (${response.status})`);
      }
      return payload;
    }

    function setFlash(id, message, kind = "") {
      const node = document.getElementById(id);
      node.className = `flash ${kind}`.trim();
      node.textContent = message || "";
    }

    function applySetupMode() {
      const chip = document.getElementById("setup-mode-chip");
      const submitButton = document.getElementById("add-agent-submit");
      const cancelButton = document.getElementById("add-agent-cancel");
      const nameInput = document.getElementById("agent-name");
      const editing = setupMode === "update" && setupTarget;
      chip.textContent = editing ? `agent skill · editing @${setupTarget}` : "agent skill · create";
      submitButton.textContent = editing ? "Update Agent" : "Add Agent";
      cancelButton.style.display = editing ? "inline-flex" : "none";
      nameInput.readOnly = Boolean(editing);
    }

    function resetSetupForm() {
      const form = document.getElementById("add-agent-form");
      setupMode = "create";
      setupTarget = null;
      form.reset();
      document.getElementById("agent-type").value = "echo_test";
      renderTemplateHelp("echo_test");
      applySetupMode();
    }

    async function loadAgentIntoSetupForm(name) {
      const detail = await apiRequest(`/api/agents/${encodeURIComponent(name)}`);
      const agent = detail.agent || {};
      const nameInput = document.getElementById("agent-name");
      const typeInput = document.getElementById("agent-type");
      const execInput = document.getElementById("agent-exec");
      const workdirInput = document.getElementById("agent-workdir");
      const ollamaModelInput = document.getElementById("agent-ollama-model");

      setupMode = "update";
      setupTarget = agent.name || name;
      nameInput.value = agent.name || name;
      if (agent.template_id) {
        typeInput.value = agent.template_id;
        renderTemplateHelp(agent.template_id);
      }
      execInput.value = agent.exec_command || "";
      workdirInput.value = agent.workdir || "";
      ollamaModelInput.value = agent.ollama_model || "";
      applySetupMode();
      setFlash("add-agent-flash", `Editing @${setupTarget}`, "success");
      document.getElementById("add-agent-form").scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function refreshButtonLabel() {
      const button = document.getElementById("refresh-toggle");
      if (!button) return;
      button.textContent = autoRefreshPaused ? "Resume Refresh" : "Pause Refresh";
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }

    function formatAge(seconds) {
      if (seconds === null || seconds === undefined || seconds === "" || Number.isNaN(Number(seconds))) {
        return "-";
      }
      const total = Math.max(0, Number(seconds));
      if (total < 60) return `${Math.floor(total)}s`;
      const minutes = Math.floor(total / 60);
      const secs = Math.floor(total % 60);
      if (minutes < 60) return `${minutes}m ${String(secs).padStart(2, "0")}s`;
      const hours = Math.floor(minutes / 60);
      const mins = minutes % 60;
      if (hours < 24) return `${hours}h ${String(mins).padStart(2, "0")}m`;
      const days = Math.floor(hours / 24);
      const remHours = hours % 24;
      return `${days}d ${String(remHours).padStart(2, "0")}h`;
    }

    function stateClass(state) {
      return `status-${String(state || "stopped").toLowerCase()}`;
    }

    function detailText(item) {
      return item?.activity_message || item?.reply_preview || item?.tool_name || item?.error || item?.message_id || "-";
    }

    function getTemplateDefinition(templateId) {
      return agentTemplates.find((item) => item.id === templateId) || null;
    }

    function renderTemplateOptions() {
      const select = document.getElementById("agent-type");
      if (!agentTemplates.length) {
        select.innerHTML = `<option value="echo_test">Echo (Test)</option>`;
        return;
      }
      select.innerHTML = agentTemplates.map((item) => {
        const suffix = item.availability === "coming_soon" ? " (Soon)" : "";
        const disabled = item.launchable ? "" : " disabled";
        return `<option value="${escapeHtml(item.id)}"${disabled}>${escapeHtml(item.label + suffix)}</option>`;
      }).join("");
    }

    function renderTemplateHelp(templateId) {
      const definition = getTemplateDefinition(templateId);
      const help = document.getElementById("runtime-help");
      const advancedLaunch = document.getElementById("advanced-launch");
      const submitButton = document.getElementById("add-agent-submit");
      const agentNameInput = document.getElementById("agent-name");
      const execGroup = document.getElementById("exec-command-group");
      const workdirGroup = document.getElementById("workdir-group");
      const ollamaModelGroup = document.getElementById("ollama-model-group");
      const execInput = document.getElementById("agent-exec");
      const workdirInput = document.getElementById("agent-workdir");
      const ollamaModelInput = document.getElementById("agent-ollama-model");
      const ollamaModelOptions = document.getElementById("ollama-model-options");
      const ollamaModelCaption = document.getElementById("ollama-model-caption");
      if (!definition) {
        help.innerHTML = `<h3>Unknown agent type</h3><p>No template definition found.</p>`;
        advancedLaunch.style.display = "none";
        submitButton.disabled = true;
        return;
      }

      const defaults = definition.defaults || {};
      const advanced = definition.advanced || {};
      const supportsOverride = Boolean(advanced.supports_command_override);
      const supportsOllamaModel = definition.id === "ollama";
      const availableOllamaModels = Array.isArray(definition.ollama_available_models) ? definition.ollama_available_models : [];
      const recommendedOllamaModel = definition.ollama_recommended_model || defaults.ollama_model || "";
      advancedLaunch.style.display = supportsOverride ? "grid" : "none";
      execGroup.style.display = supportsOverride ? "grid" : "none";
      workdirGroup.style.display = supportsOverride ? "grid" : "none";
      ollamaModelGroup.style.display = supportsOllamaModel ? "grid" : "none";
      submitButton.disabled = !definition.launchable;

      execInput.placeholder = defaults.exec_command || execInput.placeholder;
      workdirInput.placeholder = defaults.workdir || workdirInput.placeholder;
      ollamaModelInput.placeholder = "gemma4:latest";
      ollamaModelOptions.innerHTML = availableOllamaModels
        .map((item) => `<option value="${escapeHtml(item)}"></option>`)
        .join("");

      if (supportsOverride) {
        execInput.value = defaults.exec_command || "";
        workdirInput.value = defaults.workdir || "";
      }
      if (supportsOllamaModel) {
        ollamaModelInput.value = ollamaModelInput.value || recommendedOllamaModel || "";
        ollamaModelCaption.style.display = "block";
        ollamaModelCaption.textContent = definition.ollama_summary
          || (availableOllamaModels.length
            ? `Installed models: ${availableOllamaModels.join(", ")}`
            : "Gateway could not verify local Ollama models yet.");
      }
      if (!supportsOverride) {
        execInput.value = "";
        workdirInput.value = "";
      }
      if (!supportsOllamaModel) {
        ollamaModelInput.value = "";
        ollamaModelCaption.textContent = "";
        ollamaModelCaption.style.display = "none";
        ollamaModelOptions.innerHTML = "";
      }

      agentNameInput.placeholder = definition.suggested_name || agentNameInput.placeholder;

      const whatYouNeed = (definition.what_you_need || []).length
        ? `<div><strong>What you'll need</strong>${definition.what_you_need.map((note) => `<div>${escapeHtml(note)}</div>`).join("")}</div>`
        : `<div><strong>What you'll need</strong><div>Nothing extra. This one is ready to go.</div></div>`;
      const launchMode = definition.launchable ? "ready to add" : "coming soon";
      const recommendedTest = definition.recommended_test_message
        ? `<div><strong>Recommended test</strong><div>${escapeHtml(definition.recommended_test_message)}</div></div>`
        : "";
      const setupSkill = definition.setup_skill
        ? `<div><strong>Setup skill</strong><div>${escapeHtml(definition.setup_skill)} · ${escapeHtml(definition.setup_skill_path || "")}</div></div>`
        : "";

      help.innerHTML = `
        <h3>${escapeHtml(definition.label)}</h3>
        <p>${escapeHtml(definition.description || "")}</p>
        <div class="signal-grid">
          <div><strong>Type</strong>${escapeHtml(definition.asset_type_label || "-")}</div>
          <div><strong>Output</strong>${escapeHtml(definition.output_label || "-")}</div>
          <div><strong>Intake</strong>${escapeHtml(definition.intake_model || "-")}</div>
          <div><strong>Telemetry</strong>${escapeHtml(definition.telemetry_shape || "-")}</div>
          <div><strong>Why pick this</strong>${escapeHtml(definition.operator_summary || "-")}</div>
          <div><strong>Status</strong>${escapeHtml(definition.availability || "-")} · ${escapeHtml(launchMode)}</div>
          <div><strong>Model</strong>${escapeHtml(definition.id === "ollama" ? (definition.ollama_summary || "Use Ollama Model to pick a local model.") : "-")}</div>
          <div><strong>Delivery</strong>${escapeHtml(definition.signals?.delivery || "-")}</div>
          <div><strong>Liveness</strong>${escapeHtml(definition.signals?.liveness || "-")}</div>
          <div><strong>Activity</strong>${escapeHtml(definition.signals?.activity || "-")}</div>
          <div><strong>Tools</strong>${escapeHtml(definition.signals?.tools || "-")}</div>
          ${setupSkill}
          ${recommendedTest}
          ${whatYouNeed}
        </div>
      `;
    }

    async function loadTemplates() {
      const payload = await apiRequest("/api/templates");
      agentTemplates = payload.templates || [];
      renderTemplateOptions();
      renderTemplateHelp(document.getElementById("agent-type").value || "echo_test");
    }

    function renderOverview(payload) {
      const gateway = payload.gateway || {};
      const overview = document.getElementById("overview");
      overview.innerHTML = `
        <div class="meta-chip"><span>Gateway</span><strong>${escapeHtml(String(gateway.gateway_id || "-").slice(0, 8))}</strong></div>
        <div class="meta-chip"><span>Daemon</span><strong>${payload.daemon?.running ? "running" : "stopped"}</strong></div>
        <div class="meta-chip"><span>Base URL</span><strong>${escapeHtml(payload.base_url || "-")}</strong></div>
        <div class="meta-chip"><span>User</span><strong>${escapeHtml(payload.user || "-")}</strong></div>
        <div class="meta-chip"><span>Space</span><strong>${escapeHtml(payload.space_name || payload.space_id || "-")}</strong></div>
      `;
    }

    function renderMetrics(payload) {
      const agents = payload.agents || [];
      const summary = payload.summary || {};
      const queueDepth = agents.reduce((sum, agent) => sum + Number(agent.backlog_depth || 0), 0);
      const metrics = [
        ["managed agents", summary.managed_agents ?? 0, "cyan"],
        ["live", summary.live_agents ?? 0, "green"],
        ["on-demand", summary.on_demand_agents ?? 0, "blue"],
        ["inbox", summary.inbox_agents ?? 0, "cyan"],
        ["pending approvals", summary.pending_approvals ?? 0, "yellow"],
        ["low confidence", summary.low_confidence_agents ?? 0, "yellow"],
        ["blocked", summary.blocked_agents ?? 0, "red"],
        ["queue depth", queueDepth, "blue"],
      ];
      document.getElementById("metrics").innerHTML = metrics.map(([label, value, tone]) => `
        <article class="metric ${tone}">
          <strong>${escapeHtml(value)}</strong>
          <span>${escapeHtml(label)}</span>
        </article>
      `).join("");
    }

    function renderAlerts(payload) {
      const alerts = payload.alerts || [];
      document.getElementById("alert-summary").textContent = alerts.length
        ? `${alerts.length} active alert${alerts.length === 1 ? "" : "s"}`
        : "all clear";
      const feed = document.getElementById("alerts-feed");
      if (!alerts.length) {
        feed.innerHTML = `<div class="empty">No active Gateway alerts.</div>`;
        return;
      }
      feed.innerHTML = `<div class="alerts-list">${
        alerts.map((item) => `
          <div class="alert-card ${escapeHtml(item.severity || "info")}">
            <div class="alert-head">
              <span>${escapeHtml(item.severity || "info")}</span>
              <span>${escapeHtml(item.agent_name ? "@" + item.agent_name : "gateway")}</span>
            </div>
            <div><strong>${escapeHtml(item.title || "-")}</strong></div>
            <div class="alert-body">${escapeHtml(item.detail || "-")}</div>
          </div>
        `).join("")
      }</div>`;
    }

    function renderAgents(payload) {
      const agents = payload.agents || [];
      const tbody = document.getElementById("agent-rows");
      document.getElementById("managed-summary").textContent = `${agents.length} managed agent${agents.length === 1 ? "" : "s"}`;
      if (!agents.length) {
        tbody.innerHTML = `<tr><td colspan="9"><div class="empty">No managed agents yet.</div></td></tr>`;
        return;
      }
      tbody.innerHTML = agents.map((agent) => {
        const activity = agent.current_activity || agent.confidence_detail || agent.current_tool || agent.last_reply_preview || "-";
        const active = selectedAgent && selectedAgent.toLowerCase() === String(agent.name || "").toLowerCase();
        return `
          <tr>
            <td colspan="8">
              <button class="agent-button ${active ? "is-active" : ""}" data-agent-name="${escapeHtml(agent.name || "")}">
                <table>
                  <tbody>
                    <tr>
                      <td style="width:16%">
                        <div class="agent-name">@${escapeHtml(agent.name || "-")}</div>
                        <div class="agent-meta">${escapeHtml(agent.template_label || agent.runtime_type || "-")}</div>
                      </td>
                      <td style="width:12%">${escapeHtml(agent.asset_type_label || "Connected Asset")}</td>
                      <td style="width:8%"><span class="status-pill ${stateClass(agent.mode)}">${escapeHtml(agent.mode || "ON-DEMAND")}</span></td>
                      <td style="width:8%"><span class="status-pill ${stateClass(agent.presence)}">${escapeHtml(agent.presence || "OFFLINE")}</span></td>
                      <td style="width:8%">${escapeHtml(agent.output_label || agent.reply || "Reply")}</td>
                      <td style="width:10%"><span class="status-pill ${stateClass(agent.confidence)}">${escapeHtml(agent.confidence || "MEDIUM")}</span></td>
                      <td style="width:10%">${escapeHtml(agent.acting_agent_name || agent.name || "-")}</td>
                      <td style="width:12%">${escapeHtml(agent.active_space_name || agent.active_space_id || agent.space_id || "-")}</td>
                      <td style="width:6%">${escapeHtml(agent.backlog_depth || 0)}</td>
                      <td style="width:8%">${escapeHtml(formatAge(agent.last_seen_age_seconds))}</td>
                      <td style="width:22%">${escapeHtml(activity)}</td>
                    </tr>
                  </tbody>
                </table>
              </button>
            </td>
          </tr>
        `;
      }).join("");
    }

    function renderActivity(payload) {
      const activity = payload.recent_activity || [];
      const feed = document.getElementById("activity-feed");
      if (!activity.length) {
        feed.innerHTML = `<div class="empty">No recent Gateway activity.</div>`;
        return;
      }
      feed.innerHTML = `<div class="event-list">${
        activity.map((item) => `
          <div class="event-item">
            <div class="event-head">
              <span>${escapeHtml(item.event || "-")}</span>
              <span>${escapeHtml(formatAge(item.ts ? Math.max(0, ((Date.now() - Date.parse(item.ts)) / 1000)) : null))}</span>
            </div>
            <div class="event-detail">@${escapeHtml(item.agent_name || "-")} · ${escapeHtml(detailText(item))}</div>
          </div>
        `).join("")
      }</div>`;
    }

    function renderAgentDetail(detail) {
      const panel = document.getElementById("agent-detail");
      const chip = document.getElementById("selected-agent-chip");
      const sendChip = document.getElementById("quick-send-chip");
      if (!detail || !detail.agent) {
        chip.textContent = "select an agent";
        sendChip.textContent = "select an agent";
        panel.innerHTML = `<div class="empty">Choose a managed agent to inspect live detail.</div>`;
        return;
      }
      const agent = detail.agent;
      chip.textContent = `@${agent.name}`;
      sendChip.textContent = `custom send as @${agent.name}`;
      const events = detail.recent_activity || [];
      const lastReply = escapeHtml(agent.last_reply_preview || "-");
      const lastReplyCopy = encodeURIComponent(String(agent.last_reply_preview || "-"));
      panel.innerHTML = `
        <div class="detail-card">
          <div>
            <div class="agent-name">@${escapeHtml(agent.name || "-")}</div>
            <div class="caption">${escapeHtml(agent.asset_type_label || "Connected Asset")} · ${escapeHtml(agent.template_label || agent.runtime_type || "-")} · ${escapeHtml(agent.transport || "-")}</div>
          </div>
          <div class="action-row">
            <button type="button" class="ghost" data-agent-action="edit" data-agent-name="${escapeHtml(agent.name || "")}">Edit Setup</button>
            <button type="button" data-agent-action="test" data-agent-name="${escapeHtml(agent.name || "")}">Send Agent Test</button>
            <button type="button" data-agent-action="doctor" data-agent-name="${escapeHtml(agent.name || "")}">Doctor</button>
            <button type="button" data-agent-action="start" data-agent-name="${escapeHtml(agent.name || "")}">Start</button>
            <button type="button" data-agent-action="stop" data-agent-name="${escapeHtml(agent.name || "")}">Stop</button>
            <button type="button" class="danger" data-agent-action="remove" data-agent-name="${escapeHtml(agent.name || "")}">Remove</button>
          </div>
          <div id="detail-flash" class="flash"></div>
          <dl class="detail-list">
            <div><dt>Type</dt><dd>${escapeHtml(agent.asset_type_label || "-")}</dd></div>
            <div><dt>Template</dt><dd>${escapeHtml(agent.template_label || agent.runtime_type || "-")}</dd></div>
            <div><dt>Mode</dt><dd>${escapeHtml(agent.mode || "-")}</dd></div>
            <div><dt>Presence</dt><dd>${escapeHtml(agent.presence || "-")}</dd></div>
            <div><dt>Output</dt><dd>${escapeHtml(agent.output_label || agent.reply || "-")}</dd></div>
            <div><dt>Confidence</dt><dd>${escapeHtml(agent.confidence || "-")}</dd></div>
            <div><dt>Asset Class</dt><dd>${escapeHtml(agent.asset_class || "-")}</dd></div>
            <div><dt>Intake</dt><dd>${escapeHtml(agent.intake_model || "-")}</dd></div>
            <div><dt>Trigger</dt><dd>${escapeHtml((agent.trigger_sources || [])[0] || "-")}</dd></div>
            <div><dt>Return</dt><dd>${escapeHtml((agent.return_paths || [])[0] || "-")}</dd></div>
            <div><dt>Telemetry</dt><dd>${escapeHtml(agent.telemetry_shape || "-")}</dd></div>
            <div><dt>Runtime Model</dt><dd>${escapeHtml(agent.ollama_model || "-")}</dd></div>
            <div><dt>Attestation</dt><dd>${escapeHtml(agent.attestation_state || "-")}</dd></div>
            <div><dt>Approval</dt><dd>${escapeHtml(agent.approval_state || "-")}</dd></div>
            <div><dt>Acting As</dt><dd>${escapeHtml(agent.acting_agent_name || "-")}</dd></div>
            <div><dt>Identity Status</dt><dd>${escapeHtml(agent.identity_status || "-")}</dd></div>
            <div><dt>Environment</dt><dd>${escapeHtml(agent.environment_label || agent.base_url || "-")}</dd></div>
            <div><dt>Environment Status</dt><dd>${escapeHtml(agent.environment_status || "-")}</dd></div>
            <div><dt>Current Space</dt><dd>${escapeHtml(agent.active_space_name || agent.active_space_id || "-")}</dd></div>
            <div><dt>Space Status</dt><dd>${escapeHtml(agent.space_status || "-")}</dd></div>
            <div><dt>Default Space</dt><dd>${escapeHtml(agent.default_space_name || agent.default_space_id || "-")}</dd></div>
            <div><dt>Allowed Spaces</dt><dd>${escapeHtml(agent.allowed_space_count || 0)}</dd></div>
            <div><dt>Install</dt><dd>${escapeHtml(agent.install_id || "-")}</dd></div>
            <div><dt>Runtime Instance</dt><dd>${escapeHtml(agent.runtime_instance_id || "-")}</dd></div>
            <div><dt>Reachability</dt><dd>${escapeHtml(agent.reachability || "-")}</dd></div>
            <div><dt>Reason</dt><dd>${escapeHtml(agent.confidence_reason || "-")}</dd></div>
            <div><dt>Confidence Detail</dt><dd>${escapeHtml(agent.confidence_detail || "-")}</dd></div>
            <div><dt>Queue</dt><dd>${escapeHtml(agent.backlog_depth || 0)}</dd></div>
            <div><dt>Seen</dt><dd>${escapeHtml(formatAge(agent.last_seen_age_seconds))}</dd></div>
            <div><dt>Phase</dt><dd>${escapeHtml(agent.current_status || "-")}</dd></div>
            <div><dt>Activity</dt><dd>${escapeHtml(agent.current_activity || "-")}</dd></div>
            <div><dt>Processed</dt><dd>${escapeHtml(agent.processed_count || 0)}</dd></div>
            <div class="copyable-block">
              <dt>Last Reply</dt>
              <dd><pre>${lastReply}</pre></dd>
              <button type="button" class="ghost" data-copy-text="${lastReplyCopy}">Copy</button>
            </div>
            <div><dt>Last Error</dt><dd>${escapeHtml(agent.last_error || "-")}</dd></div>
            <div><dt>Doctor</dt><dd>${escapeHtml(agent.last_successful_doctor_at || "-")}</dd></div>
            <div><dt>Doctor Result</dt><dd>${escapeHtml(agent.last_doctor_result?.status || "-")}</dd></div>
            <div><dt>Effective</dt><dd>${escapeHtml(agent.effective_state || "-")}</dd></div>
            <div><dt>Workdir</dt><dd>${escapeHtml(agent.workdir || "-")}</dd></div>
            <div><dt>Exec</dt><dd>${escapeHtml(agent.exec_command || "-")}</dd></div>
          </dl>
          <div>
            <div class="panel-header" style="padding:0 0 12px;"><span>Recent Agent Activity</span></div>
            ${
              events.length
                ? `<div class="event-list">${
                    events.map((item) => `
                      <div class="event-item">
                        <div class="event-head">
                          <span>${escapeHtml(item.event || "-")}</span>
                          <span>${escapeHtml(formatAge(item.ts ? Math.max(0, ((Date.now() - Date.parse(item.ts)) / 1000)) : null))}</span>
                        </div>
                        <div class="event-detail">${escapeHtml(detailText(item))}</div>
                      </div>
                    `).join("")
                  }</div>`
                : `<div class="empty">No recent agent activity yet.</div>`
            }
          </div>
        </div>
      `;
    }

    async function loadStatus() {
      const payload = await apiRequest("/api/status");
      renderOverview(payload);
      renderMetrics(payload);
      renderAlerts(payload);
      renderAgents(payload);
      renderActivity(payload);
      if (!selectedAgent && payload.agents?.length) {
        selectedAgent = payload.agents[0].name;
      }
      if (selectedAgent) {
        await loadAgentDetail(selectedAgent);
      } else {
        renderAgentDetail(null);
      }
    }

    async function loadAgentDetail(name) {
      try {
        const payload = await apiRequest(`/api/agents/${encodeURIComponent(name)}`);
        renderAgentDetail(payload);
      } catch {
        renderAgentDetail(null);
      }
    }

    async function tick(force = false) {
      if (!force && autoRefreshPaused) {
        return;
      }
      const selection = window.getSelection ? String(window.getSelection() || "") : "";
      if (!force && selection.trim()) {
        return;
      }
      const active = document.activeElement;
      if (!force && active && ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName)) {
        return;
      }
      try {
        await loadStatus();
      } catch (error) {
        document.getElementById("activity-feed").innerHTML = `<div class="empty">Gateway UI lost contact with the local status API: ${escapeHtml(error.message || error)}</div>`;
      }
    }

    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-agent-name]");
      if (!button) return;
      if (button.hasAttribute("data-agent-action")) return;
      selectedAgent = button.getAttribute("data-agent-name");
      tick();
    });

    document.addEventListener("click", async (event) => {
      const copyButton = event.target.closest("[data-copy-text]");
      if (copyButton) {
        const text = decodeURIComponent(copyButton.getAttribute("data-copy-text") || "");
        try {
          await navigator.clipboard.writeText(text);
          setFlash("detail-flash", "Copied last reply.", "success");
        } catch {
          setFlash("detail-flash", "Could not copy to clipboard.", "warning");
        }
        return;
      }
      const button = event.target.closest("[data-agent-action]");
      if (!button) return;
      const action = button.getAttribute("data-agent-action");
      const agentName = button.getAttribute("data-agent-name");
      try {
        if (action === "edit") {
          await loadAgentIntoSetupForm(agentName);
        } else if (action === "remove") {
          await apiRequest(`/api/agents/${encodeURIComponent(agentName)}`, { method: "DELETE" });
          selectedAgent = null;
        } else if (action === "doctor") {
          const result = await apiRequest(`/api/agents/${encodeURIComponent(agentName)}/doctor`, { method: "POST", body: "{}" });
          selectedAgent = agentName;
          setFlash("detail-flash", `Doctor ${result.status} for @${agentName}`, result.status === "failed" ? "error" : (result.status === "warning" ? "warning" : "success"));
        } else if (action === "test") {
          const result = await apiRequest(`/api/agents/${encodeURIComponent(agentName)}/test`, { method: "POST", body: "{}" });
          selectedAgent = agentName;
          setFlash("detail-flash", `Test sent to @${result.target_agent}`, "success");
        } else {
          await apiRequest(`/api/agents/${encodeURIComponent(agentName)}/${action}`, { method: "POST", body: "{}" });
          selectedAgent = agentName;
          setFlash("detail-flash", `${action} requested for @${agentName}`, "success");
        }
        await tick(true);
      } catch (error) {
        setFlash("detail-flash", error.message || String(error), "error");
      }
    });

    document.getElementById("refresh-toggle").addEventListener("click", () => {
      autoRefreshPaused = !autoRefreshPaused;
      refreshButtonLabel();
      if (!autoRefreshPaused) {
        tick(true);
      }
    });

    document.getElementById("agent-type").addEventListener("change", (event) => {
      renderTemplateHelp(event.target.value);
    });

    document.getElementById("add-agent-cancel").addEventListener("click", () => {
      resetSetupForm();
      setFlash("add-agent-flash", "Setup form reset.", "warning");
    });

    document.getElementById("add-agent-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const data = new FormData(form);
      const payload = {
        name: String(data.get("name") || "").trim(),
        template_id: String(data.get("template_id") || "echo_test"),
        exec_command: String(data.get("exec_command") || "").trim(),
        workdir: String(data.get("workdir") || "").trim(),
        ollama_model: String(data.get("ollama_model") || "").trim(),
        start: true,
      };
      try {
        const updateMode = setupMode === "update" && setupTarget;
        const result = await apiRequest(
          updateMode ? `/api/agents/${encodeURIComponent(setupTarget)}` : "/api/agents",
          {
            method: updateMode ? "PUT" : "POST",
            body: JSON.stringify(payload),
          },
        );
        setFlash("add-agent-flash", `${updateMode ? "Updated" : "Added"} @${result.name}`, "success");
        selectedAgent = result.name;
        resetSetupForm();
        await tick();
      } catch (error) {
        setFlash("add-agent-flash", error.message || String(error), "error");
      }
    });

    document.getElementById("send-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!selectedAgent) {
        setFlash("send-flash", "Select a managed agent first.", "error");
        return;
      }
      const form = event.currentTarget;
      const data = new FormData(form);
      const payload = {
        to: String(data.get("to") || "").trim(),
        parent_id: String(data.get("parent_id") || "").trim(),
        content: String(data.get("content") || "").trim(),
      };
      try {
        const result = await apiRequest(`/api/agents/${encodeURIComponent(selectedAgent)}/send`, {
          method: "POST",
          body: JSON.stringify(payload),
        });
        setFlash("send-flash", `Sent as @${result.agent}`, "success");
        form.content.value = "";
        await tick(true);
      } catch (error) {
        setFlash("send-flash", error.message || String(error), "error");
      }
    });

    async function boot() {
      try {
        await loadTemplates();
      } catch (error) {
        setFlash("add-agent-flash", error.message || String(error), "error");
      }
      applySetupMode();
      refreshButtonLabel();
      await tick(true);
      window.setInterval(tick, refreshMs);
    }

    boot();
  </script>
</body>
</html>
"""
    return template.replace("__REFRESH_MS__", str(refresh_ms))


_DEMO_HTML_PATH = Path(__file__).resolve().parent.parent / "static" / "demo.html"

# Brand-mark favicon. Same connected-agent node mark as the topbar brand chip
# so the browser tab matches what users see on the page.
# Inline so no separate static asset is needed; served at /favicon.svg.
_GATEWAY_FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#5eead4"/>
      <stop offset="100%" stop-color="#7dd3fc"/>
    </linearGradient>
  </defs>
  <rect width="40" height="40" rx="13" fill="url(#g)"/>
  <path d="M12 12 20 20 12 28M20 20h8" stroke="#062018" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" opacity="0.7"/>
  <circle cx="12" cy="12" r="4.1" fill="#062018"/>
  <circle cx="12" cy="28" r="4.1" fill="#062018"/>
  <circle cx="28" cy="20" r="4.1" fill="#062018"/>
  <circle cx="20" cy="20" r="5.2" fill="#062018"/>
  <circle cx="12" cy="12" r="1.25" fill="#a7fff1"/>
  <circle cx="12" cy="28" r="1.25" fill="#a7fff1"/>
  <circle cx="28" cy="20" r="1.25" fill="#a7fff1"/>
  <circle cx="20" cy="20" r="1.8" fill="#a7fff1"/>
</svg>
""".strip()


def _render_gateway_demo_page(*, refresh_ms: int) -> str:
    body = _DEMO_HTML_PATH.read_text(encoding="utf-8")
    inject = f"<script>window.__GATEWAY_DEMO_REFRESH_MS__ = {int(refresh_ms)};</script></head>"
    return body.replace("</head>", inject, 1)


class _GatewayUiServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _write_json_response(handler: BaseHTTPRequestHandler, payload: dict, *, status: HTTPStatus = HTTPStatus.OK) -> None:
    body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    handler.send_response(status.value)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _write_html_response(handler: BaseHTTPRequestHandler, payload: str) -> None:
    body = payload.encode("utf-8")
    handler.send_response(HTTPStatus.OK.value)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_request(handler: BaseHTTPRequestHandler) -> dict:
    content_length = int(handler.headers.get("Content-Length", "0") or 0)
    if content_length <= 0:
        return {}
    raw = handler.rfile.read(content_length)
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object.")
    return payload


def _build_gateway_ui_handler(*, activity_limit: int, refresh_ms: int):
    class GatewayUiHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                _write_html_response(self, _render_gateway_demo_page(refresh_ms=refresh_ms))
                return
            if parsed.path == "/operator":
                _write_html_response(self, _render_gateway_ui_page(refresh_ms=refresh_ms))
                return
            if parsed.path == "/demo":
                _write_html_response(self, _render_gateway_demo_page(refresh_ms=refresh_ms))
                return
            if parsed.path == "/healthz":
                _write_json_response(self, {"ok": True})
                return
            if parsed.path == "/favicon.svg" or parsed.path == "/favicon.ico":
                body = _GATEWAY_FAVICON_SVG.encode("utf-8")
                self.send_response(HTTPStatus.OK.value)
                self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/status":
                _write_json_response(self, _status_payload(activity_limit=activity_limit))
                return
            if parsed.path == "/local/inbox":
                query = parse_qs(parsed.query)
                session_token = str(self.headers.get("X-Gateway-Session") or "").strip()
                limit = int((query.get("limit") or ["20"])[0] or 20)
                channel = str((query.get("channel") or ["main"])[0] or "main")
                space_id = str((query.get("space_id") or [""])[0] or "").strip() or None
                unread_only = str((query.get("unread_only") or ["true"])[0]).lower() not in {"0", "false", "no"}
                mark_read = str((query.get("mark_read") or ["true"])[0]).lower() not in {"0", "false", "no"}
                payload = _local_session_inbox(
                    session_token=session_token,
                    limit=limit,
                    channel=channel,
                    space_id=space_id,
                    unread_only=unread_only,
                    mark_read=mark_read,
                )
                _write_json_response(self, payload)
                return
            if parsed.path == "/local/sessions":
                registry = load_gateway_registry()
                sessions = list(registry.get("local_sessions") or [])
                _write_json_response(self, {"sessions": sessions, "count": len(sessions)})
                return
            if parsed.path == "/api/runtime-types":
                _write_json_response(self, _runtime_types_payload())
                return
            if parsed.path == "/api/templates":
                _write_json_response(self, _agent_templates_payload())
                return
            if parsed.path == "/api/approvals":
                query = parse_qs(parsed.query)
                status_filter = (query.get("status") or [None])[0]
                _write_json_response(self, _approval_rows_payload(status=status_filter))
                return
            if parsed.path.startswith("/api/approvals/"):
                approval_id = unquote(parsed.path.removeprefix("/api/approvals/")).strip()
                try:
                    _write_json_response(self, _approval_detail_payload(approval_id))
                except LookupError as exc:
                    _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
                return
            if parsed.path == "/api/spaces":
                try:
                    _write_json_response(self, _spaces_payload())
                except typer.Exit:
                    _write_json_response(
                        self,
                        {"error": "Gateway is not logged in.", "spaces": [], "active_space_id": None},
                        status=HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                except Exception as exc:  # noqa: BLE001
                    _write_json_response(
                        self,
                        {"error": str(exc), "spaces": [], "active_space_id": None},
                        status=HTTPStatus.BAD_GATEWAY,
                    )
                return
            if parsed.path.startswith("/api/agents/"):
                name = unquote(parsed.path.removeprefix("/api/agents/")).strip()
                payload = _agent_detail_payload(name, activity_limit=activity_limit)
                if payload is None:
                    _write_json_response(
                        self,
                        {"error": f"Managed agent not found: {name}"},
                        status=HTTPStatus.NOT_FOUND,
                    )
                    return
                _write_json_response(self, payload)
                return
            _write_json_response(self, {"error": "not found"}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                body = _read_json_request(self)
                if parsed.path.startswith("/api/templates/") and parsed.path.endswith("/install"):
                    template_id = (
                        unquote(parsed.path.removeprefix("/api/templates/").removesuffix("/install")).strip().lower()
                    )
                    if template_id not in _RUNTIME_INSTALL_RECIPES:
                        _write_json_response(
                            self,
                            {"error": f"runtime not on install allowlist: {template_id!r}"},
                            status=HTTPStatus.BAD_REQUEST,
                        )
                        return
                    operator_session = load_gateway_session()
                    if not operator_session:
                        _write_json_response(
                            self,
                            {
                                "error": "install requires an active gateway operator session — run `ax gateway login` first"
                            },
                            status=HTTPStatus.FORBIDDEN,
                        )
                        return
                    target_override = str(body.get("target") or "").strip() or None
                    try:
                        payload = _install_runtime_payload(
                            template_id,
                            target_override=target_override,
                            operator_session=operator_session,
                        )
                    except PermissionError as exc:
                        _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
                        return
                    except ValueError as exc:
                        _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                        return
                    status_code = HTTPStatus.OK if payload.get("ready") else HTTPStatus.UNPROCESSABLE_ENTITY
                    _write_json_response(self, payload, status=status_code)
                    return
                if parsed.path == "/api/agents":
                    payload = _register_managed_agent(
                        name=str(body.get("name") or "").strip(),
                        template_id=str(body.get("template_id") or "").strip() or None,
                        runtime_type=str(body.get("runtime_type") or "").strip() or None,
                        exec_cmd=str(body.get("exec_command") or "").strip() or None,
                        workdir=str(body.get("workdir") or "").strip() or None,
                        ollama_model=str(body.get("ollama_model") or "").strip() or None,
                        space_id=str(body.get("space_id") or "").strip() or None,
                        audience=str(body.get("audience") or "both"),
                        description=str(body.get("description") or "").strip() or None,
                        model=str(body.get("model") or "").strip() or None,
                        timeout_seconds=body.get("timeout_seconds", body.get("timeout")),
                        start=bool(body.get("start", True)),
                    )
                    profile = gateway_core.infer_operator_profile(payload)
                    if (
                        profile["placement"] == "attached"
                        and profile["activation"] == "attach_only"
                        and str(payload.get("desired_state") or "").strip().lower() == "running"
                    ):
                        launch_payload = _launch_attached_agent_session(
                            _prepare_attached_agent_payload(payload["name"])
                        )
                        record_gateway_activity(
                            "attached_session_launch_requested",
                            agent_name=payload["name"],
                            launch_mode=launch_payload.get("launch_mode"),
                            workdir=str(Path(str(launch_payload["mcp_path"])).parent),
                        )
                        registry = load_gateway_registry()
                        stored = find_agent_entry(registry, str(payload["name"]))
                        if stored:
                            payload = _with_registry_refs(registry, annotate_runtime_health(stored, registry=registry))
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                if parsed.path == "/local/connect":
                    agent_name = str(body.get("agent_name") or body.get("name") or "").strip()
                    registry_ref = str(
                        body.get("registry_ref") or body.get("registry") or body.get("ref") or ""
                    ).strip()
                    fingerprint = body.get("fingerprint") if isinstance(body.get("fingerprint"), dict) else {}
                    payload = _connect_local_pass_through_agent(
                        agent_name=agent_name or None,
                        registry_ref=registry_ref or None,
                        fingerprint=fingerprint,
                        space_id=str(body.get("space_id") or "").strip() or None,
                    )
                    status = HTTPStatus.OK if payload.get("status") == "approved" else HTTPStatus.ACCEPTED
                    _write_json_response(self, payload, status=status)
                    return
                if parsed.path == "/local/send":
                    session_token = str(self.headers.get("X-Gateway-Session") or "").strip()
                    payload = _send_local_session_message(session_token=session_token, body=body)
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                if parsed.path.endswith("/start") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/start")).strip()
                    payload = _set_managed_agent_desired_state(name, "running")
                    _write_json_response(self, payload)
                    return
                if parsed.path.endswith("/stop") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/stop")).strip()
                    payload = _set_managed_agent_desired_state(name, "stopped")
                    _write_json_response(self, payload)
                    return
                if parsed.path.endswith("/attach") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/attach")).strip()
                    payload = _launch_attached_agent_session(_prepare_attached_agent_payload(name))
                    record_gateway_activity(
                        "attached_session_launch_requested",
                        agent_name=name,
                        launch_mode=payload.get("launch_mode"),
                        workdir=str(Path(str(payload["mcp_path"])).parent),
                    )
                    _write_json_response(self, payload, status=HTTPStatus.ACCEPTED)
                    return
                if parsed.path.endswith("/send") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/send")).strip()
                    payload = _send_from_managed_agent(
                        name=name,
                        content=str(body.get("content") or ""),
                        to=str(body.get("to") or "").strip() or None,
                        parent_id=str(body.get("parent_id") or "").strip() or None,
                    )
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                if parsed.path.endswith("/test") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/test")).strip()
                    payload = _send_gateway_test_to_managed_agent(
                        name,
                        content=str(body.get("content") or "").strip() or None,
                        author=str(body.get("author") or "agent").strip() or "agent",
                        sender_agent=str(body.get("sender_agent") or "").strip() or None,
                        allow_self_fallback=bool(body.get("allow_self_fallback", True)),
                    )
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                if parsed.path.endswith("/ack") and parsed.path.startswith("/api/agents/"):
                    # Pass-through agents that reply via their own PAT (not via
                    # gateway-mediated send) call this to tell the gateway "I
                    # processed message_id, here's my reply_id." Updates the
                    # registry's last_reply_at + processed_count, drops the
                    # message from the local pending queue, fires a reply_sent
                    # activity event so the simple-gateway drawer surfaces it.
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/ack")).strip()
                    payload = _ack_managed_agent_message(
                        name,
                        message_id=str(body.get("message_id") or "").strip(),
                        reply_id=str(body.get("reply_id") or "").strip() or None,
                        reply_preview=str(body.get("reply_preview") or "").strip() or None,
                    )
                    _write_json_response(self, payload)
                    return
                if parsed.path.endswith("/move") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/move")).strip()
                    payload = _move_managed_agent_space(
                        name,
                        str(body.get("space_id") or "").strip(),
                    )
                    _write_json_response(self, payload)
                    return
                if parsed.path.endswith("/pin") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/pin")).strip()
                    payload = _set_managed_agent_pin(name, bool(body.get("pinned", True)))
                    _write_json_response(self, payload)
                    return
                if parsed.path.endswith("/doctor") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/doctor")).strip()
                    payload = _run_gateway_doctor(
                        name,
                        send_test=bool(body.get("send_test", False)),
                    )
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                if parsed.path.endswith("/approve") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/approve")).strip()
                    detail = _agent_detail_payload(name, activity_limit=activity_limit)
                    if detail is None:
                        _write_json_response(
                            self,
                            {"error": f"Managed agent not found: {name}"},
                            status=HTTPStatus.NOT_FOUND,
                        )
                        return
                    approval_id = str((detail.get("agent") or {}).get("approval_id") or "").strip()
                    if not approval_id:
                        _write_json_response(
                            self,
                            {"error": f"@{name} does not have a pending Gateway approval."},
                            status=HTTPStatus.BAD_REQUEST,
                        )
                        return
                    payload = approve_gateway_approval(
                        approval_id,
                        scope=str(body.get("scope") or "asset").strip() or "asset",
                    )
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                if parsed.path.endswith("/approve") and parsed.path.startswith("/api/approvals/"):
                    approval_id = unquote(parsed.path.removeprefix("/api/approvals/").removesuffix("/approve")).strip()
                    payload = approve_gateway_approval(
                        approval_id,
                        scope=str(body.get("scope") or "asset").strip() or "asset",
                    )
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                if parsed.path.endswith("/reject") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/reject")).strip()
                    payload = _reject_managed_agent_approval(name)
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                if parsed.path.endswith("/reject") and parsed.path.startswith("/api/approvals/"):
                    approval_id = unquote(parsed.path.removeprefix("/api/approvals/").removesuffix("/reject")).strip()
                    payload = deny_gateway_approval(approval_id)
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                _write_json_response(self, {"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            except LookupError as exc:
                _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except typer.Exit as exc:
                status = HTTPStatus.BAD_REQUEST if int(exc.exit_code or 1) == 1 else HTTPStatus.OK
                _write_json_response(self, {"error": "request failed"}, status=status)
            except Exception as exc:
                _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_PUT(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                body = _read_json_request(self)
                if parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/")).strip()
                    payload = _update_managed_agent(
                        name=name,
                        template_id=str(body.get("template_id") or "").strip() or None,
                        runtime_type=str(body.get("runtime_type") or "").strip() or None,
                        exec_cmd=str(body.get("exec_command") or "") if "exec_command" in body else _UNSET,
                        workdir=str(body.get("workdir") or "") if "workdir" in body else _UNSET,
                        ollama_model=str(body.get("ollama_model") or "") if "ollama_model" in body else _UNSET,
                        description=str(body.get("description") or "").strip() or None,
                        model=str(body.get("model") or "").strip() or None,
                        timeout_seconds=body.get("timeout_seconds", body.get("timeout"))
                        if "timeout_seconds" in body or "timeout" in body
                        else _UNSET,
                        desired_state=str(body.get("desired_state") or "").strip() or None,
                    )
                    _write_json_response(self, payload)
                    return
                _write_json_response(self, {"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            except LookupError as exc:
                _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except typer.Exit as exc:
                status = HTTPStatus.BAD_REQUEST if int(exc.exit_code or 1) == 1 else HTTPStatus.OK
                _write_json_response(self, {"error": "request failed"}, status=status)
            except Exception as exc:
                _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_DELETE(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/agents/"):
                name = unquote(parsed.path.removeprefix("/api/agents/")).strip()
                try:
                    payload = _remove_managed_agent(name)
                    _write_json_response(self, payload)
                except LookupError as exc:
                    _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
                return
            _write_json_response(self, {"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    return GatewayUiHandler


def _render_agent_detail(entry: dict, *, activity: list[dict]) -> Group:
    overview = Table.grid(expand=True, padding=(0, 2))
    overview.add_column(style="bold")
    overview.add_column(ratio=2)
    overview.add_column(style="bold")
    overview.add_column(ratio=2)
    overview.add_row("Agent", f"@{entry.get('name')}", "Type", _agent_type_label(entry))
    overview.add_row("Template", _agent_template_label(entry), "Output", _agent_output_label(entry))
    overview.add_row("Mode", str(entry.get("mode") or "-"), "Presence", str(entry.get("presence") or "-"))
    overview.add_row("Reply", str(entry.get("reply") or "-"), "Confidence", str(entry.get("confidence") or "-"))
    overview.add_row(
        "Asset Class", str(entry.get("asset_class") or "-"), "Intake", str(entry.get("intake_model") or "-")
    )
    overview.add_row(
        "Trigger",
        str((entry.get("trigger_sources") or [None])[0] or "-"),
        "Return",
        str((entry.get("return_paths") or [None])[0] or "-"),
    )
    overview.add_row(
        "Telemetry", str(entry.get("telemetry_shape") or "-"), "Worker", str(entry.get("worker_model") or "-")
    )
    overview.add_row(
        "Attestation", str(entry.get("attestation_state") or "-"), "Approval", str(entry.get("approval_state") or "-")
    )
    overview.add_row(
        "Acting As", str(entry.get("acting_agent_name") or "-"), "Identity", str(entry.get("identity_status") or "-")
    )
    overview.add_row(
        "Environment",
        str(entry.get("environment_label") or entry.get("base_url") or "-"),
        "Env Status",
        str(entry.get("environment_status") or "-"),
    )
    overview.add_row(
        "Current Space",
        str(entry.get("active_space_name") or entry.get("active_space_id") or "-"),
        "Space Status",
        str(entry.get("space_status") or "-"),
    )
    overview.add_row(
        "Default Space",
        str(entry.get("default_space_name") or entry.get("default_space_id") or "-"),
        "Allowed Spaces",
        str(entry.get("allowed_space_count") or 0),
    )
    overview.add_row(
        "Install", str(entry.get("install_id") or "-"), "Runtime Instance", str(entry.get("runtime_instance_id") or "-")
    )
    overview.add_row("Reachability", _reachability_copy(entry), "Reason", str(entry.get("confidence_reason") or "-"))
    overview.add_row(
        "Desired", str(entry.get("desired_state") or "-"), "Effective", str(entry.get("effective_state") or "-")
    )
    overview.add_row(
        "Connected", "yes" if entry.get("connected") else "no", "Queue", str(entry.get("backlog_depth") or 0)
    )
    overview.add_row(
        "Seen",
        _format_age(entry.get("last_seen_age_seconds")),
        "Reconnect",
        _format_age(entry.get("reconnect_backoff_seconds")),
    )
    overview.add_row(
        "Processed", str(entry.get("processed_count") or 0), "Dropped", str(entry.get("dropped_count") or 0)
    )
    overview.add_row(
        "Last Work",
        _format_timestamp(entry.get("last_work_received_at")),
        "Completed",
        _format_timestamp(entry.get("last_work_completed_at")),
    )
    overview.add_row(
        "Phase", str(entry.get("current_status") or "-"), "Activity", str(entry.get("current_activity") or "-")
    )
    overview.add_row(
        "Tool",
        str(entry.get("current_tool") or "-"),
        "Timeout",
        f"{entry.get('timeout_seconds')}s" if entry.get("timeout_seconds") else "-",
    )
    overview.add_row("Adapter", str(entry.get("runtime_type") or "-"), "Space", str(entry.get("space_id") or "-"))
    overview.add_row(
        "Cred Source", str(entry.get("credential_source") or "-"), "Token", str(entry.get("token_file") or "-")
    )
    overview.add_row(
        "Agent ID", str(entry.get("agent_id") or "-"), "Last Reply", str(entry.get("last_reply_preview") or "-")
    )
    overview.add_row(
        "Last Error",
        str(entry.get("last_error") or "-"),
        "Confidence Detail",
        str(entry.get("confidence_detail") or "-"),
    )
    overview.add_row(
        "Doctor",
        str(entry.get("last_successful_doctor_at") or "-"),
        "Doctor Status",
        str(
            (entry.get("last_doctor_result") or {}).get("status")
            if isinstance(entry.get("last_doctor_result"), dict)
            else "-"
        ),
    )

    paths = Table.grid(expand=True, padding=(0, 2))
    paths.add_column(style="bold")
    paths.add_column(ratio=3)
    paths.add_row("Token File", str(entry.get("token_file") or "-"))
    paths.add_row("Workdir", str(entry.get("workdir") or "-"))
    paths.add_row("Exec", str(entry.get("exec_command") or "-"))
    paths.add_row("Added", _format_timestamp(entry.get("added_at")))

    return Group(
        Panel(overview, title=f"Managed Agent · @{entry.get('name')}", border_style="cyan"),
        Panel(paths, title="Runtime Details", border_style="blue"),
        Panel(_render_activity_table(activity), title="Recent Agent Activity", border_style="magenta"),
    )


@app.command("login")
def login(
    token: str = typer.Option(
        None, "--token", "-t", help="User PAT (prompted or reused from axctl login when omitted)"
    ),
    base_url: str = typer.Option(
        None, "--url", "-u", help="API base URL (defaults to existing axctl login or paxai.app)"
    ),
    space_id: str = typer.Option(None, "--space-id", "-s", help="Optional default space for managed agents"),
    as_json: bool = JSON_OPTION,
):
    """Store the Gateway bootstrap session.

    The Gateway keeps the user PAT centrally and uses it to mint agent PATs for
    managed runtimes. Managed runtimes themselves never receive the PAT or JWT.
    """
    resolved_token = _resolve_gateway_login_token(token)
    if not resolved_token.startswith("axp_u_"):
        err_console.print("[red]Gateway bootstrap requires a user PAT (axp_u_).[/red]")
        raise typer.Exit(1)
    resolved_base_url = base_url or resolve_user_base_url() or auth_cmd.DEFAULT_LOGIN_BASE_URL

    err_console.print(f"[cyan]Verifying Gateway login against {resolved_base_url}...[/cyan]")
    from ..token_cache import TokenExchanger

    try:
        exchanger = TokenExchanger(resolved_base_url, resolved_token)
        exchanger.get_token(
            "user_access",
            scope="messages tasks context agents spaces search",
            force_refresh=True,
        )
        client = AxClient(base_url=resolved_base_url, token=resolved_token)
        me = client.whoami()
    except Exception as exc:
        err_console.print(f"[red]Gateway login failed:[/red] {exc}")
        raise typer.Exit(1)

    selected_space = space_id
    selected_space_name = None
    if not selected_space:
        try:
            spaces = client.list_spaces()
            space_list = spaces.get("spaces", spaces) if isinstance(spaces, dict) else spaces
            selected = auth_cmd._select_login_space([s for s in space_list if isinstance(s, dict)])
            if selected:
                selected_space = auth_cmd._candidate_space_id(selected)
                selected_space_name = str(selected.get("name") or selected_space)
        except Exception:
            selected_space = None
    elif selected_space:
        try:
            selected_space = resolve_space_id(client, explicit=selected_space)
            spaces = client.list_spaces()
            space_list = spaces.get("spaces", spaces) if isinstance(spaces, dict) else spaces
            selected_space_name = next(
                (
                    str(item.get("name") or selected_space)
                    for item in space_list
                    if isinstance(item, dict) and auth_cmd._candidate_space_id(item) == selected_space
                ),
                None,
            )
        except Exception:
            selected_space_name = None

    payload = {
        "token": resolved_token,
        "base_url": resolved_base_url,
        "principal_type": "user",
        "space_id": selected_space,
        "space_name": selected_space_name,
        "username": me.get("username"),
        "email": me.get("email"),
        "saved_at": None,
    }
    path = save_gateway_session(payload)
    registry = load_gateway_registry()
    registry.setdefault("gateway", {})
    registry["gateway"]["session_connected"] = True
    save_gateway_registry(registry)
    record_gateway_activity(
        "gateway_login", username=me.get("username"), base_url=resolved_base_url, space_id=selected_space
    )

    result = {
        "session_path": str(path),
        "base_url": resolved_base_url,
        "space_id": selected_space,
        "space_name": selected_space_name,
        "username": me.get("username"),
        "email": me.get("email"),
    }
    if as_json:
        print_json(result)
    else:
        err_console.print(f"[green]Gateway login saved:[/green] {path}")
        for key, value in result.items():
            err_console.print(f"  {key} = {value}")


@spaces_app.command("use")
def use_gateway_space(
    space: str = typer.Argument(..., help="Space id, slug, or name to make current for Gateway"),
    as_json: bool = JSON_OPTION,
):
    """Set the Gateway bootstrap session's current space by id, slug, or name."""
    session = _load_gateway_session_or_exit()
    client = _load_gateway_user_client()
    sid = resolve_space_id(client, explicit=space)
    space_name = _space_name_for_id(client, sid)
    session["space_id"] = sid
    session["space_name"] = space_name
    path = save_gateway_session(session)
    registry = load_gateway_registry()
    registry.setdefault("gateway", {})
    registry["gateway"]["space_id"] = sid
    registry["gateway"]["space_name"] = space_name
    save_gateway_registry(registry)
    record_gateway_activity("gateway_space_use", space_id=sid, space_name=space_name)
    result = {
        "session_path": str(path),
        "space_id": sid,
        "space_name": space_name,
    }
    if as_json:
        print_json(result)
        return
    err_console.print(f"[green]Gateway current space:[/green] {space_name or sid} ({sid})")
    err_console.print(f"  session = {path}")


@spaces_app.command("current")
def current_gateway_space(as_json: bool = JSON_OPTION):
    """Show the Gateway bootstrap session's current space."""
    session = _load_gateway_session_or_exit()
    result = {
        "space_id": session.get("space_id"),
        "space_name": session.get("space_name"),
        "base_url": session.get("base_url"),
        "username": session.get("username"),
    }
    if as_json:
        print_json(result)
        return
    err_console.print(f"Gateway current space: {result.get('space_name') or result.get('space_id') or '-'}")
    err_console.print(f"  space_id = {result.get('space_id') or '-'}")


@app.command("status")
def status(as_json: bool = JSON_OPTION):
    """Show Gateway status, daemon state, and managed runtimes."""
    payload = _status_payload()
    if as_json:
        print_json(payload)
        return

    err_console.print("[bold]ax gateway status[/bold]")
    err_console.print(f"  gateway_dir = {payload['gateway_dir']}")
    err_console.print(f"  connected   = {payload['connected']}")
    err_console.print(f"  daemon      = {'running' if payload['daemon']['running'] else 'stopped'}")
    if payload["daemon"]["pid"]:
        err_console.print(f"  pid         = {payload['daemon']['pid']}")
    err_console.print(f"  ui          = {'running' if payload['ui']['running'] else 'stopped'}")
    if payload["ui"]["pid"]:
        err_console.print(f"  ui_pid      = {payload['ui']['pid']}")
    err_console.print(f"  ui_url      = {payload['ui']['url']}")
    err_console.print(f"  base_url    = {payload['base_url']}")
    err_console.print(f"  space_id    = {payload['space_id']}")
    if payload.get("space_name"):
        err_console.print(f"  space_name  = {payload['space_name']}")
    err_console.print(f"  user        = {payload['user']}")
    err_console.print(f"  agents      = {payload['summary']['managed_agents']}")
    err_console.print(f"  live        = {payload['summary']['live_agents']}")
    err_console.print(f"  on_demand   = {payload['summary']['on_demand_agents']}")
    err_console.print(f"  inbox       = {payload['summary']['inbox_agents']}")
    err_console.print(f"  alerts      = {payload['summary'].get('alert_count', 0)}")
    err_console.print(f"  approvals   = {payload['summary'].get('pending_approvals', 0)} pending")
    if payload.get("alerts"):
        print_table(
            ["Level", "Alert", "Agent", "Detail"],
            payload["alerts"],
            keys=["severity", "title", "agent_name", "detail"],
        )
    if payload["agents"]:
        print_table(
            [
                "Agent",
                "Type",
                "Mode",
                "Presence",
                "Output",
                "Confidence",
                "Acting As",
                "Current Space",
                "Seen",
                "Backlog",
                "Reason",
            ],
            [
                {**agent, "type": _agent_type_label(agent), "output": _agent_output_label(agent)}
                for agent in payload["agents"]
            ],
            keys=[
                "name",
                "type",
                "mode",
                "presence",
                "output",
                "confidence",
                "acting_agent_name",
                "active_space_name",
                "last_seen_age_seconds",
                "backlog_depth",
                "confidence_reason",
            ],
        )
    if payload["recent_activity"]:
        print_table(
            ["Time", "Event", "Agent", "Message", "Preview"],
            payload["recent_activity"],
            keys=["ts", "event", "agent_name", "message_id", "reply_preview"],
        )


@runtime_app.command("install")
def runtime_install(
    template_id: str = typer.Argument(..., help="Runtime template id (today: only 'hermes')"),
    target: str = typer.Option(None, "--target", help="Override install target (must resolve under your home tree)"),
    as_json: bool = JSON_OPTION,
):
    """Install a runtime template's prerequisites (clone + venv + pip install + verify).

    Today only ``hermes`` is on the install allowlist (clones from
    https://github.com/NousResearch/hermes-agent into ~/hermes-agent and
    installs into a venv at ~/hermes-agent/.venv). Other templates require
    a code-reviewable PR to extend the allowlist per AUTOSETUP-001 §Security.

    Requires an active gateway operator session — run ``ax gateway login`` first.

        ax gateway runtime install hermes
        ax gateway runtime install hermes --target /opt/work/hermes-agent
    """
    operator_session = load_gateway_session()
    if not operator_session:
        err_console.print("[red]No active gateway session.[/red] Run `ax gateway login` first.")
        raise typer.Exit(1)
    try:
        payload = _install_runtime_payload(template_id, target_override=target, operator_session=operator_session)
    except (ValueError, PermissionError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if as_json:
        print_json(payload)
        return
    err_console.print(f"[bold]ax gateway runtime install {template_id}[/bold]")
    err_console.print(f"  target = {payload.get('target')}")
    for step in payload.get("steps", []):
        marker = {
            "ok": "[green]✓[/green]",
            "skipped": "[dim]·[/dim]",
            "running": "[cyan]…[/cyan]",
            "warn": "[yellow]![/yellow]",
            "error": "[red]✗[/red]",
        }.get(step.get("status", ""), "?")
        detail = (step.get("detail") or "")[:160]
        err_console.print(f"  {marker} {step.get('step')}: {detail}")
    state = "[green]ready[/green]" if payload.get("ready") else "[red]not ready[/red]"
    err_console.print(f"  state = {state}")
    if not payload.get("ready"):
        raise typer.Exit(1)


@runtime_app.command("status")
def runtime_status(
    template_id: str = typer.Argument(..., help="Runtime template id (today: only 'hermes')"),
    as_json: bool = JSON_OPTION,
):
    """Report whether a runtime template is ready (preflight check).

    Calls the same preflight backing the wizard's ``hermes_ready`` flag.
    Useful as an automation gate: ``ax gateway runtime status hermes`` exits
    non-zero when not ready.
    """
    if template_id.strip().lower() not in _RUNTIME_INSTALL_RECIPES:
        err_console.print(f"[red]unknown runtime template:[/red] {template_id!r}")
        raise typer.Exit(1)
    from ..gateway import hermes_setup_status

    status = hermes_setup_status({"template_id": template_id.strip().lower()})
    if as_json:
        print_json(status)
        return
    state = "[green]ready[/green]" if status.get("ready") else "[red]not ready[/red]"
    err_console.print(f"[bold]{template_id}[/bold] {state}")
    if status.get("resolved_path"):
        err_console.print(f"  resolved_path = {status['resolved_path']}")
    if status.get("expected_path"):
        err_console.print(f"  expected_path = {status['expected_path']}")
    if status.get("summary"):
        err_console.print(f"  summary = {status['summary']}")
    if not status.get("ready"):
        raise typer.Exit(1)


@app.command("runtime-types")
def runtime_types(as_json: bool = JSON_OPTION):
    """List advanced/internal Gateway runtime backends."""
    payload = _runtime_types_payload()
    if as_json:
        print_json(payload)
        return
    rows = []
    for item in payload["runtime_types"]:
        rows.append(
            {
                "id": item["id"],
                "label": item["label"],
                "kind": item.get("kind"),
                "activity": item.get("signals", {}).get("activity"),
                "tools": item.get("signals", {}).get("tools"),
            }
        )
    print_table(
        ["Type", "Label", "Kind", "Activity Signal", "Tool Signal"],
        rows,
        keys=["id", "label", "kind", "activity", "tools"],
    )


@app.command("templates")
def templates(as_json: bool = JSON_OPTION):
    """List Gateway agent templates and what signals they provide."""
    payload = _agent_templates_payload()
    if as_json:
        print_json(payload)
        return
    rows = []
    for item in payload["templates"]:
        rows.append(
            {
                "id": item["id"],
                "label": item["label"],
                "type": item.get("asset_type_label"),
                "output": item.get("output_label"),
                "availability": item.get("availability"),
                "summary": item.get("operator_summary"),
                "activity": item.get("signals", {}).get("activity"),
            }
        )
    print_table(
        ["Template", "Label", "Type", "Output", "Status", "Why Pick It", "Activity Signal"],
        rows,
        keys=["id", "label", "type", "output", "availability", "summary", "activity"],
    )


def _gateway_cli_argv(*args: str) -> list[str]:
    current_argv0 = str(sys.argv[0] or "").strip()
    if current_argv0:
        current_path = Path(current_argv0).expanduser()
        if current_path.exists() and current_path.name in {"ax", "axctl"}:
            return [str(current_path.resolve()), *args]
    python_bin = Path(sys.executable).resolve().parent
    for candidate in (python_bin / "ax", python_bin / "axctl"):
        if candidate.exists():
            return [str(candidate), *args]
    resolved = shutil.which("ax") or shutil.which("axctl")
    if resolved:
        return [resolved, *args]
    command = "import sys; from ax_cli.main import main; sys.argv = ['ax'] + sys.argv[1:]; main()"
    return [sys.executable, "-c", command, *args]


def _spawn_gateway_background_process(command: list[str], *, log_path: Path) -> subprocess.Popen[bytes]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as handle:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            cwd=str(Path.cwd()),
            start_new_session=True,
            close_fds=True,
        )
    return process


def _tail_log_lines(path: Path, *, lines: int = 12) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    chunks = [line.rstrip() for line in text.splitlines() if line.strip()]
    return "\n".join(chunks[-lines:])


def _wait_for_daemon_ready(process: subprocess.Popen[bytes], *, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        if daemon_status().get("running") or active_gateway_pid():
            return True
        time.sleep(0.1)
    return process.poll() is None and bool(daemon_status().get("running") or active_gateway_pid())


def _wait_for_ui_ready(process: subprocess.Popen[bytes], *, host: str, port: int, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _terminate_pids(pids: list[int], *, timeout: float = 8.0) -> tuple[list[int], list[int]]:
    requested: list[int] = []
    forced: list[int] = []
    for pid in sorted(set(pids)):
        try:
            os.kill(pid, signal.SIGTERM)
            requested.append(pid)
        except ProcessLookupError:
            continue
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = [pid for pid in requested if gateway_core._pid_alive(pid)]
        if not alive:
            return requested, forced
        time.sleep(0.1)
    for pid in requested:
        if not gateway_core._pid_alive(pid):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            forced.append(pid)
        except ProcessLookupError:
            continue
    return requested, forced


@app.command("ui")
def ui(
    host: str = typer.Option("127.0.0.1", "--host", help="Host interface to bind the local Gateway UI"),
    port: int = typer.Option(8765, "--port", help="Port for the local Gateway UI"),
    activity_limit: int = typer.Option(24, "--activity-limit", help="Number of recent events to expose in the UI"),
    refresh: float = typer.Option(2.0, "--refresh", help="Browser auto-refresh interval in seconds"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the local UI in a browser"),
):
    """Serve a local Gateway web UI."""
    refresh_ms = max(250, int(refresh * 1000))
    handler = _build_gateway_ui_handler(activity_limit=activity_limit, refresh_ms=refresh_ms)
    try:
        server = _GatewayUiServer((host, port), handler)
    except OSError as exc:
        err_console.print(f"[red]Failed to start Gateway UI:[/red] {exc}")
        raise typer.Exit(1)

    url = f"http://{host}:{server.server_port}"
    err_console.print("[bold]ax gateway ui[/bold] — local Gateway dashboard")
    err_console.print(f"  url      = {url}")
    err_console.print(f"  refresh  = {refresh_ms}ms")
    err_console.print(f"  source   = {gateway_dir()}")
    err_console.print("  stop     = Ctrl-C")
    write_gateway_ui_state(pid=os.getpid(), host=host, port=server.server_port)
    record_gateway_activity("gateway_ui_started", pid=os.getpid(), host=host, port=server.server_port, url=url)
    if open_browser:
        try:
            webbrowser.open_new_tab(url)
        except Exception:
            err_console.print("[yellow]Could not open a browser automatically.[/yellow]")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        err_console.print("[yellow]Gateway UI stopped.[/yellow]")
    finally:
        record_gateway_activity("gateway_ui_stopped", pid=os.getpid(), host=host, port=server.server_port, url=url)
        clear_gateway_ui_state(os.getpid())
        server.server_close()


@app.command("start")
def start(
    poll_interval: float = typer.Option(1.0, "--poll-interval", help="Registry reconcile interval in seconds"),
    host: str = typer.Option("127.0.0.1", "--host", help="Host interface to bind the local Gateway UI"),
    port: int = typer.Option(8765, "--port", help="Port for the local Gateway UI"),
    activity_limit: int = typer.Option(24, "--activity-limit", help="Number of recent events to expose in the UI"),
    refresh: float = typer.Option(2.0, "--refresh", help="Browser auto-refresh interval in seconds"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the local UI in a browser"),
):
    """Start the Gateway daemon and local UI in the background."""
    session = load_gateway_session()
    daemon_pid = active_gateway_pid()
    ui_pid = active_gateway_ui_pid()
    daemon_started = False
    ui_started = False
    daemon_note: str | None = None

    if daemon_pid is None:
        if session:
            daemon_process = _spawn_gateway_background_process(
                _gateway_cli_argv("gateway", "run", "--poll-interval", str(poll_interval)),
                log_path=daemon_log_path(),
            )
            if _wait_for_daemon_ready(daemon_process):
                daemon_pid = active_gateway_pid() or daemon_process.pid
                daemon_started = True
            else:
                detail = _tail_log_lines(daemon_log_path())
                err_console.print(
                    f"[red]Failed to start Gateway daemon.[/red] {detail or 'Check gateway.log for details.'}"
                )
                raise typer.Exit(1)
        else:
            daemon_note = "Gateway is not logged in yet; the UI can still start in disconnected mode."

    if ui_pid is None:
        ui_process = _spawn_gateway_background_process(
            _gateway_cli_argv(
                "gateway",
                "ui",
                "--host",
                host,
                "--port",
                str(port),
                "--activity-limit",
                str(activity_limit),
                "--refresh",
                str(refresh),
                "--no-open",
            ),
            log_path=ui_log_path(),
        )
        if _wait_for_ui_ready(ui_process, host=host, port=port):
            ui_pid = active_gateway_ui_pid() or ui_process.pid
            ui_started = True
        else:
            detail = _tail_log_lines(ui_log_path())
            if daemon_started and daemon_pid:
                _terminate_pids([daemon_pid])
                gateway_core.clear_gateway_pid()
            err_console.print(f"[red]Failed to start Gateway UI.[/red] {detail or 'Check gateway-ui.log for details.'}")
            raise typer.Exit(1)

    ui_meta = ui_status()
    if open_browser and ui_meta.get("running"):
        try:
            webbrowser.open_new_tab(str(ui_meta.get("url") or f"http://{host}:{port}"))
        except Exception:
            err_console.print("[yellow]Could not open a browser automatically.[/yellow]")

    err_console.print("[bold]ax gateway start[/bold]")
    err_console.print(f"  daemon    = {'started' if daemon_started else 'running' if daemon_pid else 'not started'}")
    if daemon_pid:
        err_console.print(f"  daemon_pid= {daemon_pid}")
    err_console.print(f"  ui        = {'started' if ui_started else 'running' if ui_pid else 'not started'}")
    if ui_pid:
        err_console.print(f"  ui_pid    = {ui_pid}")
    err_console.print(f"  url       = {ui_meta.get('url') or f'http://{host}:{port}'}")
    err_console.print(f"  logs      = {daemon_log_path()}")
    err_console.print(f"  ui_logs   = {ui_log_path()}")
    if daemon_note:
        err_console.print(f"[yellow]{daemon_note}[/yellow]")


@app.command("stop")
def stop():
    """Stop the background Gateway daemon and local UI."""
    daemon_pids = active_gateway_pids()
    ui_pids = active_gateway_ui_pids()
    if not daemon_pids and not ui_pids:
        clear_gateway_ui_state()
        gateway_core.clear_gateway_pid()
        err_console.print("[yellow]Gateway daemon and UI are already stopped.[/yellow]")
        return

    ui_requested, ui_forced = _terminate_pids(ui_pids)
    daemon_requested, daemon_forced = _terminate_pids(daemon_pids)
    clear_gateway_ui_state()
    gateway_core.clear_gateway_pid()
    record_gateway_activity(
        "gateway_services_stopped",
        daemon_pids=daemon_requested,
        ui_pids=ui_requested,
        daemon_forced=daemon_forced,
        ui_forced=ui_forced,
    )

    err_console.print("[bold]ax gateway stop[/bold]")
    err_console.print(f"  daemon = {daemon_requested or []}")
    err_console.print(f"  ui     = {ui_requested or []}")
    if daemon_forced or ui_forced:
        err_console.print(f"[yellow]Forced kill:[/yellow] daemon={daemon_forced or []} ui={ui_forced or []}")


@app.command("watch")
def watch(
    interval: float = typer.Option(2.0, "--interval", "-n", help="Dashboard refresh interval in seconds"),
    activity_limit: int = typer.Option(8, "--activity-limit", help="Number of recent events to display"),
    once: bool = typer.Option(False, "--once", help="Render one dashboard frame and exit"),
):
    """Watch the Gateway in a live terminal dashboard."""

    def render_dashboard() -> Group:
        return _render_gateway_dashboard(_status_payload(activity_limit=activity_limit))

    if once:
        console.print(render_dashboard())
        return

    try:
        with Live(render_dashboard(), console=console, screen=True, auto_refresh=False) as live:
            while True:
                live.update(render_dashboard(), refresh=True)
                time.sleep(interval)
    except KeyboardInterrupt:
        err_console.print("[yellow]Gateway watch stopped.[/yellow]")


@app.command("run")
def run(
    poll_interval: float = typer.Option(1.0, "--poll-interval", help="Registry reconcile interval in seconds"),
    once: bool = typer.Option(False, "--once", help="Run one reconcile pass and exit"),
):
    """Run the foreground Gateway supervisor."""
    _load_gateway_session_or_exit()
    err_console.print("[bold]ax gateway[/bold] — local control plane")
    err_console.print(f"  state_dir = {gateway_dir()}")
    err_console.print(f"  interval  = {poll_interval}s")
    err_console.print(f"  mode      = {'single-pass' if once else 'foreground'}")
    daemon = GatewayDaemon(logger=lambda msg: err_console.print(f"[dim]{msg}[/dim]"), poll_interval=poll_interval)
    try:
        daemon.run(once=once)
    except RuntimeError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    except KeyboardInterrupt:
        daemon.stop()
        err_console.print("[yellow]Gateway stopped.[/yellow]")


@approvals_app.command("list")
def list_approvals(
    status: str | None = typer.Option(
        None, "--status", help="Optional filter: pending | approved | rejected | archived"
    ),
    include_archived: bool = typer.Option(False, "--include-archived", help="Include archived/stale approvals"),
    as_json: bool = JSON_OPTION,
):
    """List local Gateway approval requests."""
    payload = _approval_rows_payload(status=status, include_archived=include_archived)
    if as_json:
        print_json(payload)
        return
    err_console.print("[bold]ax gateway approvals list[/bold]")
    err_console.print(f"  approvals = {payload['count']}")
    err_console.print(f"  pending   = {payload['pending']}")
    if not payload["approvals"]:
        err_console.print("[dim]No Gateway approvals found.[/dim]")
        return
    print_table(
        ["Approval", "Asset", "Kind", "Status", "Risk", "Reason", "Requested"],
        payload["approvals"],
        keys=["approval_id", "asset_id", "approval_kind", "status", "risk", "reason", "requested_at"],
    )


@approvals_app.command("cleanup")
def cleanup_approvals(as_json: bool = JSON_OPTION):
    """Archive stale approval requests that no longer match managed agents."""
    payload = archive_stale_gateway_approvals()
    if as_json:
        print_json(payload)
        return
    archived_count = int(payload.get("archived_count") or 0)
    err_console.print(f"[green]Archived stale approvals:[/green] {archived_count}")
    err_console.print(f"  pending = {payload.get('remaining_pending', 0)}")


@approvals_app.command("show")
def show_approval(
    approval_id: str = typer.Argument(..., help="Approval request id"),
    as_json: bool = JSON_OPTION,
):
    """Show one local Gateway approval request."""
    try:
        payload = _approval_detail_payload(approval_id)
    except LookupError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if as_json:
        print_json(payload)
        return
    approval = payload["approval"]
    print_table(
        ["Field", "Value"],
        [
            {"field": "approval_id", "value": approval.get("approval_id")},
            {"field": "asset_id", "value": approval.get("asset_id")},
            {"field": "gateway_id", "value": approval.get("gateway_id")},
            {"field": "install_id", "value": approval.get("install_id")},
            {"field": "kind", "value": approval.get("approval_kind")},
            {"field": "status", "value": approval.get("status")},
            {"field": "risk", "value": approval.get("risk")},
            {"field": "action", "value": approval.get("action")},
            {"field": "resource", "value": approval.get("resource")},
            {"field": "reason", "value": approval.get("reason")},
            {"field": "requested_at", "value": approval.get("requested_at")},
            {"field": "decided_at", "value": approval.get("decided_at")},
            {"field": "decision_scope", "value": approval.get("decision_scope")},
        ],
        keys=["field", "value"],
    )
    candidate = approval.get("candidate_binding") if isinstance(approval.get("candidate_binding"), dict) else None
    if candidate:
        print_table(
            ["Candidate Field", "Value"],
            [
                {"field": "path", "value": candidate.get("path")},
                {"field": "binding_type", "value": candidate.get("binding_type")},
                {"field": "launch_spec_hash", "value": candidate.get("launch_spec_hash")},
                {"field": "candidate_signature", "value": candidate.get("candidate_signature")},
            ],
            keys=["field", "value"],
        )


@approvals_app.command("approve")
def approve_approval(
    approval_id: str = typer.Argument(..., help="Approval request id"),
    scope: str = typer.Option("asset", "--scope", help="Recorded approval scope: once | asset | gateway"),
    as_json: bool = JSON_OPTION,
):
    """Approve a local Gateway binding request."""
    try:
        payload = approve_gateway_approval(approval_id, scope=scope)
    except (LookupError, ValueError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if as_json:
        print_json(payload)
        return
    approval = payload["approval"]
    err_console.print(f"[green]Approved:[/green] {approval['approval_id']}")
    err_console.print(f"  asset = {approval.get('asset_id')}")
    err_console.print(f"  scope = {approval.get('decision_scope')}")


@approvals_app.command("deny")
def deny_approval(
    approval_id: str = typer.Argument(..., help="Approval request id"),
    as_json: bool = JSON_OPTION,
):
    """Deny a local Gateway binding request."""
    try:
        payload = deny_gateway_approval(approval_id)
    except LookupError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if as_json:
        print_json({"approval": payload})
        return
    err_console.print(f"[yellow]Denied:[/yellow] {payload['approval_id']}")
    err_console.print(f"  asset = {payload.get('asset_id')}")


@local_app.command("connect")
def local_connect(
    agent_name: str | None = typer.Argument(None, help="Local pass-through agent name"),
    registry_ref: str = typer.Option(
        None,
        "--registry",
        "--ref",
        help="Existing Gateway registry row, name, install id, or id prefix to reconnect",
    ),
    gateway_url: str = typer.Option("http://127.0.0.1:8765", "--url", help="Local Gateway UI/API URL"),
    workdir: str = typer.Option(None, "--workdir", help="Workspace folder to fingerprint"),
    space_id: str = typer.Option(None, "--space-id", help="Initial home space if Gateway cannot infer one"),
    as_json: bool = JSON_OPTION,
):
    """Request Gateway access for a local polling/pass-through agent."""
    try:
        payload = _request_local_connect(
            agent_name=agent_name,
            registry_ref=registry_ref,
            gateway_url=gateway_url,
            workdir=workdir,
            space_id=space_id,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if as_json:
        print_json(payload)
        return
    status = str(payload.get("status") or "pending")
    connected_name = str(payload.get("agent", {}).get("name") or agent_name or registry_ref or "")
    console.print(f"[bold]local connect[/bold] @{connected_name}: {status}")
    if payload.get("registry_ref"):
        console.print(f"  registry = {payload['registry_ref']}")
    if payload.get("approval_id"):
        console.print(f"  approval = {payload['approval_id']}")
    if payload.get("session_token"):
        console.print(f"  session  = {payload['session_token']}")
        console.print(f"  expires  = {payload.get('expires_at')}")


def _gateway_local_config_text(*, agent_name: str, gateway_url: str, workdir: str | None = None) -> str:
    lines = [
        "[gateway]",
        'mode = "local"',
        f'url = "{gateway_url}"',
        "",
        "[agent]",
        f'agent_name = "{agent_name}"',
    ]
    if workdir:
        lines.append(f'workdir = "{workdir}"')
    return "\n".join(lines) + "\n"


def _gateway_local_config_from_workdir(workdir: str | None) -> dict:
    if not workdir:
        return {}
    config_path = Path(workdir).expanduser().resolve() / ".ax" / "config.toml"
    if not config_path.exists():
        return {}
    try:
        cfg = tomllib.loads(config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    gateway = cfg.get("gateway") if isinstance(cfg.get("gateway"), dict) else {}
    agent = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}
    mode = str(gateway.get("mode") or cfg.get("gateway_mode") or "").strip().lower()
    url = str(gateway.get("url") or gateway.get("base_url") or cfg.get("gateway_url") or "").strip()
    agent_name = str(
        agent.get("agent_name") or agent.get("name") or cfg.get("gateway_agent_name") or cfg.get("agent_name") or ""
    ).strip()
    registry_ref = str(
        agent.get("registry_ref") or agent.get("registry") or cfg.get("gateway_registry_ref") or ""
    ).strip()
    if mode not in {"local", "pass_through", "gateway"} and not url:
        return {}
    return {
        "agent_name": agent_name or None,
        "registry_ref": registry_ref or None,
        "gateway_url": url or None,
        "config_path": str(config_path),
    }


def _resolve_local_gateway_identity(
    *,
    agent_name: str | None,
    registry_ref: str | None,
    workdir: str | None,
) -> tuple[str | None, str | None]:
    workdir_cfg = _gateway_local_config_from_workdir(workdir)
    configured_agent = str(workdir_cfg.get("agent_name") or "").strip()
    configured_ref = str(workdir_cfg.get("registry_ref") or "").strip()
    requested_agent = str(agent_name or "").strip()
    requested_ref = str(registry_ref or "").strip()

    if configured_agent and requested_agent and configured_agent != requested_agent:
        raise ValueError(
            "Gateway identity mismatch: "
            f"{workdir_cfg.get('config_path')} is configured for @{configured_agent}, "
            f"but this command requested @{requested_agent}. "
            "Run from that agent's directory, omit --agent, or update the repo-local Gateway config."
        )
    if configured_ref and requested_ref and configured_ref != requested_ref:
        raise ValueError(
            "Gateway registry mismatch: "
            f"{workdir_cfg.get('config_path')} is configured for {configured_ref}, "
            f"but this command requested {requested_ref}."
        )
    if not requested_agent and not requested_ref:
        requested_agent = configured_agent
        requested_ref = configured_ref
    return requested_agent or None, requested_ref or None


def _approval_required_guidance(
    *,
    connect_payload: dict,
    gateway_url: str,
    agent_name: str | None = None,
    workdir: str | None = None,
    action: str = "continue",
) -> str:
    agent = connect_payload.get("agent") if isinstance(connect_payload.get("agent"), dict) else {}
    approval = connect_payload.get("approval") if isinstance(connect_payload.get("approval"), dict) else {}
    fingerprint = connect_payload.get("fingerprint") if isinstance(connect_payload.get("fingerprint"), dict) else {}
    name = str(agent.get("name") or agent_name or connect_payload.get("agent_name") or "").strip()
    approval_id = str(
        connect_payload.get("approval_id") or approval.get("approval_id") or agent.get("approval_id") or ""
    ).strip()
    resolved_workdir = str(
        workdir or agent.get("workdir") or fingerprint.get("cwd") or approval.get("resource") or ""
    ).strip()
    space_label = str(
        agent.get("active_space_name")
        or agent.get("active_space_id")
        or agent.get("space_name")
        or agent.get("space_id")
        or ""
    ).strip()
    binding_type = str(approval.get("approval_kind") or approval.get("action") or "runtime binding").strip()
    risk = str(approval.get("risk") or "").strip()

    subject = f"@{name}" if name else "this local agent"
    lines = [
        f"Gateway approval required for {subject}.",
        f"Ask the user to open {gateway_url.rstrip('/')} and approve the pending binding before I can {action}.",
    ]
    details = []
    if approval_id:
        details.append(f"approval_id={approval_id}")
    if resolved_workdir:
        details.append(f"workdir={resolved_workdir}")
    if space_label:
        details.append(f"space={space_label}")
    if binding_type:
        details.append(f"binding={binding_type}")
    if risk:
        details.append(f"risk={risk}")
    if details:
        lines.append("Details: " + " ".join(details))
    lines.append("Do not fall back to a direct PAT; this agent is waiting on Gateway approval.")
    return " ".join(lines)


@local_app.command("init")
def local_init(
    agent_name: str = typer.Argument(..., help="Local Gateway agent name"),
    gateway_url: str = typer.Option("http://127.0.0.1:8765", "--url", help="Local Gateway UI/API URL"),
    workdir: str = typer.Option(None, "--workdir", help="Workspace folder to configure; defaults to CWD"),
    connect: bool = typer.Option(True, "--connect/--no-connect", help="Immediately request Gateway approval/session"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing .ax/config.toml"),
    as_json: bool = JSON_OPTION,
):
    """Write a Gateway-native local config that contains no PAT or token file."""
    root = Path(workdir or Path.cwd()).expanduser().resolve()
    ax_dir = root / ".ax"
    config_path = ax_dir / "config.toml"
    if config_path.exists() and not force:
        raise typer.BadParameter(f"{config_path} already exists; pass --force to replace it.")
    ax_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        _gateway_local_config_text(agent_name=agent_name, gateway_url=gateway_url, workdir=str(root)),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    payload: dict = {
        "config_path": str(config_path),
        "workdir": str(root),
        "agent_name": agent_name,
        "gateway_url": gateway_url,
        "token_stored": False,
    }
    if connect:
        try:
            payload["connect"] = _request_local_connect(
                agent_name=agent_name,
                gateway_url=gateway_url,
                workdir=str(root),
                space_id=None,
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    if as_json:
        print_json(payload)
        return
    console.print(f"[green]Gateway local config written:[/green] {config_path}")
    console.print(f"  agent  = {agent_name}")
    console.print("  token  = not stored")
    if payload.get("connect"):
        status = str(payload["connect"].get("status") or "pending")
        console.print(f"  status = {status}")
        if payload["connect"].get("approval_id"):
            console.print(f"  approval = {payload['connect']['approval_id']}")


def _request_local_connect(
    *,
    agent_name: str | None = None,
    registry_ref: str | None = None,
    gateway_url: str = "http://127.0.0.1:8765",
    workdir: str | None = None,
    space_id: str | None = None,
) -> dict:
    agent_name, registry_ref = _resolve_local_gateway_identity(
        agent_name=agent_name,
        registry_ref=registry_ref,
        workdir=workdir,
    )
    display_name = str(agent_name or registry_ref or "").strip()
    if not display_name:
        raise ValueError("Provide a local agent name or --registry/--ref.")
    fingerprint = _local_process_fingerprint(agent_name=display_name, cwd=workdir)
    body = {"fingerprint": fingerprint}
    if agent_name:
        body["agent_name"] = agent_name
    if registry_ref:
        body["registry_ref"] = registry_ref
    if space_id:
        body["space_id"] = space_id
    try:
        response = httpx.post(
            f"{gateway_url.rstrip('/')}/local/connect",
            json=body,
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        try:
            detail = exc.response.json().get("error", detail)
        except Exception:
            pass
        raise ValueError(f"Gateway local connect failed: {detail}") from exc
    except Exception as exc:
        raise ValueError(f"Gateway local connect failed: {exc}") from exc
    return payload


def _resolve_local_gateway_session(
    *,
    session_token: str | None,
    agent_name: str | None = None,
    registry_ref: str | None = None,
    gateway_url: str = "http://127.0.0.1:8765",
    workdir: str | None = None,
    space_id: str | None = None,
) -> tuple[str, dict | None]:
    token = str(session_token or "").strip()
    if token:
        return token, None
    payload = _request_local_connect(
        agent_name=agent_name,
        registry_ref=registry_ref,
        gateway_url=gateway_url,
        workdir=workdir,
        space_id=space_id,
    )
    token = str(payload.get("session_token") or "").strip()
    if not token:
        status = str(payload.get("status") or "pending")
        if status == "pending":
            raise ValueError(
                _approval_required_guidance(
                    connect_payload=payload,
                    gateway_url=gateway_url,
                    agent_name=agent_name,
                    workdir=workdir,
                    action="send or poll",
                )
            )
        raise ValueError(f"Gateway local session is {status}; approve the agent before sending.")
    return token, payload


def _poll_local_inbox_over_http(
    *,
    gateway_url: str,
    session_token: str,
    limit: int = 10,
    channel: str = "main",
    space_id: str | None = None,
    mark_read: bool = True,
    wait_seconds: int = 0,
    poll_interval: float = 1.0,
) -> dict:
    params = {
        "limit": limit,
        "channel": channel,
        "unread_only": "true",
        "mark_read": "true" if mark_read else "false",
    }
    if space_id:
        params["space_id"] = space_id
    deadline = time.monotonic() + wait_seconds
    payload = None
    while True:
        response = httpx.get(
            f"{gateway_url.rstrip('/')}/local/inbox",
            params=params,
            headers={"X-Gateway-Session": session_token},
            timeout=20.0,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("messages") or wait_seconds <= 0 or time.monotonic() >= deadline:
            return payload
        time.sleep(poll_interval)


@local_app.command("send")
def local_send(
    session_token: str = typer.Option(
        None, "--session-token", envvar="AX_GATEWAY_SESSION", help="Gateway session token"
    ),
    content: str = typer.Argument(..., help="Message content"),
    space_id: str = typer.Option(None, "--space-id", help="Space to send into"),
    agent_name: str = typer.Option(
        None, "--agent", "--name", help="Approved local pass-through agent to connect as if no session token is set"
    ),
    registry_ref: str = typer.Option(
        None, "--registry", "--ref", help="Existing Gateway registry row to connect as if no session token is set"
    ),
    workdir: str = typer.Option(None, "--workdir", help="Workspace folder to fingerprint when auto-connecting"),
    gateway_url: str = typer.Option("http://127.0.0.1:8765", "--url", help="Local Gateway UI/API URL"),
    parent_id: str = typer.Option(None, "--parent-id", help="Optional parent message id"),
    include_inbox: bool = typer.Option(
        True,
        "--inbox/--no-inbox",
        help="After sending, include unread messages waiting for this pass-through agent.",
    ),
    inbox_wait: int = typer.Option(
        2,
        "--inbox-wait",
        min=0,
        help="Seconds to wait for inbound messages after sending. Use 0 to only check immediately.",
    ),
    inbox_limit: int = typer.Option(10, "--inbox-limit", min=1, max=100, help="Max inbound messages to return."),
    as_json: bool = JSON_OPTION,
):
    """Send through an approved local pass-through Gateway session."""
    try:
        resolved_session_token, connect_payload = _resolve_local_gateway_session(
            session_token=session_token,
            agent_name=agent_name,
            registry_ref=registry_ref,
            gateway_url=gateway_url,
            workdir=workdir,
            space_id=space_id,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    body = {"content": content, "space_id": space_id, "parent_id": parent_id}
    try:
        response = httpx.post(
            f"{gateway_url.rstrip('/')}/local/send",
            json=body,
            headers={"X-Gateway-Session": resolved_session_token},
            timeout=20.0,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        try:
            detail = exc.response.json().get("error", detail)
        except Exception:
            pass
        raise typer.BadParameter(f"Gateway local send failed: {detail}") from exc
    except Exception as exc:
        raise typer.BadParameter(f"Gateway local send failed: {exc}") from exc
    if include_inbox:
        try:
            payload["inbox"] = _poll_local_inbox_over_http(
                gateway_url=gateway_url,
                session_token=resolved_session_token,
                limit=inbox_limit,
                space_id=space_id,
                mark_read=True,
                wait_seconds=inbox_wait,
            )
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text
            try:
                detail = exc.response.json().get("error", detail)
            except Exception:
                pass
            payload["inbox_error"] = detail
        except Exception as exc:
            payload["inbox_error"] = str(exc)
    if as_json:
        if connect_payload:
            payload["connect"] = {
                "status": connect_payload.get("status"),
                "registry_ref": connect_payload.get("registry_ref"),
                "agent": (connect_payload.get("agent") or {}).get("name")
                if isinstance(connect_payload.get("agent"), dict)
                else None,
            }
        print_json(payload)
        return
    console.print(f"[green]Sent through Gateway[/green] as @{payload.get('agent')}")
    inbox_payload = payload.get("inbox") if isinstance(payload.get("inbox"), dict) else {}
    messages = inbox_payload.get("messages") if isinstance(inbox_payload, dict) else []
    if messages:
        console.print(
            f"[bold]inbox[/bold] @{inbox_payload.get('agent') or payload.get('agent')}: {len(messages)} unread"
        )
        for message in messages:
            created = str(message.get("created_at") or "")
            author = str(message.get("display_name") or message.get("agent_name") or message.get("sender") or "-")
            body_text = str(message.get("content") or "").replace("\n", " ")
            console.print(f"  {created} {author}: {body_text[:160]}")
    elif payload.get("inbox_error"):
        console.print(f"[yellow]Inbox check failed:[/yellow] {payload['inbox_error']}")


@local_app.command("inbox")
def local_inbox(
    session_token: str = typer.Option(
        None, "--session-token", envvar="AX_GATEWAY_SESSION", help="Gateway session token"
    ),
    limit: int = typer.Option(20, "--limit", min=1, max=100, help="Max messages to return"),
    channel: str = typer.Option("main", "--channel", help="Message channel"),
    space_id: str = typer.Option(None, "--space-id", help="Space to poll"),
    agent_name: str = typer.Option(
        None, "--agent", "--name", help="Approved local pass-through agent to connect as if no session token is set"
    ),
    registry_ref: str = typer.Option(
        None, "--registry", "--ref", help="Existing Gateway registry row to connect as if no session token is set"
    ),
    workdir: str = typer.Option(None, "--workdir", help="Workspace folder to fingerprint when auto-connecting"),
    mark_read: bool = typer.Option(
        True,
        "--mark-read/--no-mark-read",
        help="Mark returned messages as read. Use --no-mark-read to peek without clearing.",
    ),
    wait_seconds: int = typer.Option(
        0,
        "--wait",
        min=0,
        help="Wait up to this many seconds for an inbox message before returning.",
    ),
    poll_interval: float = typer.Option(
        2.0,
        "--poll-interval",
        min=0.5,
        max=30.0,
        help="Seconds between inbox checks when --wait is used.",
    ),
    gateway_url: str = typer.Option("http://127.0.0.1:8765", "--url", help="Local Gateway UI/API URL"),
    as_json: bool = JSON_OPTION,
):
    """Poll an approved local pass-through Gateway inbox."""
    try:
        resolved_session_token, connect_payload = _resolve_local_gateway_session(
            session_token=session_token,
            agent_name=agent_name,
            registry_ref=registry_ref,
            gateway_url=gateway_url,
            workdir=workdir,
            space_id=space_id,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    try:
        payload = _poll_local_inbox_over_http(
            gateway_url=gateway_url,
            session_token=resolved_session_token,
            limit=limit,
            channel=channel,
            space_id=space_id,
            mark_read=mark_read,
            wait_seconds=wait_seconds,
            poll_interval=poll_interval,
        )
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        try:
            detail = exc.response.json().get("error", detail)
        except Exception:
            pass
        raise typer.BadParameter(f"Gateway local inbox failed: {detail}") from exc
    except Exception as exc:
        raise typer.BadParameter(f"Gateway local inbox failed: {exc}") from exc
    if as_json:
        if connect_payload:
            payload["connect"] = {
                "status": connect_payload.get("status"),
                "registry_ref": connect_payload.get("registry_ref"),
                "agent": (connect_payload.get("agent") or {}).get("name")
                if isinstance(connect_payload.get("agent"), dict)
                else None,
            }
        if wait_seconds > 0:
            payload["waited_seconds"] = wait_seconds
        print_json(payload)
        return
    messages = payload.get("messages") or []
    console.print(f"[bold]local inbox[/bold] @{payload.get('agent')}: {len(messages)} unread")
    for message in messages:
        created = str(message.get("created_at") or "")
        author = str(message.get("display_name") or message.get("agent_name") or message.get("sender") or "-")
        content = str(message.get("content") or "").replace("\n", " ")
        console.print(f"  {created} {author}: {content[:160]}")


@agents_app.command("add")
def add_agent(
    name: str = typer.Argument(..., help="Managed agent name"),
    template_id: str = typer.Option(
        None, "--template", help="Agent template: echo_test | ollama | hermes | sentinel_cli | claude_code_channel"
    ),
    runtime_type: str = typer.Option(
        None,
        "--type",
        help="Advanced/internal runtime backend: echo | exec | hermes_sentinel | sentinel_cli | claude_code_channel | inbox",
    ),
    exec_cmd: str = typer.Option(None, "--exec", help="Advanced override for exec-based templates"),
    workdir: str = typer.Option(None, "--workdir", help="Advanced working directory override"),
    ollama_model: str = typer.Option(None, "--ollama-model", help="Ollama model override for the Ollama template"),
    space_id: str = typer.Option(None, "--space-id", help="Target space (defaults to gateway session)"),
    audience: str = typer.Option("both", "--audience", help="Minted PAT audience"),
    description: str = typer.Option(None, "--description", help="Create/update description"),
    model: str = typer.Option(None, "--model", help="Create/update model"),
    timeout_seconds: int = typer.Option(
        None, "--timeout", "--timeout-seconds", help="Max seconds a runtime may process one message"
    ),
    start: bool = typer.Option(True, "--start/--no-start", help="Desired running state after registration"),
    as_json: bool = JSON_OPTION,
):
    """Register a managed agent and mint a Gateway-owned PAT for it."""
    selected_template = template_id or ("echo_test" if not runtime_type else None)
    try:
        entry = _register_managed_agent(
            name=name,
            template_id=selected_template,
            runtime_type=runtime_type,
            exec_cmd=exec_cmd,
            workdir=workdir,
            ollama_model=ollama_model,
            space_id=space_id,
            audience=audience,
            description=description,
            model=model,
            timeout_seconds=timeout_seconds,
            start=start,
        )
    except (ValueError, LookupError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if as_json:
        print_json(entry)
    else:
        err_console.print(f"[green]Managed agent ready:[/green] @{name}")
        if entry.get("template_label"):
            err_console.print(f"  type = {entry['template_label']}")
        if entry.get("asset_type_label"):
            err_console.print(f"  asset = {entry['asset_type_label']}")
        err_console.print(f"  desired_state = {entry['desired_state']}")
        if entry.get("timeout_seconds"):
            err_console.print(f"  timeout = {entry.get('timeout_seconds')}s")
        err_console.print(f"  token_file = {entry['token_file']}")


@agents_app.command("update")
def update_agent(
    name: str = typer.Argument(..., help="Managed agent name"),
    template_id: str = typer.Option(None, "--template", help="Replace the agent template"),
    runtime_type: str = typer.Option(
        None,
        "--type",
        help="Advanced/internal runtime backend override: echo | exec | hermes_sentinel | sentinel_cli | claude_code_channel | inbox",
    ),
    exec_cmd: str = typer.Option(None, "--exec", help="Advanced override for exec-based templates"),
    workdir: str = typer.Option(None, "--workdir", help="Advanced working directory override"),
    ollama_model: str = typer.Option(None, "--ollama-model", help="Ollama model override for the Ollama template"),
    description: str = typer.Option(None, "--description", help="Update platform agent description"),
    model: str = typer.Option(None, "--model", help="Update platform agent model"),
    timeout_seconds: int = typer.Option(
        None, "--timeout", "--timeout-seconds", help="Max seconds a runtime may process one message"
    ),
    desired_state: str = typer.Option(None, "--desired-state", help="running | stopped"),
    as_json: bool = JSON_OPTION,
):
    """Update a managed agent without redoing Gateway bootstrap."""
    try:
        entry = _update_managed_agent(
            name=name,
            template_id=template_id,
            runtime_type=runtime_type,
            exec_cmd=exec_cmd if exec_cmd is not None else _UNSET,
            workdir=workdir if workdir is not None else _UNSET,
            ollama_model=ollama_model if ollama_model is not None else _UNSET,
            description=description,
            model=model,
            timeout_seconds=timeout_seconds if timeout_seconds is not None else _UNSET,
            desired_state=desired_state,
        )
    except (LookupError, ValueError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if as_json:
        print_json(entry)
        return
    err_console.print(f"[green]Managed agent updated:[/green] @{name}")
    err_console.print(f"  type = {entry.get('template_label') or entry.get('runtime_type')}")
    err_console.print(f"  desired_state = {entry.get('desired_state')}")
    if entry.get("timeout_seconds"):
        err_console.print(f"  timeout = {entry.get('timeout_seconds')}s")


@agents_app.command("list")
def list_agents(as_json: bool = JSON_OPTION):
    """List Gateway-managed agents."""
    agents = _status_payload()["agents"]
    if as_json:
        print_json({"agents": agents, "count": len(agents)})
        return
    print_table(
        ["Ref", "Agent", "Type", "Mode", "Presence", "Output", "Confidence", "Space"],
        [{**agent, "type": _agent_type_label(agent), "output": _agent_output_label(agent)} for agent in agents],
        keys=["registry_ref", "name", "type", "mode", "presence", "output", "confidence", "space_id"],
    )


@agents_app.command("show")
def show_agent(
    name: str = typer.Argument(..., help="Managed agent name"),
    activity_limit: int = typer.Option(12, "--activity-limit", help="Number of recent agent events to display"),
    as_json: bool = JSON_OPTION,
):
    """Show one managed agent in detail."""
    result = _agent_detail_payload(name, activity_limit=activity_limit)
    if result is None:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
        raise typer.Exit(1)
    if as_json:
        print_json(result)
        return
    console.print(_render_agent_detail(result["agent"], activity=result["recent_activity"]))


@agents_app.command("test")
def test_agent(
    name: str = typer.Argument(..., help="Managed agent name"),
    message: str = typer.Option(None, "--message", help="Override the recommended Gateway test prompt"),
    author: str = typer.Option("agent", "--author", help="Who should author the test message: agent | user"),
    sender_agent: str = typer.Option(None, "--sender-agent", help="Managed sender identity to use when --author agent"),
    as_json: bool = JSON_OPTION,
):
    """Send a Gateway-authored test message to one managed agent."""
    try:
        result = _send_gateway_test_to_managed_agent(name, content=message, author=author, sender_agent=sender_agent)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if as_json:
        print_json(result)
        return

    err_console.print(f"[green]Gateway test sent:[/green] @{result['target_agent']}")
    err_console.print(f"  prompt = {result['recommended_prompt']}")
    message_payload = result.get("message") or {}
    if isinstance(message_payload, dict) and message_payload.get("id"):
        err_console.print(f"  message_id = {message_payload['id']}")


@agents_app.command("move")
def move_agent(
    name: str = typer.Argument(..., help="Managed agent name"),
    space_id: str = typer.Option(..., "--space-id", "-s", help="Target space id"),
    as_json: bool = JSON_OPTION,
):
    """Move a Gateway-managed agent to another allowed space."""
    try:
        result = _move_managed_agent_space(name, space_id)
    except (LookupError, ValueError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if as_json:
        print_json(result)
        return

    err_console.print(f"[green]Managed agent moved:[/green] @{name}")
    err_console.print(
        f"  space = {result.get('active_space_name') or result.get('active_space_id') or result.get('space_id')}"
    )


@agents_app.command("doctor")
def doctor_agent(
    name: str = typer.Argument(..., help="Managed agent name"),
    send_test: bool = typer.Option(False, "--send-test", help="Also send a Gateway-authored smoke test"),
    as_json: bool = JSON_OPTION,
):
    """Run Gateway Doctor checks for one managed agent."""
    try:
        result = _run_gateway_doctor(name, send_test=send_test)
    except (LookupError, ValueError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if as_json:
        print_json(result)
        return

    tone = {"passed": "green", "warning": "yellow", "failed": "red"}.get(result["status"], "cyan")
    err_console.print(f"[{tone}]Gateway Doctor {result['status']}:[/{tone}] @{name}")
    err_console.print(f"  summary = {result['summary']}")
    print_table(["Check", "Status", "Detail"], result["checks"], keys=["name", "status", "detail"])


@agents_app.command("send")
def send_as_agent(
    name: str = typer.Argument(..., help="Managed agent name to send as"),
    content: str = typer.Argument(..., help="Message content"),
    to: str = typer.Option(None, "--to", help="Prepend a mention like @codex automatically"),
    parent_id: str = typer.Option(None, "--parent-id", help="Reply inside an existing thread"),
    as_json: bool = JSON_OPTION,
):
    """Send a message as a Gateway-managed agent."""
    try:
        result = _send_from_managed_agent(name=name, content=content, to=to, parent_id=parent_id)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if as_json:
        print_json(result)
        return
    err_console.print(f"[green]Sent as managed agent:[/green] @{result['agent']}")
    if isinstance(result["message"], dict) and result["message"].get("id"):
        err_console.print(f"  id = {result['message']['id']}")
    err_console.print(f"  content = {result['content']}")


@agents_app.command("start")
def start_agent(name: str = typer.Argument(..., help="Managed agent name")):
    """Set a managed agent's desired state to running."""
    try:
        _set_managed_agent_desired_state(name, "running")
    except LookupError:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
        raise typer.Exit(1)
    err_console.print(f"[green]Desired state set to running:[/green] @{name}")


def _attach_command_for_payload(payload: dict) -> str:
    workdir = Path(str(payload["mcp_path"])).parent
    return f"cd {shlex.quote(str(workdir))} && {payload['launch_command']}"


def _prepare_attached_agent_payload(name: str) -> dict:
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    profile = gateway_core.infer_operator_profile(entry)
    if profile["placement"] != "attached" or profile["activation"] != "attach_only":
        raise ValueError(f"@{name} is not an attached-session agent.")
    workdir = str(entry.get("workdir") or "").strip()
    if not workdir:
        raise ValueError(f"Attached agent has no workdir: @{name}")

    from ..commands.channel import write_channel_setup

    payload = write_channel_setup(agent_name=name, workdir=Path(workdir))
    payload["attach_command"] = _attach_command_for_payload(payload)
    _set_managed_agent_desired_state(name, "running")
    return payload


def _launch_attached_agent_session(payload: dict) -> dict:
    workdir = Path(str(payload["mcp_path"])).parent
    launch_command = str(payload.get("launch_command") or "").strip()
    server_name = str(payload.get("server_name") or "ax-channel").strip() or "ax-channel"
    agent_name = str(payload.get("agent") or "attached-session").strip() or "attached-session"
    registry = load_gateway_registry()
    existing_entry = find_agent_entry(registry, agent_name)
    old_pid = int(existing_entry.get("attached_session_pid") or 0) if existing_entry else 0
    if old_pid:
        try:
            os.killpg(old_pid, signal.SIGTERM)
        except OSError:
            pass
    command = [
        "claude",
        "--strict-mcp-config",
        "--mcp-config",
        str(payload["mcp_path"]),
        "--dangerously-load-development-channels",
        f"server:{server_name}",
    ]
    if not shutil.which("claude"):
        raise ValueError("Claude Code is not on PATH. Install or open Claude Code, then try attaching again.")

    log_path = agent_dir(agent_name) / "attached-session.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as handle:
        script_bin = shutil.which("script")
        if script_bin and sys.platform == "darwin":
            # Claude Code expects a TTY. macOS `script file command ...` gives
            # it a background pseudo-terminal without opening Terminal.app.
            # `script` writes the PTY transcript to log_path itself; routing
            # stdout to the same handle duplicates every byte on macOS.
            process_command = [script_bin, "-q", str(log_path), *command]
            stdin = subprocess.PIPE
            stdout = subprocess.DEVNULL
        elif script_bin:
            process_command = [script_bin, "-q", "-f", "-c", launch_command, str(log_path)]
            stdin = subprocess.PIPE
            stdout = subprocess.DEVNULL
        else:
            process_command = command
            stdin = subprocess.DEVNULL
            stdout = handle
        process = subprocess.Popen(
            process_command,
            cwd=str(workdir),
            stdin=stdin,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        if process.stdin:
            # Claude Code may ask first-run terminal questions before the
            # channel starts. These answers select the default prompt choices
            # so Gateway can attach without opening an operator terminal.
            for _ in range(3):
                try:
                    process.stdin.write(b"1\n\n")
                    process.stdin.flush()
                except OSError:
                    break
                time.sleep(0.45)
            if process.poll() is not None:
                handle.write(
                    b"\n[ax-gateway] Claude Code exited immediately during background attach; "
                    b"open this workspace manually or rerun Start after resolving first-run prompts.\n"
                )
                handle.flush()
        _ATTACHED_SESSION_PROCESSES[:] = [managed for managed in _ATTACHED_SESSION_PROCESSES if managed.poll() is None]
        _ATTACHED_SESSION_PROCESSES.append(process)
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, agent_name)
    if entry:
        entry["desired_state"] = "running"
        entry["effective_state"] = "starting"
        entry["current_status"] = "attaching"
        entry["current_activity"] = "Starting Claude Code"
        entry["attached_session_pid"] = process.pid
        entry["attached_session_log_path"] = str(log_path)
        entry["last_started_at"] = datetime.now(timezone.utc).isoformat()
        save_gateway_registry(registry)
    return {
        **payload,
        "launched": True,
        "launch_mode": "background",
        "pid": process.pid,
        "log_path": str(log_path),
        "message": "Started Claude Code channel in the background.",
    }


@agents_app.command("attach")
def attach_agent(
    name: str = typer.Argument(..., help="Attached-session agent name"),
    run: bool = typer.Option(False, "--run", help="Run Claude Code in this terminal after writing config"),
    as_json: bool = JSON_OPTION,
):
    """Write channel config for an attached Claude Code agent and print the attach command."""
    try:
        payload = _prepare_attached_agent_payload(name)
    except LookupError:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
        raise typer.Exit(1)
    except ValueError as exc:
        err_console.print(f"[red]Not an attached-session agent:[/red] @{name}")
        err_console.print(str(exc))
        raise typer.Exit(1)
    workdir = str(Path(str(payload["mcp_path"])).parent)
    if as_json:
        print_json(payload)
        return
    err_console.print(f"[green]Claude Code channel ready:[/green] @{name}")
    err_console.print(f"  workdir = {workdir}")
    err_console.print(f"  mcp     = {payload['mcp_path']}")
    err_console.print(f"  env     = {payload['env_path']}")
    err_console.print("")
    err_console.print("[bold]Attach from a terminal:[/bold]")
    err_console.print(payload["attach_command"])
    if run:
        os.chdir(workdir)
        os.execvp(
            "claude",
            [
                "claude",
                "--strict-mcp-config",
                "--mcp-config",
                payload["mcp_path"],
                "--dangerously-load-development-channels",
                f"server:{payload['server_name']}",
            ],
        )


@agents_app.command("stop")
def stop_agent(name: str = typer.Argument(..., help="Managed agent name")):
    """Set a managed agent's desired state to stopped."""
    try:
        _set_managed_agent_desired_state(name, "stopped")
    except LookupError:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
        raise typer.Exit(1)
    err_console.print(f"[green]Desired state set to stopped:[/green] @{name}")


@agents_app.command("remove")
def remove_agent(name: str = typer.Argument(..., help="Managed agent name")):
    """Remove a managed agent from local Gateway control."""
    try:
        _remove_managed_agent(name)
    except LookupError:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
        raise typer.Exit(1)
    err_console.print(f"[green]Removed managed agent:[/green] @{name}")
