"""ax qa — API-first regression smoke checks."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import typer

from ..config import get_client, resolve_space_id
from ..context_keys import build_upload_context_key
from ..output import JSON_OPTION, console, print_json

app = typer.Typer(name="qa", help="Regression and contract smoke checks", no_args_is_help=True)


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
        "filename": str(raw.get("original_filename") or raw.get("filename") or upload_data.get("original_filename") or ""),
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


@app.command("contracts")
def contracts(
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
    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)
    checks: list[dict[str, Any]] = []

    whoami_payload = _run_check(checks, "auth.whoami", client.whoami)
    _run_check(checks, "spaces.list", client.list_spaces, summarize=_summarize_collection(("spaces", "items", "results")))
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
    result = {
        "ok": ok,
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

    if as_json:
        print_json(result)
    else:
        console.print(f"[bold]aX contract smoke:[/bold] {'PASS' if ok else 'FAIL'}")
        console.print(f"space_id={sid} mode={result['mode']}")
        for check in checks:
            status = "[green]PASS[/green]" if check["ok"] else "[red]FAIL[/red]"
            suffix = f" count={check['count']}" if "count" in check else ""
            console.print(f"  {status} {check['name']} ({check['duration_ms']}ms){suffix}")
            if not check["ok"]:
                console.print(f"    [red]{check['error']}[/red]")

    if not ok:
        raise typer.Exit(1)
