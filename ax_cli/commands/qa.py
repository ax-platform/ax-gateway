"""ax qa — API-first regression smoke checks."""

from __future__ import annotations

import json
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import typer
from rich.table import Table

from ..client import AxClient
from ..config import (
    _global_config_dir,
    _load_user_config,
    _normalize_user_env,
    _user_config_path,
    diagnose_auth_config,
    get_client,
    resolve_space_id,
)
from ..context_keys import build_upload_context_key
from ..output import EXIT_NOT_OK, EXIT_SKIPPED, JSON_OPTION, apply_envelope, console, mention_prefix, print_json
from .apps import (
    APP_SPECS,
    _build_signal_metadata,
    _context_item_from_response,
    _default_signal_message,
    _is_passive_signal,
)

app = typer.Typer(name="qa", help="Regression and contract smoke checks", no_args_is_help=True)

_mention_prefix = mention_prefix


def _extract_items(payload: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_items(value, keys)
            if nested:
                return nested
    return []


def _count(payload: Any, keys: tuple[str, ...]) -> int:
    if isinstance(payload, dict):
        for key in ("total", "count", "total_count"):
            value = payload.get(key)
            if isinstance(value, int):
                return value
    return len(_extract_items(payload, keys))


def _http_error(exc: httpx.HTTPStatusError) -> dict[str, Any]:
    response = exc.response
    detail: Any
    try:
        detail = response.json()
    except Exception:
        detail = response.text[:500]
    return {
        "status_code": response.status_code,
        "url": str(response.request.url),
        "detail": detail,
    }


def _error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, httpx.HTTPStatusError):
        return _http_error(exc)
    return {
        "type": exc.__class__.__name__,
        "detail": str(exc),
    }


def _run_check(
    checks: list[dict[str, Any]],
    name: str,
    fn: Callable[[], Any],
    *,
    summarize: Callable[[Any], dict[str, Any]] | None = None,
) -> Any:
    started = time.monotonic()
    try:
        payload = fn()
    except Exception as exc:
        checks.append(
            {
                "name": name,
                "ok": False,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "error": _error_payload(exc),
            }
        )
        return None

    check = {
        "name": name,
        "ok": True,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }
    if summarize:
        check.update(summarize(payload))
    checks.append(check)
    return payload


def _summarize_collection(keys: tuple[str, ...]) -> Callable[[Any], dict[str, Any]]:
    def summarize(payload: Any) -> dict[str, Any]:
        return {"count": _count(payload, keys)}

    return summarize


def _normalize_upload(upload_data: dict[str, Any]) -> dict[str, Any]:
    raw = upload_data.get("attachment", upload_data)
    if not isinstance(raw, dict):
        raw = {}
    attachment_id = (
        raw.get("id")
        or raw.get("attachment_id")
        or raw.get("file_id")
        or upload_data.get("id")
        or upload_data.get("attachment_id")
        or ""
    )
    return {
        "attachment_id": str(attachment_id),
        "url": str(raw.get("url") or upload_data.get("url") or ""),
        "content_type": str(raw.get("content_type") or upload_data.get("content_type") or ""),
        "size": int(raw.get("size") or upload_data.get("size") or 0),
        "filename": str(
            raw.get("original_filename") or raw.get("filename") or upload_data.get("original_filename") or ""
        ),
    }


def _attachment_ref(info: dict[str, Any], *, context_key: str) -> dict[str, Any]:
    return {
        "id": info["attachment_id"],
        "filename": info["filename"],
        "content_type": info["content_type"],
        "size": info["size"],
        "size_bytes": info["size"],
        "url": info["url"],
        "kind": "file",
        "context_key": context_key,
    }


def _message_from_response(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        message = data.get("message")
        if isinstance(message, dict):
            return message
        return data
    return {}


def _message_id(data: Any) -> str | None:
    message = _message_from_response(data)
    value = message.get("id") or message.get("message_id")
    return str(value) if value else None


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _client_for_env(env_name: str) -> tuple[AxClient, dict[str, Any]]:
    """Return a user-authored client for a named login environment."""
    normalized = _normalize_user_env(env_name)
    cfg = _load_user_config(normalized)
    if not cfg:
        console.print(f"[red]No user login found for env '{normalized}'.[/red]")
        console.print(f"Run: axctl login --env {normalized} --url <base-url>")
        raise typer.Exit(1)

    token = str(cfg.get("token") or "")
    if not token:
        console.print(f"[red]User login env '{normalized}' has no token.[/red]")
        raise typer.Exit(1)
    if token.startswith("axp_a_"):
        console.print(f"[red]User login env '{normalized}' contains an agent PAT.[/red]")
        console.print("`--env` selects user-authored QA credentials. Use an agent profile for agent runtime QA.")
        raise typer.Exit(1)

    base_url = str(cfg.get("base_url") or "http://localhost:8001")
    return AxClient(base_url=base_url, token=token), {**cfg, "environment": normalized}


def _space_id_from_item(item: dict[str, Any]) -> str | None:
    value = item.get("id") or item.get("space_id")
    return str(value) if value else None


def _select_default_space_id(spaces_payload: Any) -> str | None:
    items = _extract_items(spaces_payload, ("spaces", "items", "results"))
    if len(items) == 1:
        return _space_id_from_item(items[0])

    for key in ("is_current", "current", "is_default", "default"):
        matches = [item for item in items if item.get(key) is True]
        if len(matches) == 1:
            return _space_id_from_item(matches[0])

    personal = [
        item
        for item in items
        if item.get("is_personal") is True or str(item.get("space_mode", "")).lower() == "personal"
    ]
    if len(personal) == 1:
        return _space_id_from_item(personal[0])
    return None


def _resolve_env_space_id(client: AxClient, env_cfg: dict[str, Any], *, explicit: str | None) -> str:
    if explicit:
        return explicit
    if env_cfg.get("space_id"):
        return str(env_cfg["space_id"])

    spaces_payload = client.list_spaces()
    selected = _select_default_space_id(spaces_payload)
    if selected:
        return selected

    count = _count(spaces_payload, ("spaces", "items", "results"))
    env_label = env_cfg.get("environment") or "selected"
    console.print(f"[red]No default space is configured for env '{env_label}'.[/red]")
    if count:
        console.print(
            f"{count} visible spaces found; pass --space-id or rerun axctl login --env {env_label} --space-id <id>."
        )
    else:
        console.print("No visible spaces found for this credential.")
    raise typer.Exit(1)


def _default_alert_target(whoami_payload: dict[str, Any]) -> str | None:
    bound_agent = whoami_payload.get("bound_agent") if isinstance(whoami_payload.get("bound_agent"), dict) else {}
    for value in (
        bound_agent.get("agent_name"),
        whoami_payload.get("resolved_agent"),
        whoami_payload.get("username"),
    ):
        if value:
            return str(value)
    return None


def _fixture_context_value(*, run_id: str, context_key: str, filename: str, content: str) -> dict[str, Any]:
    return {
        "type": "file_upload",
        "source": "axctl_qa_widgets",
        "context_key": context_key,
        "filename": filename,
        "content_type": "text/markdown",
        "size": len(content.encode()),
        "summary": f"QA widget fixture artifact {run_id}",
        "content": content,
        "file_content": content,
        "file_upload": {
            "filename": filename,
            "content_type": "text/markdown",
            "size": len(content.encode()),
            "context_key": context_key,
        },
    }


def _read_evidence_file(path: str, *, content_type_hint: str | None = None) -> tuple[str, str, str]:
    evidence_path = Path(path).expanduser().resolve()
    content_type = content_type_hint or mimetypes.guess_type(evidence_path.name)[0] or "application/octet-stream"
    is_text = content_type.startswith("text/") or content_type in {"application/json", "application/xml"}
    content = evidence_path.read_text(errors="replace") if is_text and evidence_path.stat().st_size <= 100_000 else ""
    return evidence_path.name, content_type, content


def _send_widget_fixture(
    *,
    client: AxClient,
    space_id: str,
    app_name: str,
    action: str,
    title: str,
    summary: str,
    channel: str,
    message: str | None = None,
    context_key: str | None = None,
    context_item: dict[str, Any] | None = None,
    whoami_payload: dict[str, Any] | None = None,
    collection_payload: Any | None = None,
    target: str | None = None,
    alert_kind: str | None = None,
    severity: str = "info",
    attachments: list[dict[str, Any]] | None = None,
    name: str | None = None,
    card_type: str | None = None,
    card_payload_updates: dict[str, Any] | None = None,
    widget_updates: dict[str, Any] | None = None,
    widget_initial_data_updates: dict[str, Any] | None = None,
    alert_updates: dict[str, Any] | None = None,
    expected: list[str] | None = None,
) -> dict[str, Any]:
    spec = APP_SPECS[app_name]
    metadata, tool_call_id = _build_signal_metadata(
        app_name=app_name,
        resource_uri=spec["resource_uri"],
        title=title,
        action=action,
        space_id=space_id,
        context_key=context_key,
        context_item=context_item,
        whoami_payload=whoami_payload,
        collection_payload=collection_payload,
        summary=summary,
        target=target,
        alert_kind=alert_kind,
        severity=severity,
    )
    if _is_passive_signal(to=target, alert_kind=alert_kind):
        metadata["top_level_ingress"] = False
        metadata["signal_only"] = True
        metadata["app_signal"]["signal_only"] = True

    ui = metadata.get("ui") if isinstance(metadata.get("ui"), dict) else {}
    cards = ui.get("cards") if isinstance(ui.get("cards"), list) else []
    card = cards[0] if cards and isinstance(cards[0], dict) else None
    widget = ui.get("widget") if isinstance(ui.get("widget"), dict) else None
    if card_type and card is not None:
        card["type"] = card_type
    if card_payload_updates and card is not None:
        payload = card.get("payload") if isinstance(card.get("payload"), dict) else {}
        payload.update({key: value for key, value in card_payload_updates.items() if value is not None})
        card["payload"] = payload
    if widget_updates and widget is not None:
        widget.update({key: value for key, value in widget_updates.items() if value is not None})
    if widget_initial_data_updates and widget is not None:
        initial_data = widget.get("initial_data") if isinstance(widget.get("initial_data"), dict) else {}
        initial_data = dict(initial_data)
        initial_data.update({key: value for key, value in widget_initial_data_updates.items() if value is not None})
        widget["initial_data"] = initial_data
    if alert_updates and isinstance(metadata.get("alert"), dict):
        metadata["alert"].update({key: value for key, value in alert_updates.items() if value is not None})

    body = message or _default_signal_message(title=title, summary=summary, context_key=context_key)
    prefix = _mention_prefix(target)
    if prefix:
        body = f"{prefix} {body}"

    data = client.send_message(
        space_id,
        body,
        channel=channel,
        metadata=metadata,
        message_type="system",
        attachments=attachments,
    )
    message_data = _message_from_response(data)
    return {
        "name": name or (app_name if not alert_kind else f"alert:{alert_kind}"),
        "message_id": _message_id(data),
        "title": title,
        "app": app_name,
        "resource_uri": spec["resource_uri"],
        "tool_call_id": tool_call_id,
        "context_key": context_key,
        "target": _mention_prefix(target).lstrip("@") or None,
        "alert_kind": alert_kind,
        "message_type": message_data.get("message_type") or "system",
        "expected": expected
        or [
            "one transcript object",
            "relative time in the card header",
            "card body remains selectable",
            "Share uses the connected-dots icon and seeds the composer",
            "Open launches the widget panel",
        ],
    }


def _run_widget_fixtures(
    *,
    space_id: str | None,
    channel: str,
    run_id: str | None,
    ttl: int,
    alert_to: str | None,
    create_task: bool,
    include_media_message: bool,
    evidence_file: str | None,
) -> dict[str, Any]:
    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)
    whoami_payload = client.whoami()
    resolved_run_id = run_id or f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    target = alert_to or _default_alert_target(whoami_payload)

    context_key = f"qa:widgets:{resolved_run_id}:artifact.md"
    filename = f"qa-widget-{resolved_run_id}.md"
    content = (
        f"# QA Widget Fixture\n\n"
        f"Run: `{resolved_run_id}`\n\n"
        "This artifact backs the Context widget and alert evidence fixtures.\n\n"
        "- Context Open should use the immersive artifact viewer.\n"
        "- Alert Open should route to Context, not Identity.\n"
        "- Details should hold raw ids; the card header should use relative time.\n"
    )
    attachment_refs: list[dict[str, Any]] = []
    if evidence_file:
        upload_info = _normalize_upload(client.upload_file(evidence_file, space_id=sid))
        context_key = build_upload_context_key(upload_info.get("filename") or filename, upload_info["attachment_id"])
        filename, content_type, content = _read_evidence_file(
            evidence_file,
            content_type_hint=upload_info.get("content_type") or None,
        )
        context_value: dict[str, Any] = {
            "type": "file_upload",
            "source": "axctl_qa_widgets",
            "attachment_id": upload_info["attachment_id"],
            "context_key": context_key,
            "filename": filename,
            "content_type": content_type or upload_info.get("content_type"),
            "size": upload_info.get("size"),
            "url": upload_info.get("url"),
            "summary": f"QA widget fixture evidence {resolved_run_id}",
            "file_upload": {
                "filename": filename,
                "content_type": content_type or upload_info.get("content_type"),
                "size": upload_info.get("size"),
                "context_key": context_key,
                "url": upload_info.get("url"),
            },
        }
        if content:
            context_value["content"] = content
            context_value["file_content"] = content
        attachment_refs.append(_attachment_ref(upload_info, context_key=context_key))
    else:
        context_value = _fixture_context_value(
            run_id=resolved_run_id,
            context_key=context_key,
            filename=filename,
            content=content,
        )

    client.set_context(sid, context_key, json.dumps(context_value), ttl=ttl)
    context_item = _context_item_from_response(context_key, client.get_context(context_key, space_id=sid))

    task_payload: Any | None = None
    created_task_id: str | None = None
    created_task: dict[str, Any] | None = None
    if create_task:
        task_data = client.create_task(
            sid,
            f"QA widget fixture task {resolved_run_id}",
            description=(
                "Generated by `axctl qa widgets` so the Tasks widget has a fresh, non-empty board during browser QA."
            ),
            priority="medium",
        )
        created_task = (
            task_data.get("task") if isinstance(task_data.get("task"), dict) else _message_from_response(task_data)
        )
        created_task_id = str(created_task.get("id") or created_task.get("task_id") or "") or None
    task_payload = client.list_tasks(limit=50, space_id=sid)
    task_items = _extract_items(task_payload, ("tasks", "items", "results"))
    task_detail = created_task or (task_items[0] if task_items else {})
    task_detail_id = _first_text(task_detail.get("id"), task_detail.get("task_id"), created_task_id, "qa-task")
    task_detail_title = _first_text(task_detail.get("title"), f"QA widget fixture task {resolved_run_id}")
    task_detail_summary = _first_text(
        task_detail.get("description"),
        task_detail.get("summary"),
        "Task detail widget should open from task, reminder, and notice cards.",
    )
    task_detail_initial_data = {
        "kind": "task",
        "version": 1,
        "action": "get",
        "selected_task_id": task_detail_id,
        "items": [
            {
                **task_detail,
                "id": task_detail_id,
                "task_id": task_detail_id,
                "title": task_detail_title,
                "summary": task_detail_summary,
                "description": task_detail_summary,
                "status": task_detail.get("status") or "open",
                "priority": task_detail.get("priority") or "medium",
            }
        ],
        "count": 1,
        "total": 1,
        "space_id": sid,
        "source": "axctl_qa_widgets",
    }
    agents_payload = client.list_agents(space_id=sid, limit=500)
    spaces_payload = client.list_spaces()

    fixtures: list[dict[str, Any]] = []
    fixtures.append(
        _send_widget_fixture(
            client=client,
            space_id=sid,
            app_name="whoami",
            action="get",
            title=f"QA whoami identity {resolved_run_id}",
            summary="Identity widget should hydrate the signal subject, not User / Unknown.",
            channel=channel,
            whoami_payload=whoami_payload,
        )
    )
    fixtures.append(
        _send_widget_fixture(
            client=client,
            space_id=sid,
            app_name="tasks",
            action="list",
            title=f"QA task board {resolved_run_id}",
            summary="Task board should hydrate from fresh task list initial_data.",
            channel=channel,
            collection_payload=task_payload,
        )
    )
    fixtures.append(
        _send_widget_fixture(
            client=client,
            space_id=sid,
            app_name="tasks/detail",
            action="get",
            title=f"QA task detail {resolved_run_id}",
            summary="Task card should show Share and the Open-widget icon.",
            channel=channel,
            collection_payload={"tasks": [task_detail_initial_data["items"][0]], "total": 1},
            name="task:detail",
            card_type="task",
            card_payload_updates={
                "task_id": task_detail_id,
                "title": task_detail_title,
                "summary": task_detail_summary,
                "status": task_detail.get("status") or "open",
                "priority": task_detail.get("priority") or "medium",
            },
            widget_initial_data_updates=task_detail_initial_data,
        )
    )
    fixtures.append(
        _send_widget_fixture(
            client=client,
            space_id=sid,
            app_name="tasks/detail",
            action="get",
            title=f"QA task reminder {resolved_run_id}",
            summary="Reminder should be one slim alert card with Open and Share.",
            channel=channel,
            collection_payload={"tasks": [task_detail_initial_data["items"][0]], "total": 1},
            target=target,
            alert_kind="task_reminder",
            severity="warn",
            name="alert:task_reminder",
            card_payload_updates={"task_id": task_detail_id},
            widget_initial_data_updates=task_detail_initial_data,
            alert_updates={
                "task_id": task_detail_id,
                "source_task_id": task_detail_id,
                "task_title": task_detail_title,
                "task_summary": task_detail_summary,
                "task_status": task_detail.get("status") or "open",
                "task_priority": task_detail.get("priority") or "medium",
                "reason": "QA reminder verifies task alerts open directly to the task detail widget.",
            },
        )
    )
    fixtures.append(
        _send_widget_fixture(
            client=client,
            space_id=sid,
            app_name="tasks/detail",
            action="get",
            title=f"QA task completion notice {resolved_run_id}",
            summary="Closure notice should be reviewable without looking like a duplicate task message.",
            channel=channel,
            collection_payload={"tasks": [task_detail_initial_data["items"][0]], "total": 1},
            alert_kind="task_completed",
            severity="info",
            name="notice:task_completed",
            card_payload_updates={"task_id": task_detail_id},
            widget_initial_data_updates={
                **task_detail_initial_data,
                "items": [
                    {
                        **task_detail_initial_data["items"][0],
                        "status": "done",
                    }
                ],
            },
            alert_updates={
                "task_id": task_detail_id,
                "source_task_id": task_detail_id,
                "task_title": task_detail_title,
                "task_summary": "QA completion notice for Activity Stream review.",
                "task_status": "done",
                "task_priority": task_detail.get("priority") or "medium",
            },
        )
    )
    fixtures.append(
        _send_widget_fixture(
            client=client,
            space_id=sid,
            app_name="agents",
            action="list",
            title=f"QA agents dashboard {resolved_run_id}",
            summary="Agents dashboard should show real agent rows without nested chrome.",
            channel=channel,
            collection_payload=agents_payload,
        )
    )
    fixtures.append(
        _send_widget_fixture(
            client=client,
            space_id=sid,
            app_name="spaces",
            action="list",
            title=f"QA spaces navigator {resolved_run_id}",
            summary="Spaces navigator should show real spaces and stable controls.",
            channel=channel,
            collection_payload=spaces_payload,
        )
    )
    fixtures.append(
        _send_widget_fixture(
            client=client,
            space_id=sid,
            app_name="agents",
            action="create_draft",
            title=f"QA approve agent creation {resolved_run_id}",
            summary="Review card should not render inline Approve/Deny; Open launches the draft widget.",
            channel=channel,
            collection_payload=agents_payload,
            name="review:agent_creation",
            card_type="confirmation",
            card_payload_updates={
                "title": f"Approve agent creation {resolved_run_id}",
                "summary": "Human review required. Open the widget to inspect and approve.",
                "action_id": f"qa-agent-create:{resolved_run_id}",
                "intent": "review",
                "evidence_mode": "widget",
                "status": "needs_review",
            },
            widget_updates={
                "tool_action": "create_draft",
                "lifecycle": "approval_required",
                "title": f"Approve agent creation {resolved_run_id}",
            },
            widget_initial_data_updates={
                "kind": "agents",
                "version": 1,
                "action": "create_draft",
                "state": "approval_required",
                "draft": {
                    "display_name": f"QA Demo Agent {resolved_run_id}",
                    "handle": f"qa_demo_{resolved_run_id.replace('-', '_')}"[:48],
                    "purpose": "QA-only draft for Activity Stream widget review.",
                },
                "summary": "Open the widget for approve/deny controls.",
            },
            expected=[
                "Review card is slim and has no inline Approve/Deny buttons",
                "Open-widget icon launches the HITL app panel",
                "Share uses the connected-dots icon",
            ],
        )
    )
    fixtures.append(
        _send_widget_fixture(
            client=client,
            space_id=sid,
            app_name="context",
            action="get",
            title=f"QA context artifact {resolved_run_id}",
            summary="Context Open should use the immersive artifact viewer.",
            channel=channel,
            context_key=context_key,
            context_item=context_item,
        )
    )
    fixtures.append(
        _send_widget_fixture(
            client=client,
            space_id=sid,
            app_name="context",
            action="get",
            title=f"QA alert evidence {resolved_run_id}",
            summary="Wake-up alert with context evidence; Open should route to Context.",
            channel=channel,
            context_key=context_key,
            context_item=context_item,
            target=target,
            alert_kind="qa_widget_smoke",
            severity="warn",
            attachments=attachment_refs or None,
        )
    )

    media_message_id: str | None = None
    if include_media_message:
        media_data = client.send_message(
            sid,
            (
                f"QA link/media sidecar {resolved_run_id}\n\n"
                "Plain URL should open in a new tab: https://example.com/\n"
                "YouTube should render as a sidecar: https://www.youtube.com/watch?v=jNQXAC9IVRw"
            ),
            channel=channel,
            metadata={"qa_fixture": {"kind": "link_media_sidecar", "run_id": resolved_run_id}},
            message_type="text",
        )
        media_message_id = _message_id(media_data)
        fixtures.append(
            {
                "name": "link_media_sidecar",
                "message_id": media_message_id,
                "title": f"QA link/media sidecar {resolved_run_id}",
                "app": None,
                "resource_uri": None,
                "tool_call_id": None,
                "context_key": None,
                "target": None,
                "alert_kind": None,
                "message_type": "text",
                "expected": [
                    "plain URL opens in a new tab",
                    "YouTube URL renders as a message sidecar",
                    "sidecar remains visible without expanding the message summary",
                ],
            }
        )

    result = {
        "ok": True,
        "command": "ax qa widgets",
        "space_id": sid,
        "channel": channel,
        "run_id": resolved_run_id,
        "context_key": context_key,
        "created_task_id": created_task_id,
        "alert_target": _mention_prefix(target).lstrip("@") or None,
        "fixtures": fixtures,
    }
    apply_envelope(
        result,
        summary={
            "command": "ax qa widgets",
            "space_id": sid,
            "channel": channel,
            "run_id": resolved_run_id,
            "fixtures": len(fixtures),
            "message_ids": [fixture["message_id"] for fixture in fixtures],
        },
        details=fixtures,
    )
    return result


def _base_url_env_label(base_url: str | None) -> str | None:
    if not base_url:
        return None
    host = base_url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    first = host.split(".", 1)[0].strip().lower()
    if first and first not in {"www"}:
        return first
    return None


def _configured_matrix_envs() -> list[str]:
    envs: list[str] = []
    users_dir = _global_config_dir() / "users"
    if users_dir.exists():
        for path in sorted(users_dir.glob("*/user.toml")):
            if path.parent.name.startswith("."):
                continue
            envs.append(path.parent.name)

    default_cfg = _load_user_config("default")
    if default_cfg:
        default_label = _base_url_env_label(str(default_cfg.get("base_url") or "")) or "default"
        if default_label not in envs:
            envs.append(default_label)
        elif "default" not in envs:
            envs.append("default")
    return envs


def _matrix_actual_env(requested: str) -> tuple[str, str]:
    """Return (display_label, env_name_to_load)."""
    normalized = _normalize_user_env(requested)
    if _user_config_path(normalized).exists():
        return normalized, normalized

    default_cfg = _load_user_config("default")
    default_label = _base_url_env_label(str(default_cfg.get("base_url") or "")) if default_cfg else None
    if default_cfg and normalized == default_label:
        return normalized, "default"
    return normalized, normalized


def _parse_space_overrides(values: list[str] | None) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            console.print(f"[red]Invalid --space value '{value}'. Expected env=space-id.[/red]")
            raise typer.Exit(1)
        env_name, space_id = value.split("=", 1)
        env_key = _normalize_user_env(env_name)
        if not space_id.strip():
            console.print(f"[red]Invalid --space value '{value}'. Space id cannot be empty.[/red]")
            raise typer.Exit(1)
        overrides[env_key] = space_id.strip()
    return overrides


def _check_summary(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not result:
        return []
    summary = []
    for check in result.get("checks", []):
        item = {
            "name": check.get("name"),
            "ok": bool(check.get("ok")),
        }
        if "count" in check:
            item["count"] = check["count"]
        if "error" in check:
            item["error"] = check["error"]
        summary.append(item)
    return summary


def _run_contracts(
    *,
    env_name: str | None,
    space_id: str | None,
    limit: int,
    write: bool,
    upload_file: str | None,
    send_message: bool,
    ttl: int,
    cleanup: bool,
) -> dict[str, Any]:
    selected_env: str | None = None
    if env_name:
        client, env_cfg = _client_for_env(env_name)
        selected_env = str(env_cfg.get("environment") or env_name)
        sid = _resolve_env_space_id(client, env_cfg, explicit=space_id)
    else:
        client = get_client()
        sid = resolve_space_id(client, explicit=space_id)
    checks: list[dict[str, Any]] = []

    whoami_payload = _run_check(checks, "auth.whoami", client.whoami)
    _run_check(
        checks, "spaces.list", client.list_spaces, summarize=_summarize_collection(("spaces", "items", "results"))
    )
    _run_check(checks, "spaces.get", lambda: client.get_space(sid))
    _run_check(
        checks,
        "spaces.members",
        lambda: client.list_space_members(sid),
        summarize=_summarize_collection(("members", "items", "results")),
    )
    _run_check(
        checks,
        "agents.list",
        lambda: client.list_agents(space_id=sid, limit=max(limit, 1)),
        summarize=_summarize_collection(("agents", "items", "results")),
    )
    _run_check(
        checks,
        "tasks.list",
        lambda: client.list_tasks(limit=max(limit, 1), space_id=sid),
        summarize=_summarize_collection(("tasks", "items", "results")),
    )
    _run_check(
        checks,
        "context.list",
        lambda: client.list_context(space_id=sid),
        summarize=_summarize_collection(("context", "items", "results")),
    )
    _run_check(
        checks,
        "messages.list",
        lambda: client.list_messages(limit=max(limit, 1), space_id=sid),
        summarize=_summarize_collection(("messages", "items", "results")),
    )

    artifacts: dict[str, Any] = {}

    if write:
        key = f"qa:{int(time.time())}:{uuid.uuid4().hex[:12]}"
        value = json.dumps(
            {
                "type": "qa_contract_probe",
                "source": "axctl qa contracts",
                "space_id": sid,
                "created_at_unix": int(time.time()),
            }
        )

        _run_check(checks, "context.set", lambda: client.set_context(sid, key, value, ttl=ttl))
        context_get = _run_check(checks, "context.get", lambda: client.get_context(key, space_id=sid))
        artifacts["context_key"] = key
        if context_get is not None and cleanup:
            _run_check(checks, "context.delete", lambda: client.delete_context(key, space_id=sid))

        if upload_file:
            path = Path(upload_file).expanduser().resolve()

            upload_info = _run_check(
                checks,
                "uploads.create",
                lambda: _normalize_upload(client.upload_file(str(path), space_id=sid)),
                summarize=lambda payload: {
                    "filename": payload.get("filename") or path.name,
                    "content_type": payload.get("content_type"),
                    "size": payload.get("size"),
                },
            )

            if isinstance(upload_info, dict) and upload_info.get("attachment_id"):
                filename = upload_info.get("filename") or path.name
                context_key = build_upload_context_key(filename, upload_info["attachment_id"])
                context_value = {
                    "type": "file_upload",
                    "source": "qa_contract_probe",
                    "attachment_id": upload_info["attachment_id"],
                    "context_key": context_key,
                    "filename": filename,
                    "content_type": upload_info.get("content_type"),
                    "size": upload_info.get("size"),
                    "url": upload_info.get("url"),
                }
                if path.stat().st_size <= 50_000 and str(upload_info.get("content_type", "")).startswith("text/"):
                    context_value["content"] = path.read_text(errors="replace")

                _run_check(
                    checks,
                    "uploads.context.set",
                    lambda: client.set_context(sid, context_key, json.dumps(context_value), ttl=ttl),
                )
                _run_check(checks, "uploads.context.get", lambda: client.get_context(context_key, space_id=sid))
                artifacts["upload_context_key"] = context_key
                artifacts["attachment_id"] = upload_info["attachment_id"]

                if send_message:
                    message_content = f"QA upload contract probe: `{filename}` (context: `{context_key}`)"
                    message = _run_check(
                        checks,
                        "uploads.message.send",
                        lambda: client.send_message(
                            sid,
                            message_content,
                            attachments=[_attachment_ref(upload_info, context_key=context_key)],
                        ),
                    )
                    if isinstance(message, dict):
                        artifacts["message_id"] = message.get("id") or message.get("message", {}).get("id")

                if cleanup:
                    _run_check(
                        checks,
                        "uploads.context.delete",
                        lambda: client.delete_context(context_key, space_id=sid),
                    )

    ok = all(check["ok"] for check in checks)
    failed_checks = [check for check in checks if not check["ok"]]
    result = {
        "ok": ok,
        "environment": selected_env,
        "space_id": sid,
        "principal": {
            "username": whoami_payload.get("username") if isinstance(whoami_payload, dict) else None,
            "principal_type": whoami_payload.get("principal_type") if isinstance(whoami_payload, dict) else None,
            "bound_agent": whoami_payload.get("bound_agent") if isinstance(whoami_payload, dict) else None,
        },
        "mode": "write" if write else "read_only",
        "artifacts": artifacts,
        "checks": checks,
    }
    apply_envelope(
        result,
        summary={
            "command": "ax qa contracts",
            "environment": selected_env,
            "space_id": sid,
            "principal_type": result["principal"]["principal_type"],
            "mode": result["mode"],
            "checks_total": len(checks),
            "checks_failed": len(failed_checks),
            "failed_checks": [check["name"] for check in failed_checks],
        },
        details=checks,
    )
    return result


def _write_artifact(path: str | Path, result: dict[str, Any]) -> Path:
    artifact_path = Path(path).expanduser().resolve()
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return artifact_path


def _preflight_result(
    *,
    target: str,
    env_name: str | None,
    space_id: str | None,
    limit: int,
    write: bool,
    upload_file: str | None,
    send_message: bool,
    ttl: int,
    cleanup: bool,
) -> dict[str, Any]:
    result = _run_contracts(
        env_name=env_name,
        space_id=space_id,
        limit=limit,
        write=write,
        upload_file=upload_file,
        send_message=send_message,
        ttl=ttl,
        cleanup=cleanup,
    )
    result["preflight"] = {
        "target": target,
        "passed": bool(result.get("ok")),
        "generated_at_unix": int(time.time()),
        "command": "ax qa preflight",
    }
    result["summary"].update(
        {
            "command": "ax qa preflight",
            "target": target,
            "preflight_passed": bool(result.get("ok")),
        }
    )
    return result


def _emit_result(result: dict[str, Any], *, as_json: bool, artifact_path: Path | None = None) -> None:
    ok = bool(result.get("ok"))
    if as_json:
        print_json(result)
    else:
        console.print(f"[bold]aX contract smoke:[/bold] {'PASS' if ok else 'FAIL'}")
        console.print(f"space_id={result.get('space_id')} mode={result.get('mode')}")
        for check in result.get("checks", []):
            status = "[green]PASS[/green]" if check["ok"] else "[red]FAIL[/red]"
            suffix = f" count={check['count']}" if "count" in check else ""
            console.print(f"  {status} {check['name']} ({check['duration_ms']}ms){suffix}")
            if not check["ok"]:
                console.print(f"    [red]{check['error']}[/red]")
        if artifact_path:
            console.print(f"artifact={artifact_path}")

    if result.get("skipped"):
        raise typer.Exit(EXIT_SKIPPED)
    if not ok:
        raise typer.Exit(EXIT_NOT_OK)


@app.command("contracts")
def contracts(
    env_name: Optional[str] = typer.Option(
        None,
        "--env",
        help="Use a named user-login environment created with `axctl login --env`",
    ),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override target space"),
    limit: int = typer.Option(10, "--limit", help="Small collection read limit"),
    write: bool = typer.Option(False, "--write", help="Run mutating round-trip checks"),
    upload_file: Optional[str] = typer.Option(None, "--upload-file", help="Upload this file during write checks"),
    send_message: bool = typer.Option(False, "--send-message", help="Send a visible QA message for upload checks"),
    ttl: int = typer.Option(300, "--ttl", help="TTL for temporary context writes"),
    cleanup: bool = typer.Option(True, "--cleanup/--keep", help="Delete temporary context keys after write checks"),
    as_json: bool = JSON_OPTION,
):
    """Run API-first smoke checks against the active environment.

    Default mode is read-only. Use --write when validating dev/staging flows
    that create temporary context, upload files, or emit visible message
    signals.
    """
    result = _run_contracts(
        env_name=env_name,
        space_id=space_id,
        limit=limit,
        write=write,
        upload_file=upload_file,
        send_message=send_message,
        ttl=ttl,
        cleanup=cleanup,
    )
    _emit_result(result, as_json=as_json)


@app.command("preflight")
def preflight(
    target: str = typer.Option(
        "mcp-ui",
        "--for",
        help="Target being gated, e.g. mcp-jam, widget, playwright, ui",
    ),
    env_name: Optional[str] = typer.Option(
        None,
        "--env",
        help="Use a named user-login environment created with `axctl login --env`",
    ),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override target space"),
    limit: int = typer.Option(10, "--limit", help="Small collection read limit"),
    write: bool = typer.Option(False, "--write", help="Run mutating round-trip checks"),
    upload_file: Optional[str] = typer.Option(None, "--upload-file", help="Upload this file during write checks"),
    send_message: bool = typer.Option(False, "--send-message", help="Send a visible QA message for upload checks"),
    ttl: int = typer.Option(300, "--ttl", help="TTL for temporary context writes"),
    cleanup: bool = typer.Option(True, "--cleanup/--keep", help="Delete temporary context keys after write checks"),
    artifact: Optional[str] = typer.Option(
        None,
        "--artifact",
        help="Write the preflight result JSON to this path for CI/MCP/UI wrappers",
    ),
    as_json: bool = JSON_OPTION,
):
    """Gate MCP Jam, widget, or Playwright checks on API-first contracts."""
    result = _preflight_result(
        target=target,
        env_name=env_name,
        space_id=space_id,
        limit=limit,
        write=write,
        upload_file=upload_file,
        send_message=send_message,
        ttl=ttl,
        cleanup=cleanup,
    )
    artifact_path = Path(artifact).expanduser().resolve() if artifact else None
    if artifact_path:
        result["preflight"]["artifact"] = str(artifact_path)
        _write_artifact(artifact_path, result)
    _emit_result(result, as_json=as_json, artifact_path=artifact_path)


@app.command("widgets")
def widgets(
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override target space"),
    channel: str = typer.Option("main", "--channel", help="Message channel for visible fixture messages"),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Stable fixture run id; defaults to timestamp-random"),
    ttl: int = typer.Option(86400, "--ttl", help="TTL for generated context evidence"),
    alert_to: Optional[str] = typer.Option(
        None,
        "--alert-to",
        "--to",
        help="Agent/user to wake with the alert fixture; defaults to the active bound agent when available",
    ),
    create_task: bool = typer.Option(
        True,
        "--create-task/--no-create-task",
        help="Create one QA task before emitting the task-board fixture so the board is non-empty",
    ),
    include_media_message: bool = typer.Option(
        True,
        "--media-message/--no-media-message",
        help="Also send a normal message containing a plain URL and YouTube URL for sidecar QA",
    ),
    evidence_file: Optional[str] = typer.Option(
        None,
        "--evidence-file",
        help="Optional local file to upload and attach to the alert/context evidence fixture",
    ),
    as_json: bool = JSON_OPTION,
):
    """Generate fresh browser-QA fixtures for the current widget/signal contract."""
    result = _run_widget_fixtures(
        space_id=space_id,
        channel=channel,
        run_id=run_id,
        ttl=ttl,
        alert_to=alert_to,
        create_task=create_task,
        include_media_message=include_media_message,
        evidence_file=evidence_file,
    )
    if as_json:
        print_json(result)
    else:
        console.print(f"[green]Widget QA fixtures sent.[/green] run_id={result['run_id']}")
        console.print(f"space_id={result['space_id']} channel={result['channel']}")
        for fixture in result["fixtures"]:
            console.print(f"  {fixture['name']}: {fixture['message_id']}")


@app.command("matrix")
def matrix(
    env_names: Optional[list[str]] = typer.Option(
        None,
        "--env",
        "-e",
        help="Named user-login environment to check. Repeatable. Defaults to all configured user logins.",
    ),
    target: str = typer.Option(
        "mcp-ui",
        "--for",
        help="Target being gated, e.g. mcp-jam, widget, playwright, ui",
    ),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Use this space for every environment"),
    space_overrides: Optional[list[str]] = typer.Option(
        None,
        "--space",
        help="Per-env space override as env=space-id. Repeatable.",
    ),
    limit: int = typer.Option(10, "--limit", help="Small collection read limit"),
    artifact_dir: Optional[str] = typer.Option(
        None,
        "--artifact-dir",
        help="Write one preflight JSON artifact per environment into this directory",
    ),
    as_json: bool = JSON_OPTION,
):
    """Run auth doctor plus QA preflight across environments."""
    requested_envs = list(env_names or _configured_matrix_envs())
    if not requested_envs:
        result = {
            "ok": False,
            "target": target,
            "generated_at_unix": int(time.time()),
            "envs": [],
        }
        apply_envelope(
            result,
            skipped=True,
            summary={
                "command": "ax qa matrix",
                "target": target,
                "reason": "no configured user-login environments found",
                "env_count": 0,
            },
            details=[],
        )
        if artifact_dir:
            matrix_path = Path(artifact_dir).expanduser().resolve() / "matrix.json"
            result["artifact_path"] = str(matrix_path)
            _write_artifact(matrix_path, result)
        if as_json:
            print_json(result)
        else:
            console.print(
                "[yellow]No user-login environments found.[/yellow] Run axctl login --env dev --url <base-url>."
            )
        raise typer.Exit(EXIT_SKIPPED)

    space_map = _parse_space_overrides(space_overrides)
    rows: list[dict[str, Any]] = []
    artifact_root = Path(artifact_dir).expanduser().resolve() if artifact_dir else None
    matrix_started = int(time.time())

    for requested in requested_envs:
        label, actual_env = _matrix_actual_env(requested)
        selected_space = space_map.get(label) or space_map.get(actual_env) or space_id
        doctor = diagnose_auth_config(env_name=actual_env, explicit_space_id=selected_space)
        effective = doctor.get("effective", {})
        preflight_payload: dict[str, Any] | None = None
        preflight_error: dict[str, Any] | None = None
        artifact_path: Path | None = None

        if doctor.get("ok"):
            try:
                preflight_payload = _preflight_result(
                    target=target,
                    env_name=actual_env,
                    space_id=selected_space,
                    limit=limit,
                    write=False,
                    upload_file=None,
                    send_message=False,
                    ttl=300,
                    cleanup=True,
                )
                if artifact_root:
                    artifact_path = artifact_root / f"{label}-preflight.json"
                    preflight_payload["preflight"]["artifact"] = str(artifact_path)
                    _write_artifact(artifact_path, preflight_payload)
            except typer.Exit as exc:
                preflight_error = {"type": "Exit", "code": exc.exit_code}
            except Exception as exc:
                preflight_error = _error_payload(exc)

        row = {
            "env": label,
            "requested_env": requested,
            "selected_env": doctor.get("selected_env"),
            "principal_intent": effective.get("principal_intent"),
            "auth_source": effective.get("auth_source"),
            "base_url": effective.get("base_url"),
            "host": effective.get("host"),
            "space_id": effective.get("space_id"),
            "warnings": doctor.get("warnings", []),
            "doctor_ok": bool(doctor.get("ok")),
            "doctor_problems": doctor.get("problems", []),
            "preflight_ok": bool(preflight_payload and preflight_payload.get("ok")),
            "artifact_path": str(artifact_path) if artifact_path else None,
            "preflight_error": preflight_error,
            "checks": _check_summary(preflight_payload),
        }
        rows.append(row)

    result = {
        "ok": all(row["doctor_ok"] and row["preflight_ok"] for row in rows),
        "target": target,
        "generated_at_unix": matrix_started,
        "envs": rows,
    }
    failed_rows = [row for row in rows if not row["doctor_ok"] or not row["preflight_ok"]]
    apply_envelope(
        result,
        summary={
            "command": "ax qa matrix",
            "target": target,
            "env_count": len(rows),
            "failed_envs": [row["env"] for row in failed_rows],
            "warnings": sum(len(row.get("warnings", [])) for row in rows),
        },
        details=rows,
    )

    if artifact_root:
        matrix_path = artifact_root / "matrix.json"
        result["artifact_path"] = str(matrix_path)
        _write_artifact(matrix_path, result)

    if as_json:
        print_json(result)
    else:
        table = Table(show_header=True)
        table.add_column("Env")
        table.add_column("Intent")
        table.add_column("Host")
        table.add_column("Space")
        table.add_column("Auth")
        table.add_column("Doctor")
        table.add_column("Preflight")
        table.add_column("Warnings")
        for row in rows:
            table.add_row(
                str(row["env"]),
                str(row["principal_intent"]),
                str(row["host"]),
                str(row["space_id"]),
                str(row["auth_source"]),
                "PASS" if row["doctor_ok"] else "FAIL",
                "PASS" if row["preflight_ok"] else "FAIL",
                str(len(row["warnings"])),
            )
        console.print(table)
        if artifact_root:
            console.print(f"artifact_dir={artifact_root}")

    if result.get("skipped"):
        raise typer.Exit(EXIT_SKIPPED)
    if not result["ok"]:
        raise typer.Exit(EXIT_NOT_OK)
