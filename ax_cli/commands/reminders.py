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
    _fetch_task_snapshot,
    _format_mention_content,
    _normalize_severity,
    _resolve_target_from_task,
    _strip_at,
    _task_lifecycle,
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


_LOOP_MODES = ("auto", "draft", "manual")
_DEFAULT_PRIORITY = 50


def _empty_store() -> dict[str, Any]:
    return {"version": 2, "policies": [], "drafts": []}


def _normalize_mode(value: str | None) -> str:
    text = (value or "auto").strip().lower()
    if text not in _LOOP_MODES:
        raise typer.BadParameter(f"--mode must be one of: {', '.join(_LOOP_MODES)}")
    return text


def _normalize_priority(value: int | None) -> int:
    if value is None:
        return _DEFAULT_PRIORITY
    if value < 0 or value > 100:
        raise typer.BadParameter("--priority must be between 0 and 100 (lower = higher priority)")
    return int(value)


def _short_draft_id() -> str:
    return f"draft-{uuid.uuid4().hex[:10]}"


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
    data.setdefault("drafts", [])
    if not isinstance(data["policies"], list):
        typer.echo(f"Error: reminders policies must be a list: {path}", err=True)
        raise typer.Exit(1)
    if not isinstance(data["drafts"], list):
        typer.echo(f"Error: reminders drafts must be a list: {path}", err=True)
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


def _policy_sort_key(policy: dict[str, Any]) -> tuple:
    """Priority queue order: priority asc (lower = higher), then next_fire asc, then id."""
    priority = int(policy.get("priority", _DEFAULT_PRIORITY))
    next_fire = str(policy.get("next_fire_at") or "")
    pol_id = str(policy.get("id") or "")
    return (priority, next_fire, pol_id)


def _policy_rows(store: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    sorted_policies = sorted(
        (p for p in store.get("policies", []) if isinstance(p, dict)),
        key=_policy_sort_key,
    )
    for policy in sorted_policies:
        rows.append(
            {
                "id": policy.get("id", ""),
                "priority": int(policy.get("priority", _DEFAULT_PRIORITY)),
                "mode": str(policy.get("mode", "auto")),
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
    priority: int = typer.Option(_DEFAULT_PRIORITY, "--priority", help="Queue priority 0-100 (lower = higher)"),
    mode: str = typer.Option("auto", "--mode", help="auto | draft | manual"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    policy_file: Optional[str] = typer.Option(None, "--file", help="Reminder policy JSON file"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Add a local reminder policy.

    The policy is local state. Use ``ax reminders run`` to fire due policies.
    Mode controls firing behavior:
      auto   — fire immediately when due (default)
      draft  — prepare draft, queue for HITL review via ``ax reminders drafts``
      manual — never auto-fire; only fired by explicit ``run --force``
    """
    if max_fires < 1:
        raise typer.BadParameter("--max-fires must be at least 1")
    if cadence < 1:
        raise typer.BadParameter("--cadence-minutes must be at least 1")
    if first_in < 0:
        raise typer.BadParameter("--first-in-minutes cannot be negative")
    normalized_priority = _normalize_priority(priority)
    normalized_mode = _normalize_mode(mode)

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
        "priority": normalized_priority,
        "mode": normalized_mode,
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
        ["ID", "Pri", "Mode", "Enabled", "Task", "Target", "Next Fire", "Fires", "Reason"],
        rows,
        keys=["id", "priority", "mode", "enabled", "task", "target", "next_fire", "fires", "reason"],
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


def _build_fire_payload(client: Any, policy: dict[str, Any], *, now: _dt.datetime) -> dict[str, Any] | None:
    """Build target/reason/content/metadata for a due policy.

    Returns None if the policy must be skipped (e.g. source task is terminal —
    side-effect: marks the policy disabled). Otherwise returns a dict with
    keys: target, target_resolved_from, content, metadata, channel.
    """
    source_task = str(policy.get("source_task_id") or "")
    reason = str(policy.get("reason") or "Please review this task.")
    target = _strip_at(policy.get("target"))
    target_resolved_from = None

    lifecycle = _task_lifecycle(client, source_task) if source_task else None

    if lifecycle and lifecycle.get("is_terminal"):
        policy["enabled"] = False
        policy["disabled_reason"] = f"source task {source_task} is {lifecycle.get('status')}"
        policy["updated_at"] = _iso(now)
        policy["_skip_reason"] = f"source_task_terminal:{lifecycle.get('status')}"
        return None

    if lifecycle and lifecycle.get("is_pending_review"):
        review_target = lifecycle.get("review_owner") or lifecycle.get("creator_name")
        if review_target:
            target = review_target
            target_resolved_from = "review_owner" if lifecycle.get("review_owner") else "creator_fallback"
            reason = f"[pending review] {reason}"
        elif not target:
            target, target_resolved_from = (lifecycle.get("assignee_name"), "assignee")
    elif source_task and not target:
        if lifecycle and lifecycle.get("assignee_name"):
            target, target_resolved_from = lifecycle["assignee_name"], "assignee"
        elif lifecycle and lifecycle.get("creator_name"):
            target, target_resolved_from = lifecycle["creator_name"], "creator"
        else:
            target, target_resolved_from = _resolve_target_from_task(client, source_task)

    try:
        triggered_by = resolve_agent_name(client=client)
    except Exception:
        triggered_by = None

    task_snapshot = (
        lifecycle.get("snapshot")
        if lifecycle and lifecycle.get("snapshot")
        else (_fetch_task_snapshot(client, source_task) if source_task else None)
    )

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
        task_snapshot=task_snapshot,
    )
    metadata["reminder_policy"] = {
        "policy_id": policy.get("id"),
        "fire_key": policy.get("_current_fire_key"),
        "cadence_seconds": policy.get("cadence_seconds"),
        "fired_count": policy.get("fired_count", 0) + 1,
        "max_fires": policy.get("max_fires"),
        "target_resolved_from": target_resolved_from,
        "mode": str(policy.get("mode", "auto")),
    }

    return {
        "target": target,
        "target_resolved_from": target_resolved_from,
        "content": _format_mention_content(target, reason, "reminder"),
        "metadata": metadata,
        "channel": str(policy.get("channel") or "main"),
        "fired_at": fired_at,
    }


def _fire_policy(
    client: Any,
    policy: dict[str, Any],
    *,
    now: _dt.datetime,
    drafts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fire a due policy according to its mode.

    - auto:   send immediately (existing behavior)
    - draft:  build payload, append to drafts, do NOT send
    - manual: skip (manual policies should not appear in due_policies; safety net)
    """
    payload = _build_fire_payload(client, policy, now=now)
    if payload is None:
        # Skipped — _build_fire_payload populated _skip_reason on the policy.
        skip_reason = str(policy.pop("_skip_reason", "skipped"))
        return {
            "policy_id": policy.get("id"),
            "skipped": True,
            "reason": skip_reason,
            "source_task_id": str(policy.get("source_task_id") or ""),
            "fired_at": None,
        }

    mode = str(policy.get("mode", "auto"))

    if mode == "manual":
        return {
            "policy_id": policy.get("id"),
            "skipped": True,
            "reason": "manual_mode",
            "fired_at": None,
        }

    if mode == "draft":
        if drafts is None:
            return {
                "policy_id": policy.get("id"),
                "error": "draft mode requires a drafts store; pass --file to a v2 store",
            }
        draft = {
            "id": _short_draft_id(),
            "policy_id": policy.get("id"),
            "fire_key": policy.get("_current_fire_key"),
            "created_at": payload["fired_at"],
            "target": payload["target"],
            "target_resolved_from": payload["target_resolved_from"],
            "content": payload["content"],
            "metadata": payload["metadata"],
            "channel": payload["channel"],
            "space_id": str(policy.get("space_id") or ""),
            "status": "pending",
        }
        drafts.append(draft)
        return {
            "policy_id": policy.get("id"),
            "draft_id": draft["id"],
            "drafted": True,
            "target": payload["target"],
            "fired_at": payload["fired_at"],
        }

    # mode == "auto"
    result = client.send_message(
        str(policy.get("space_id")),
        payload["content"],
        channel=payload["channel"],
        metadata=payload["metadata"],
        message_type="reminder",
    )
    message = result.get("message", result) if isinstance(result, dict) else {}
    return {
        "policy_id": policy.get("id"),
        "message_id": message.get("id"),
        "target": payload["target"],
        "target_resolved_from": payload["target_resolved_from"],
        "fired_at": payload["fired_at"],
    }


def _due_policies(store: dict[str, Any], *, now: _dt.datetime, include_manual: bool = False) -> list[dict[str, Any]]:
    """Return enabled, due policies in priority queue order.

    Manual-mode policies are excluded by default; pass ``include_manual=True``
    to include them (e.g. for an explicit ``run --force <id>`` path).
    """
    due = []
    for policy in store.get("policies", []):
        if not isinstance(policy, dict) or not policy.get("enabled", True):
            continue
        if not include_manual and str(policy.get("mode", "auto")) == "manual":
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
    due.sort(key=_policy_sort_key)
    return due


def _advance_policy(
    policy: dict[str, Any],
    *,
    now: _dt.datetime,
    message_id: str | None,
    draft_id: str | None = None,
) -> None:
    """Advance a policy after a successful fire (auto-sent or drafted).

    Drafted fires DO advance fired_count and next_fire_at — drafts are
    real fires from the loop's perspective. The HITL send/cancel does not
    re-tick the policy.
    """
    fire_key = str(policy.pop("_current_fire_key", ""))
    fired_keys = list(policy.get("fired_keys") or [])
    if fire_key:
        fired_keys.append(fire_key)
    policy["fired_keys"] = fired_keys[-50:]
    policy["fired_count"] = int(policy.get("fired_count", 0)) + 1
    policy["last_fired_at"] = _iso(now)
    policy["last_message_id"] = message_id
    policy["last_draft_id"] = draft_id
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
        drafts_list = store.setdefault("drafts", [])
        for policy in _due_policies(store, now=now):
            try:
                result = _fire_policy(client, policy, now=now, drafts=drafts_list)
            except httpx.HTTPStatusError as exc:
                result = {
                    "policy_id": policy.get("id"),
                    "error": f"{exc.response.status_code} {exc.response.text[:200]}",
                }
            except (httpx.ConnectError, httpx.ReadError) as exc:
                result = {"policy_id": policy.get("id"), "error": str(exc)}
            if not result.get("error") and not result.get("skipped"):
                _advance_policy(
                    policy,
                    now=now,
                    message_id=result.get("message_id"),
                    draft_id=result.get("draft_id"),
                )
            pass_results.append(result)
            all_results.append(result)
        _save_store(path, store)

        if once:
            if as_json:
                print_json({"file": str(path), "fired": all_results})
            elif pass_results:
                rows = []
                for item in pass_results:
                    if item.get("error"):
                        status = f"error: {item['error'][:40]}"
                    elif item.get("skipped"):
                        status = f"skipped ({item.get('reason', '')})"
                    elif item.get("drafted"):
                        status = f"drafted: {item.get('draft_id')}"
                    elif item.get("message_id"):
                        status = f"sent: {item['message_id']}"
                    else:
                        status = "fired"
                    rows.append(
                        {
                            "policy_id": item.get("policy_id"),
                            "status": status,
                            "target": item.get("target"),
                            "fired_at": item.get("fired_at"),
                        }
                    )
                print_table(
                    ["Policy", "Status", "Target", "Fired At"],
                    rows,
                    keys=["policy_id", "status", "target", "fired_at"],
                )
            else:
                console.print(f"No due reminders in {path}")
            return

        if pass_results and not as_json:
            for item in pass_results:
                if item.get("error"):
                    console.print(f"[red]{item['policy_id']}[/red]: {item['error']}")
                elif item.get("skipped"):
                    reason = item.get("reason") or "skipped"
                    console.print(f"[yellow]{item['policy_id']}[/yellow] skipped ({reason})")
                elif item.get("drafted"):
                    console.print(
                        f"[cyan]{item['policy_id']}[/cyan] drafted "
                        f"draft={item.get('draft_id')} target={item.get('target')}"
                    )
                else:
                    console.print(
                        f"[green]{item['policy_id']}[/green] fired "
                        f"message={item.get('message_id')} target={item.get('target')}"
                    )
        time.sleep(interval)


# ---- Operator commands: pause / resume / cancel / update -------------------


@app.command("pause")
def pause(
    policy_id: str = typer.Argument(..., help="Policy ID or unique prefix"),
    policy_file: Optional[str] = typer.Option(None, "--file", help="Reminder policy JSON file"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Pause a reminder policy. Use ``resume`` to re-enable."""
    path = _policy_file(policy_file)
    store = _load_store(path)
    policy = _find_policy(store, policy_id)
    policy["enabled"] = False
    policy["disabled_reason"] = "paused"
    policy["updated_at"] = _iso(_now())
    _save_store(path, store)
    if as_json:
        print_json({"policy": policy, "file": str(path)})
        return
    console.print(f"Paused reminder policy {policy['id']}")


@app.command("resume")
def resume(
    policy_id: str = typer.Argument(..., help="Policy ID or unique prefix"),
    policy_file: Optional[str] = typer.Option(None, "--file", help="Reminder policy JSON file"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Resume a paused reminder policy.

    Refuses to resume policies that finished (max_fires reached) or were
    auto-disabled because the source task is terminal.
    """
    path = _policy_file(policy_file)
    store = _load_store(path)
    policy = _find_policy(store, policy_id)
    if int(policy.get("fired_count", 0)) >= int(policy.get("max_fires", 1)):
        typer.echo(f"Error: policy {policy['id']} has reached max_fires; create a new policy", err=True)
        raise typer.Exit(1)
    disabled_reason = str(policy.get("disabled_reason") or "")
    if disabled_reason.startswith("source task"):
        typer.echo(f"Error: source task is terminal; refusing to resume {policy['id']}", err=True)
        raise typer.Exit(1)
    policy["enabled"] = True
    policy.pop("disabled_reason", None)
    policy["updated_at"] = _iso(_now())
    _save_store(path, store)
    if as_json:
        print_json({"policy": policy, "file": str(path)})
        return
    console.print(f"Resumed reminder policy {policy['id']}")


@app.command("cancel")
def cancel(
    policy_id: str = typer.Argument(..., help="Policy ID or unique prefix"),
    policy_file: Optional[str] = typer.Option(None, "--file", help="Reminder policy JSON file"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Cancel a reminder policy permanently. Like ``disable`` but with explicit cancel reason."""
    path = _policy_file(policy_file)
    store = _load_store(path)
    policy = _find_policy(store, policy_id)
    policy["enabled"] = False
    policy["disabled_reason"] = "cancelled"
    policy["updated_at"] = _iso(_now())
    _save_store(path, store)
    if as_json:
        print_json({"policy": policy, "file": str(path)})
        return
    console.print(f"Cancelled reminder policy {policy['id']}")


@app.command("update")
def update_policy(
    policy_id: str = typer.Argument(..., help="Policy ID or unique prefix"),
    priority: Optional[int] = typer.Option(None, "--priority", help="New priority (0-100, lower = higher)"),
    cadence: Optional[int] = typer.Option(None, "--cadence-minutes", help="New cadence in minutes"),
    max_fires: Optional[int] = typer.Option(None, "--max-fires", help="New max-fires cap"),
    mode: Optional[str] = typer.Option(None, "--mode", help="auto | draft | manual"),
    reason: Optional[str] = typer.Option(None, "--reason", help="New reason text"),
    target: Optional[str] = typer.Option(None, "--target", help="New target @agent/user"),
    policy_file: Optional[str] = typer.Option(None, "--file", help="Reminder policy JSON file"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Update fields on a reminder policy. ``--priority`` re-orders the queue."""
    path = _policy_file(policy_file)
    store = _load_store(path)
    policy = _find_policy(store, policy_id)
    if priority is not None:
        policy["priority"] = _normalize_priority(priority)
    if mode is not None:
        policy["mode"] = _normalize_mode(mode)
    if cadence is not None:
        if cadence < 1:
            raise typer.BadParameter("--cadence-minutes must be at least 1")
        policy["cadence_seconds"] = cadence * 60
    if max_fires is not None:
        if max_fires < 1:
            raise typer.BadParameter("--max-fires must be at least 1")
        policy["max_fires"] = max_fires
    if reason is not None:
        policy["reason"] = reason
    if target is not None:
        policy["target"] = _strip_at(target)
    policy["updated_at"] = _iso(_now())
    _save_store(path, store)
    if as_json:
        print_json({"policy": policy, "file": str(path)})
        return
    console.print(f"Updated reminder policy {policy['id']}")


# ---- Drafts subcommand group: list / show / edit / send / cancel ----------

drafts_app = typer.Typer(name="drafts", help="HITL drafts queued by draft-mode policies", no_args_is_help=True)
app.add_typer(drafts_app, name="drafts")


def _find_draft(store: dict[str, Any], draft_id: str) -> dict[str, Any]:
    matches = [
        d
        for d in store.get("drafts", [])
        if isinstance(d, dict) and str(d.get("id", "")).startswith(draft_id) and d.get("status") == "pending"
    ]
    if not matches:
        typer.echo(f"Error: pending draft not found: {draft_id}", err=True)
        raise typer.Exit(1)
    if len(matches) > 1:
        typer.echo(f"Error: draft id is ambiguous: {draft_id}", err=True)
        raise typer.Exit(1)
    return matches[0]


@drafts_app.command("list")
def drafts_list(
    policy_file: Optional[str] = typer.Option(None, "--file", help="Reminder policy JSON file"),
    as_json: bool = JSON_OPTION,
) -> None:
    """List pending HITL drafts."""
    path = _policy_file(policy_file)
    store = _load_store(path)
    pending = [d for d in store.get("drafts", []) if isinstance(d, dict) and d.get("status") == "pending"]
    if as_json:
        print_json({"file": str(path), "drafts": pending})
        return
    if not pending:
        console.print(f"No pending drafts in {path}")
        return
    rows = [
        {
            "id": d.get("id", ""),
            "policy": d.get("policy_id", ""),
            "target": d.get("target") or "(none)",
            "created_at": d.get("created_at", ""),
            "preview": (d.get("content") or "")[:60],
        }
        for d in pending
    ]
    print_table(
        ["ID", "Policy", "Target", "Created", "Preview"],
        rows,
        keys=["id", "policy", "target", "created_at", "preview"],
    )


@drafts_app.command("show")
def drafts_show(
    draft_id: str = typer.Argument(..., help="Draft ID or unique prefix"),
    policy_file: Optional[str] = typer.Option(None, "--file", help="Reminder policy JSON file"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Show a pending draft's full body and metadata."""
    path = _policy_file(policy_file)
    store = _load_store(path)
    draft = _find_draft(store, draft_id)
    if as_json:
        print_json({"draft": draft, "file": str(path)})
        return
    console.print(f"[bold]{draft['id']}[/bold] (policy={draft.get('policy_id')})")
    console.print(f"[bold]target[/bold]: {draft.get('target')}")
    console.print(f"[bold]channel[/bold]: {draft.get('channel')}")
    console.print(f"[bold]created[/bold]: {draft.get('created_at')}")
    console.print()
    console.print(draft.get("content", ""))


@drafts_app.command("edit")
def drafts_edit(
    draft_id: str = typer.Argument(..., help="Draft ID or unique prefix"),
    body: Optional[str] = typer.Option(None, "--body", help="New message body"),
    target: Optional[str] = typer.Option(None, "--target", help="New target @agent/user"),
    policy_file: Optional[str] = typer.Option(None, "--file", help="Reminder policy JSON file"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Edit a pending draft before sending."""
    if body is None and target is None:
        raise typer.BadParameter("--body and/or --target required")
    path = _policy_file(policy_file)
    store = _load_store(path)
    draft = _find_draft(store, draft_id)
    if target is not None:
        draft["target"] = _strip_at(target)
        # Re-mention prefix the body if it doesn't already lead with @target
        if body is None and draft.get("content"):
            existing = str(draft["content"])
            # strip the old @mention if present
            if existing.startswith("@"):
                existing_body = existing.split(" ", 1)[1] if " " in existing else ""
            else:
                existing_body = existing
            draft["content"] = _format_mention_content(draft["target"], existing_body, "reminder")
    if body is not None:
        draft["content"] = _format_mention_content(draft.get("target"), body, "reminder")
    draft["edited"] = True
    draft["updated_at"] = _iso(_now())
    _save_store(path, store)
    if as_json:
        print_json({"draft": draft, "file": str(path)})
        return
    console.print(f"Edited draft {draft['id']}")


@drafts_app.command("send")
def drafts_send(
    draft_id: str = typer.Argument(..., help="Draft ID or unique prefix"),
    policy_file: Optional[str] = typer.Option(None, "--file", help="Reminder policy JSON file"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Send a pending draft via the messages API."""
    path = _policy_file(policy_file)
    store = _load_store(path)
    draft = _find_draft(store, draft_id)
    client = get_client()
    try:
        result = client.send_message(
            str(draft.get("space_id")),
            str(draft.get("content") or ""),
            channel=str(draft.get("channel") or "main"),
            metadata=draft.get("metadata") or {},
            message_type="reminder",
        )
    except httpx.HTTPStatusError as exc:
        typer.echo(f"Error: send failed: {exc.response.status_code} {exc.response.text[:200]}", err=True)
        raise typer.Exit(1)
    message = result.get("message", result) if isinstance(result, dict) else {}
    draft["status"] = "sent"
    draft["sent_at"] = _iso(_now())
    draft["message_id"] = message.get("id")
    _save_store(path, store)
    if as_json:
        print_json({"draft": draft, "message_id": message.get("id"), "file": str(path)})
        return
    console.print(f"Sent draft {draft['id']} (message {message.get('id')})")


@drafts_app.command("cancel")
def drafts_cancel(
    draft_id: str = typer.Argument(..., help="Draft ID or unique prefix"),
    policy_file: Optional[str] = typer.Option(None, "--file", help="Reminder policy JSON file"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Cancel a pending draft. Does NOT re-tick the source policy."""
    path = _policy_file(policy_file)
    store = _load_store(path)
    draft = _find_draft(store, draft_id)
    draft["status"] = "cancelled"
    draft["cancelled_at"] = _iso(_now())
    _save_store(path, store)
    if as_json:
        print_json({"draft": draft, "file": str(path)})
        return
    console.print(f"Cancelled draft {draft['id']}")
