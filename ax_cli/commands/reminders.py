"""Local reminder policy runner.

This is intentionally a CLI-first dogfood loop. It stores reminder policy
state in a local JSON file, then emits Activity Stream reminder cards through
the existing ``ax alerts`` metadata contract when policies become due.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
import typer

from ..config import get_client, resolve_agent_name, resolve_space_id
from ..output import JSON_OPTION, console, print_json, print_table
from .alerts import (
    _build_alert_metadata,
    _format_mention_content,
    _normalize_severity,
    _resolve_target_from_task,
    _strip_at,
    _validate_timestamp,
)

app = typer.Typer(name="reminders", help="Local task reminder policy runner", no_args_is_help=True)


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def _iso(value: _dt.datetime) -> str:
    return value.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> _dt.datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = _dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


def _default_policy_file() -> Path:
    env_path = os.environ.get("AX_REMINDERS_FILE")
    if env_path:
        return Path(env_path).expanduser()

    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        ax_dir = parent / ".ax"
        if ax_dir.is_dir():
            return ax_dir / "reminders.json"
    return Path.home() / ".ax" / "reminders.json"


def _policy_file(path: str | None) -> Path:
    return Path(path).expanduser() if path else _default_policy_file()


def _empty_store() -> dict[str, Any]:
    return {"version": 1, "policies": []}


def _load_store(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_store()
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        typer.echo(f"Error: reminder policy file is not valid JSON: {path} ({exc})", err=True)
        raise typer.Exit(1)
    if not isinstance(data, dict):
        typer.echo(f"Error: reminder policy file must contain a JSON object: {path}", err=True)
        raise typer.Exit(1)
    data.setdefault("version", 1)
    data.setdefault("policies", [])
    if not isinstance(data["policies"], list):
        typer.echo(f"Error: reminders policies must be a list: {path}", err=True)
        raise typer.Exit(1)
    return data


def _save_store(path: Path, store: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)
    path.chmod(0o600)


def _short_id() -> str:
    return f"rem-{uuid.uuid4().hex[:10]}"


def _find_policy(store: dict[str, Any], policy_id: str) -> dict[str, Any]:
    matches = [
        p for p in store.get("policies", []) if isinstance(p, dict) and str(p.get("id", "")).startswith(policy_id)
    ]
    if not matches:
        typer.echo(f"Error: reminder policy not found: {policy_id}", err=True)
        raise typer.Exit(1)
    if len(matches) > 1:
        typer.echo(f"Error: reminder policy id is ambiguous: {policy_id}", err=True)
        raise typer.Exit(1)
    return matches[0]


def _policy_rows(store: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for policy in store.get("policies", []):
        if not isinstance(policy, dict):
            continue
        rows.append(
            {
                "id": policy.get("id", ""),
                "enabled": policy.get("enabled", True),
                "task": policy.get("source_task_id", ""),
                "target": policy.get("target") or "(task default)",
                "next_fire": policy.get("next_fire_at", ""),
                "fires": f"{policy.get('fired_count', 0)}/{policy.get('max_fires', '-')}",
                "reason": policy.get("reason", ""),
            }
        )
    return rows


@app.command("add")
def add(
    source_task: str = typer.Argument(..., help="Task ID to remind about"),
    reason: str = typer.Option("Please review this task.", "--reason", "-r", help="Reminder text"),
    target: Optional[str] = typer.Option(None, "--target", "-t", help="@agent/user; default resolves from task"),
    first_at: Optional[str] = typer.Option(None, "--first-at", help="First fire time, ISO-8601 UTC"),
    first_in: int = typer.Option(5, "--first-in-minutes", help="Minutes from now for first fire"),
    cadence: int = typer.Option(5, "--cadence-minutes", help="Minutes between recurring fires"),
    max_fires: int = typer.Option(1, "--max-fires", help="Maximum reminder fires before disabling"),
    severity: str = typer.Option("info", "--severity", "-s", help="info | warn | critical"),
    expected_response: Optional[str] = typer.Option(None, "--expected-response", help="What response is expected"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    policy_file: Optional[str] = typer.Option(None, "--file", help="Reminder policy JSON file"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Add a local reminder policy.

    The policy is local state. Use ``ax reminders run`` to fire due policies.
    """
    if max_fires < 1:
        raise typer.BadParameter("--max-fires must be at least 1")
    if cadence < 1:
        raise typer.BadParameter("--cadence-minutes must be at least 1")
    if first_in < 0:
        raise typer.BadParameter("--first-in-minutes cannot be negative")

    first_at = _validate_timestamp(first_at, flag="--first-at")
    next_fire = _parse_iso(first_at) if first_at else _now() + _dt.timedelta(minutes=first_in)

    client = get_client()
    try:
        resolved_space = resolve_space_id(client, explicit=space_id)
    except Exception as exc:
        typer.echo(f"Error: Space ID not resolvable: {exc}. Pass --space-id or configure default.", err=True)
        raise typer.Exit(2)

    path = _policy_file(policy_file)
    store = _load_store(path)
    policy = {
        "id": _short_id(),
        "enabled": True,
        "space_id": resolved_space,
        "source_task_id": source_task,
        "reason": reason,
        "target": _strip_at(target),
        "severity": _normalize_severity(severity),
        "expected_response": expected_response,
        "cadence_seconds": cadence * 60,
        "next_fire_at": _iso(next_fire),
        "max_fires": max_fires,
        "fired_count": 0,
        "fired_keys": [],
        "created_at": _iso(_now()),
        "updated_at": _iso(_now()),
    }
    store["policies"].append(policy)
    _save_store(path, store)

    if as_json:
        print_json({"policy": policy, "file": str(path)})
        return

    console.print(f"[bold cyan]Reminder policy added[/bold cyan] {policy['id']}")
    console.print(f"[bold]file[/bold]: {path}")
    console.print(f"[bold]next_fire_at[/bold]: {policy['next_fire_at']}")


@app.command("list")
def list_policies(
    policy_file: Optional[str] = typer.Option(None, "--file", help="Reminder policy JSON file"),
    as_json: bool = JSON_OPTION,
) -> None:
    """List local reminder policies."""
    path = _policy_file(policy_file)
    store = _load_store(path)
    if as_json:
        print_json({"file": str(path), "policies": store.get("policies", [])})
        return
    rows = _policy_rows(store)
    if not rows:
        console.print(f"No reminder policies in {path}")
        return
    print_table(
        ["ID", "Enabled", "Task", "Target", "Next Fire", "Fires", "Reason"],
        rows,
        keys=["id", "enabled", "task", "target", "next_fire", "fires", "reason"],
    )


@app.command("disable")
def disable(
    policy_id: str = typer.Argument(..., help="Policy ID or unique prefix"),
    policy_file: Optional[str] = typer.Option(None, "--file", help="Reminder policy JSON file"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Disable a local reminder policy."""
    path = _policy_file(policy_file)
    store = _load_store(path)
    policy = _find_policy(store, policy_id)
    policy["enabled"] = False
    policy["updated_at"] = _iso(_now())
    _save_store(path, store)
    if as_json:
        print_json({"policy": policy, "file": str(path)})
        return
    console.print(f"Disabled reminder policy {policy['id']}")


def _fire_policy(client: Any, policy: dict[str, Any], *, now: _dt.datetime) -> dict[str, Any]:
    source_task = str(policy.get("source_task_id") or "")
    reason = str(policy.get("reason") or "Please review this task.")
    target = _strip_at(policy.get("target"))
    target_resolved_from = None
    if source_task and not target:
        target, target_resolved_from = _resolve_target_from_task(client, source_task)

    try:
        triggered_by = resolve_agent_name(client=client)
    except Exception:
        triggered_by = None

    fired_at = _iso(now)
    metadata = _build_alert_metadata(
        kind="reminder",
        severity=str(policy.get("severity") or "info"),
        target=target,
        reason=reason,
        source_task_id=source_task,
        due_at=policy.get("due_at"),
        remind_at=fired_at,
        expected_response=policy.get("expected_response"),
        response_required=True,
        evidence=policy.get("evidence"),
        triggered_by_agent=triggered_by,
        title=policy.get("title"),
    )
    metadata["reminder_policy"] = {
        "policy_id": policy.get("id"),
        "fire_key": policy.get("_current_fire_key"),
        "cadence_seconds": policy.get("cadence_seconds"),
        "fired_count": policy.get("fired_count", 0) + 1,
        "max_fires": policy.get("max_fires"),
        "target_resolved_from": target_resolved_from,
    }

    result = client.send_message(
        str(policy.get("space_id")),
        _format_mention_content(target, reason, "reminder"),
        channel=str(policy.get("channel") or "main"),
        metadata=metadata,
        message_type="reminder",
    )
    message = result.get("message", result) if isinstance(result, dict) else {}
    return {
        "policy_id": policy.get("id"),
        "message_id": message.get("id"),
        "target": target,
        "target_resolved_from": target_resolved_from,
        "fired_at": fired_at,
    }


def _due_policies(store: dict[str, Any], *, now: _dt.datetime) -> list[dict[str, Any]]:
    due = []
    for policy in store.get("policies", []):
        if not isinstance(policy, dict) or not policy.get("enabled", True):
            continue
        if int(policy.get("fired_count", 0)) >= int(policy.get("max_fires", 1)):
            policy["enabled"] = False
            policy["updated_at"] = _iso(now)
            continue
        try:
            next_fire = _parse_iso(str(policy.get("next_fire_at")))
        except Exception:
            policy["enabled"] = False
            policy["disabled_reason"] = "invalid next_fire_at"
            policy["updated_at"] = _iso(now)
            continue
        if next_fire <= now:
            fire_key = f"{policy.get('id')}:{policy.get('next_fire_at')}"
            if fire_key in set(policy.get("fired_keys") or []):
                continue
            policy["_current_fire_key"] = fire_key
            due.append(policy)
    return due


def _advance_policy(policy: dict[str, Any], *, now: _dt.datetime, message_id: str | None) -> None:
    fire_key = str(policy.pop("_current_fire_key", ""))
    fired_keys = list(policy.get("fired_keys") or [])
    if fire_key:
        fired_keys.append(fire_key)
    policy["fired_keys"] = fired_keys[-50:]
    policy["fired_count"] = int(policy.get("fired_count", 0)) + 1
    policy["last_fired_at"] = _iso(now)
    policy["last_message_id"] = message_id
    policy["updated_at"] = _iso(now)

    max_fires = int(policy.get("max_fires", 1))
    if policy["fired_count"] >= max_fires:
        policy["enabled"] = False
        policy["disabled_reason"] = "max_fires reached"
        return
    cadence_seconds = int(policy.get("cadence_seconds", 300))
    policy["next_fire_at"] = _iso(now + _dt.timedelta(seconds=cadence_seconds))


@app.command("run")
def run(
    once: bool = typer.Option(False, "--once", help="Run one due-policy pass and exit"),
    watch: bool = typer.Option(False, "--watch", help="Keep running due-policy passes"),
    interval: int = typer.Option(30, "--interval", help="Seconds between watch passes"),
    policy_file: Optional[str] = typer.Option(None, "--file", help="Reminder policy JSON file"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Fire due local reminder policies.

    Use ``--once`` for cron-like execution. Use ``--watch`` for dogfood loops.
    """
    if not once and not watch:
        once = True
    if interval < 1:
        raise typer.BadParameter("--interval must be at least 1 second")

    path = _policy_file(policy_file)
    all_results: list[dict[str, Any]] = []
    client = get_client()

    while True:
        store = _load_store(path)
        now = _now()
        pass_results: list[dict[str, Any]] = []
        for policy in _due_policies(store, now=now):
            try:
                result = _fire_policy(client, policy, now=now)
            except httpx.HTTPStatusError as exc:
                result = {
                    "policy_id": policy.get("id"),
                    "error": f"{exc.response.status_code} {exc.response.text[:200]}",
                }
            except (httpx.ConnectError, httpx.ReadError) as exc:
                result = {"policy_id": policy.get("id"), "error": str(exc)}
            if not result.get("error"):
                _advance_policy(policy, now=now, message_id=result.get("message_id"))
            pass_results.append(result)
            all_results.append(result)
        _save_store(path, store)

        if once:
            if as_json:
                print_json({"file": str(path), "fired": all_results})
            elif pass_results:
                print_table(
                    ["Policy", "Message", "Target", "Fired At"],
                    pass_results,
                    keys=["policy_id", "message_id", "target", "fired_at"],
                )
            else:
                console.print(f"No due reminders in {path}")
            return

        if pass_results and not as_json:
            for item in pass_results:
                if item.get("error"):
                    console.print(f"[red]{item['policy_id']}[/red]: {item['error']}")
                else:
                    console.print(
                        f"[green]{item['policy_id']}[/green] fired "
                        f"message={item.get('message_id')} target={item.get('target')}"
                    )
        time.sleep(interval)
