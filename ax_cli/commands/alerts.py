"""ax alerts — fire Activity Stream alert/reminder cards via message metadata.

First-slice MVP (task dfef4c92): a thin wrapper over POST /api/v1/messages that
builds a ``metadata.alert`` + ``metadata.ui.cards[]`` envelope the existing
frontend already renders as an AlertCardBody. No backend schema changes; no
scheduler dependency; manual fire only.

Design notes:
- The card type is "alert" so AxMessageWidgets.getCardChrome picks the
  ShieldAlert accent and AlertCardBody renders the alert detail block.
- We keep reminder metadata compact — no task-board widget initial_data.
  A clickable source_task_id link is enough for the first demo.
- State transitions (ack/resolve) post a REPLY to the original alert with
  ``metadata.alert_state_change``. Backend PATCH only accepts ``content``
  today — metadata updates are silently dropped — so state-change-as-reply
  keeps the slice honest and produces an auditable stream event. A small
  frontend follow-up can fold the reply into the parent card's state badge.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from typing import Any, Optional

import httpx
import typer

from ..config import get_client, resolve_agent_name, resolve_space_id
from ..output import JSON_OPTION, console, print_json, print_kv


def _fail(message: str, *, exit_code: int = 1) -> None:
    """Print an error and exit — alerts.py's own handle_error variant
    that accepts a string (the shared ``handle_error`` only wraps
    httpx.HTTPStatusError instances)."""
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(exit_code)


def _print_kv(data: dict, *, title: str | None = None) -> None:
    """print_kv with optional title prefix."""
    if title:
        console.print(f"[bold cyan]{title}[/bold cyan]")
    print_kv(data)

app = typer.Typer(name="alerts", help="Activity Stream alerts and task reminders", no_args_is_help=True)


_ALLOWED_SEVERITIES = {"info", "warn", "warning", "critical", "error"}
_ALLOWED_KINDS = {"alert", "reminder", "task_reminder"}
_ALLOWED_STATES = {"triggered", "acknowledged", "resolved", "stale", "escalated"}


def _normalize_severity(value: str) -> str:
    value = (value or "info").strip().lower()
    if value == "warning":
        return "warn"
    if value == "error":
        return "critical"
    if value not in _ALLOWED_SEVERITIES:
        raise typer.BadParameter("severity must be one of: info, warn, critical")
    return value


def _normalize_kind(value: str) -> str:
    value = (value or "alert").strip().lower()
    if value == "task_reminder":
        value = "reminder"
    if value not in {"alert", "reminder"}:
        raise typer.BadParameter("kind must be 'alert' or 'reminder'")
    return value


def _normalize_state(value: str) -> str:
    value = (value or "triggered").strip().lower()
    if value not in _ALLOWED_STATES:
        raise typer.BadParameter(
            f"state must be one of: {', '.join(sorted(_ALLOWED_STATES))}"
        )
    return value


def _strip_at(target: str | None) -> str | None:
    if not target:
        return None
    return target.strip().lstrip("@") or None


def _iso_utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _build_alert_metadata(
    *,
    kind: str,
    severity: str,
    target: str | None,
    reason: str,
    source_task_id: str | None,
    due_at: str | None,
    remind_at: str | None,
    expected_response: str | None,
    response_required: bool,
    evidence: str | None,
    triggered_by_agent: str | None,
    title: str | None,
    state: str = "triggered",
) -> dict[str, Any]:
    """Build the ``metadata`` block the frontend's AlertCardBody reads.

    Shape mirrors the dogfood message 1942cc2c but with the compact
    reminder fields ChatGPT flagged (source_task_id, due_at, remind_at,
    state) and no task-board widget hydration.
    """
    card_title = title or (
        f"Reminder: {reason[:80]}" if kind == "reminder" else f"Alert: {reason[:80]}"
    )
    fired_at = _iso_utc_now()
    card_id = f"alert:{uuid.uuid4()}"

    alert: dict[str, Any] = {
        "kind": "task_reminder" if kind == "reminder" else "alert",
        "severity": severity,
        "source": "axctl_alerts",
        "state": state,
        "fired_at": fired_at,
        "title": card_title,
        "summary": reason,
        "reason": reason,
        "response_required": response_required,
    }
    if target:
        alert["target_agent"] = target
        alert["target"] = target
    if source_task_id:
        alert["source_task_id"] = source_task_id
    if due_at:
        alert["due_at"] = due_at
    if remind_at:
        alert["remind_at"] = remind_at
    if expected_response:
        alert["expected_response"] = expected_response
    if evidence:
        alert["context_key"] = evidence
    if triggered_by_agent:
        alert["triggered_by_agent_name"] = triggered_by_agent

    card_payload: dict[str, Any] = {
        "title": card_title,
        "summary": reason,
        "severity": severity,
        "alert": alert,
        "intent": "alert",
    }
    if source_task_id:
        card_payload["source_task_id"] = source_task_id
        card_payload["resource_uri"] = f"ui://tasks/{source_task_id}"

    return {
        "alert": alert,
        "ui": {
            "cards": [
                {
                    "card_id": card_id,
                    "type": "alert",
                    "version": 1,
                    "payload": card_payload,
                }
            ]
        },
    }


def _format_mention_content(target: str | None, reason: str, kind: str) -> str:
    label = "Reminder" if kind == "reminder" else "Alert"
    prefix = f"@{target} " if target else ""
    return f"{prefix}{label}: {reason}"


@app.command("send")
def send(
    reason: str = typer.Argument(..., help="Short human-readable reason / summary"),
    target: Optional[str] = typer.Option(None, "--target", "-t", help="@agent or username (no @ needed)"),
    severity: str = typer.Option("info", "--severity", "-s", help="info | warn | critical"),
    kind: str = typer.Option("alert", "--kind", "-k", help="alert | reminder"),
    source_task: Optional[str] = typer.Option(None, "--source-task", help="Linked task id (clickable in card)"),
    due_at: Optional[str] = typer.Option(None, "--due-at", help="ISO-8601 due timestamp (reminder)"),
    remind_at: Optional[str] = typer.Option(None, "--remind-at", help="ISO-8601 remind-at timestamp (reminder)"),
    expected_response: Optional[str] = typer.Option(None, "--expected-response", help="What response is expected"),
    response_required: bool = typer.Option(False, "--response-required", help="Mark response as required"),
    evidence: Optional[str] = typer.Option(None, "--evidence", help="Context key / URL pointing at evidence"),
    title: Optional[str] = typer.Option(None, "--title", help="Override card title (defaults to reason)"),
    channel: str = typer.Option("main", "--channel", "-c", help="Channel (default: main)"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Fire an alert or reminder into the Activity Stream.

    Examples:

        ax alerts send "dev ALB regressed on /auth/me" --target @orion --severity critical
        ax alerts send "review needed" --kind reminder --source-task dfef4c92 --remind-at 2026-04-16T17:00Z
    """
    severity_n = _normalize_severity(severity)
    kind_n = _normalize_kind(kind)
    target_n = _strip_at(target)

    if kind_n == "reminder" and not source_task:
        raise typer.BadParameter("--source-task is required for --kind reminder")

    client = get_client()
    try:
        resolved_space = resolve_space_id(client, explicit=space_id)
    except Exception as exc:
        _fail(f"Space ID not resolvable: {exc}. Pass --space-id or configure default.", exit_code=2)

    try:
        triggered_by = resolve_agent_name(client=client)
    except Exception:
        triggered_by = None

    metadata = _build_alert_metadata(
        kind=kind_n,
        severity=severity_n,
        target=target_n,
        reason=reason,
        source_task_id=source_task,
        due_at=due_at,
        remind_at=remind_at,
        expected_response=expected_response,
        response_required=response_required,
        evidence=evidence,
        triggered_by_agent=triggered_by,
        title=title,
    )

    content = _format_mention_content(target_n, reason, kind_n)

    try:
        result = client.send_message(
            resolved_space,
            content,
            channel=channel,
            metadata=metadata,
            message_type="alert" if kind_n == "alert" else "reminder",
        )
    except httpx.HTTPStatusError as exc:
        _fail(f"send failed: {exc.response.status_code} {exc.response.text[:300]}", exit_code=1)
    except (httpx.ConnectError, httpx.ReadError) as exc:
        _fail(f"cannot reach aX API: {exc}", exit_code=1)

    if as_json:
        print_json(result)
        return

    # Response is either {"id": ...} or {"message": {"id": ...}}
    msg: dict[str, Any] = (
        result.get("message", result) if isinstance(result, dict) else {}
    )
    _print_kv(
        {
            "id": msg.get("id", "?"),
            "kind": kind_n,
            "severity": severity_n,
            "target": target_n or "-",
            "source_task": source_task or "-",
            "state": "triggered",
        },
        title=f"{'Reminder' if kind_n == 'reminder' else 'Alert'} fired",
    )


@app.command("reminder")
def reminder(
    reason: str = typer.Argument(..., help="Short reminder text"),
    source_task: str = typer.Option(..., "--source-task", help="Linked task id (required)"),
    target: Optional[str] = typer.Option(None, "--target", "-t", help="@agent or username"),
    severity: str = typer.Option("info", "--severity", "-s", help="info | warn | critical"),
    due_at: Optional[str] = typer.Option(None, "--due-at", help="ISO-8601 due timestamp"),
    remind_at: Optional[str] = typer.Option(None, "--remind-at", help="ISO-8601 remind-at timestamp"),
    evidence: Optional[str] = typer.Option(None, "--evidence", help="Context key / URL"),
    channel: str = typer.Option("main", "--channel", "-c", help="Channel"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Shortcut for ``ax alerts send --kind reminder``."""
    # Delegate to send() with kind=reminder
    send(  # type: ignore[call-arg]
        reason=reason,
        target=target,
        severity=severity,
        kind="reminder",
        source_task=source_task,
        due_at=due_at,
        remind_at=remind_at,
        expected_response=None,
        response_required=False,
        evidence=evidence,
        title=None,
        channel=channel,
        space_id=space_id,
        as_json=as_json,
    )


def _post_state_change(message_id: str, new_state: str, *, as_json: bool = False) -> None:
    """Post a state-change *reply* to an existing alert message.

    The backend's message PATCH endpoint (``MessageEditBody``) only accepts
    ``content`` — metadata updates are silently dropped. So for the first
    MVP slice we treat state transitions as first-class stream events: a
    reply-message whose ``metadata.alert_state_change`` references the
    parent alert. The frontend can fold these into the parent card's
    state badge on render (that's a small follow-up PR).

    This keeps the slice honest about the backend constraint while still
    producing an auditable, streamable state-change event.
    """
    new_state = _normalize_state(new_state)
    client = get_client()

    try:
        r = client._http.get(
            f"/api/v1/messages/{message_id}",
            headers=client._with_agent(None),
        )
        r.raise_for_status()
        parent_wrapper = client._parse_json(r)
    except httpx.HTTPStatusError as exc:
        _fail(f"fetch parent failed: {exc.response.status_code}", exit_code=1)
    except (httpx.ConnectError, httpx.ReadError) as exc:
        _fail(f"cannot reach aX API: {exc}", exit_code=1)

    parent = parent_wrapper.get("message", parent_wrapper) if isinstance(parent_wrapper, dict) else {}
    parent_metadata = parent.get("metadata") or {}
    parent_alert = parent_metadata.get("alert") or {}
    if not parent_alert:
        _fail(f"message {message_id} has no metadata.alert — not an alert", exit_code=1)

    parent_space = parent.get("space_id")
    if not parent_space:
        _fail(f"message {message_id} has no space_id", exit_code=1)

    now = _iso_utc_now()
    parent_kind = parent_alert.get("kind", "alert")
    previous_state = parent_alert.get("state", "triggered")
    state_change_metadata = {
        "alert_state_change": {
            "parent_message_id": message_id,
            "new_state": new_state,
            "previous_state": previous_state,
            "changed_at": now,
            "kind": parent_kind,
        },
        "alert": {
            # Mirror as a lightweight alert so existing card renderers that
            # key on metadata.alert still pick up the transition as an event.
            "kind": "alert_state_change",
            "severity": parent_alert.get("severity", "info"),
            "state": new_state,
            "source": "axctl_alerts",
            "parent_message_id": message_id,
            "fired_at": now,
            "title": f"{parent_kind} → {new_state}",
            "summary": f"State changed from {previous_state} to {new_state}",
        },
    }

    content = f"[{parent_kind} → {new_state}]"

    try:
        result = client.send_message(
            parent_space,
            content,
            parent_id=message_id,
            metadata=state_change_metadata,
            message_type="alert_state_change",
        )
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:300]
        hint = ""
        if "Cannot reply to your own message" in detail:
            hint = (
                "\nHint: alerts are acked/resolved by the *recipient*, not the firer. "
                "Run this command as the target agent (or the user)."
            )
        _fail(
            f"state-change post failed: {exc.response.status_code} {detail}{hint}",
            exit_code=1,
        )
    except (httpx.ConnectError, httpx.ReadError) as exc:
        _fail(f"cannot reach aX API: {exc}", exit_code=1)

    if as_json:
        print_json(result)
        return

    reply = result.get("message", result) if isinstance(result, dict) else {}
    _print_kv(
        {
            "parent": message_id,
            "reply": reply.get("id", "?"),
            "new_state": new_state,
        },
        title=f"Alert state → {new_state} (posted as reply)",
    )


@app.command("ack")
def ack(
    message_id: str = typer.Argument(..., help="Alert message ID"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Acknowledge an alert (state → acknowledged).

    Semantics: the *recipient* of an alert acks it, not the firer. Running
    this on an alert you sent will fail with "Cannot reply to your own
    message" — run it as the targeted agent or user instead.

    Today this posts a state-change reply linked to the parent alert
    because the backend PATCH endpoint drops metadata updates. Once
    247f7bf0 lands (backend accepts metadata on PATCH), this becomes
    an in-place state transition.
    """
    _post_state_change(message_id, "acknowledged", as_json=as_json)


@app.command("resolve")
def resolve(
    message_id: str = typer.Argument(..., help="Alert message ID"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Resolve an alert (state → resolved).

    Semantics: the recipient (or an authorized responder) resolves —
    not the firer. See ``ax alerts ack --help`` for the full note.
    """
    _post_state_change(message_id, "resolved", as_json=as_json)


@app.command("state")
def set_state(
    message_id: str = typer.Argument(..., help="Alert message ID"),
    new_state: str = typer.Argument(..., help="triggered | acknowledged | resolved | stale | escalated"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Set an arbitrary state on an existing alert.

    Subject to the same recipient-acks-not-firer rule as ``ack``/``resolve``.
    """
    _post_state_change(message_id, new_state, as_json=as_json)
