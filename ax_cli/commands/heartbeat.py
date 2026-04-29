"""Local-first heartbeat primitive.

Per madtank 2026-04-25: connectedness is one of three urgent primitives
(gateway / connectedness / registry). Heartbeats are the foundation —
each agent declares its own cadence; routing asks "did it meet its own
cadence within tolerance?" Decouples "alive" from "replied."

Design:
- Local store at ``~/.ax/heartbeats.json``: cadence, current status,
  history, push state.
- ``ax heartbeat send``: post to ``/api/v1/agents/heartbeat`` AND save
  locally. Offline-first: on network error, queue locally with
  ``pushed=false`` and mark a ``last_push_error``.
- ``ax heartbeat status``: surface online/offline + last_sent + next_due
  + queued count, structured for tooling.
- ``ax heartbeat watch``: tick at cadence, send each tick.
- ``ax heartbeat push``: drain queued (unpushed) heartbeats.

Status vocabulary aligned with the heartbeat primitive memory note
(active / busy / delayed / sleeping / unresponsive / suspended /
disabled / unknown). Unknown values pass through to the backend body
so the protocol can evolve without CLI updates.
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

from ..config import get_client, resolve_agent_name
from ..output import JSON_OPTION, console, print_json, print_table

app = typer.Typer(name="heartbeat", help="Local-first agent heartbeat primitive", no_args_is_help=True)


_DEFAULT_CADENCE_SECONDS = 60
_VALID_STATUSES = (
    "active",
    "busy",
    "delayed",
    "sleeping",
    "unresponsive",
    "suspended",
    "disabled",
    "unknown",
)


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


def _default_store_file() -> Path:
    env_path = os.environ.get("AX_HEARTBEATS_FILE")
    if env_path:
        return Path(env_path).expanduser()
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        ax_dir = parent / ".ax"
        if ax_dir.is_dir():
            return ax_dir / "heartbeats.json"
    return Path.home() / ".ax" / "heartbeats.json"


def _store_file(path: str | None) -> Path:
    return Path(path).expanduser() if path else _default_store_file()


def _empty_store() -> dict[str, Any]:
    return {
        "version": 1,
        "agent_name": None,
        "agent_id": None,
        "cadence_seconds": _DEFAULT_CADENCE_SECONDS,
        "current_status": "unknown",
        "current_note": None,
        "last_sent_at": None,
        "last_pushed_at": None,
        "next_due_at": None,
        "history": [],
    }


def _load_store(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_store()
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        typer.echo(f"Error: heartbeats file is not valid JSON: {path} ({exc})", err=True)
        raise typer.Exit(1)
    if not isinstance(data, dict):
        typer.echo(f"Error: heartbeats file must contain a JSON object: {path}", err=True)
        raise typer.Exit(1)
    # Forward-compat defaults
    base = _empty_store()
    for key, default in base.items():
        data.setdefault(key, default)
    if not isinstance(data["history"], list):
        typer.echo(f"Error: heartbeats history must be a list: {path}", err=True)
        raise typer.Exit(1)
    return data


def _save_store(path: Path, store: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)
    path.chmod(0o600)


def _short_id() -> str:
    return f"hb-{uuid.uuid4().hex[:10]}"


def _normalize_status(value: str | None, *, allow_passthrough: bool = True) -> str:
    text = (value or "active").strip().lower()
    if not allow_passthrough and text not in _VALID_STATUSES:
        raise typer.BadParameter(f"--status must be one of: {', '.join(_VALID_STATUSES)} (got '{text}')")
    # Pass-through unknown values so the protocol can evolve. Warn via stderr
    # in human mode is the operator's call; tooling-side relies on the value.
    return text


def _record_heartbeat(
    store: dict[str, Any],
    *,
    status: str,
    note: str | None,
    cadence_seconds: int,
    pushed: bool,
    push_error: str | None,
    backend_response: dict | None,
    now: _dt.datetime,
) -> dict[str, Any]:
    record = {
        "id": _short_id(),
        "status": status,
        "note": note,
        "sent_at": _iso(now),
        "pushed": pushed,
        "pushed_at": _iso(now) if pushed else None,
        "push_error": push_error,
        "backend_ttl_seconds": (backend_response or {}).get("ttl_seconds"),
    }
    store["current_status"] = status
    store["current_note"] = note
    store["cadence_seconds"] = int(cadence_seconds)
    store["last_sent_at"] = record["sent_at"]
    if pushed:
        store["last_pushed_at"] = record["pushed_at"]
    store["next_due_at"] = _iso(now + _dt.timedelta(seconds=int(cadence_seconds)))
    history = list(store.get("history") or [])
    history.append(record)
    store["history"] = history[-100:]  # ring buffer
    return record


def _try_push(
    client: Any,
    *,
    status: str,
    note: str | None,
    cadence_seconds: int,
) -> tuple[bool, str | None, dict | None]:
    """Attempt to POST a heartbeat. Returns (pushed, error_str, response)."""
    try:
        resp = client.send_heartbeat(
            status=status,
            note=note,
            cadence_seconds=cadence_seconds,
        )
        return True, None, resp if isinstance(resp, dict) else None
    except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
        return False, f"network: {exc}", None
    except httpx.HTTPStatusError as exc:
        return False, f"{exc.response.status_code}: {exc.response.text[:200]}", None
    except Exception as exc:  # noqa: BLE001 — defensive: any client error degrades to local queue
        return False, str(exc), None


@app.command("send")
def send(
    status: str = typer.Option("active", "--status", "-s", help=f"Status: {' | '.join(_VALID_STATUSES)}"),
    note: Optional[str] = typer.Option(None, "--note", "-n", help="Optional context note"),
    cadence: int = typer.Option(_DEFAULT_CADENCE_SECONDS, "--cadence", "-c", help="Declared cadence in seconds"),
    skip_push: bool = typer.Option(False, "--skip-push", help="Record locally only; do not POST to backend"),
    store_file: Optional[str] = typer.Option(None, "--file", help="Local heartbeats JSON file"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Send one heartbeat. Records locally + POSTs to backend (offline-safe).

    On network failure the heartbeat is recorded locally with pushed=false
    and a push_error string. Use ``ax heartbeat push`` to drain the queue
    once connectivity returns.
    """
    if cadence < 1:
        raise typer.BadParameter("--cadence must be at least 1 second")
    normalized_status = _normalize_status(status)

    path = _store_file(store_file)
    store = _load_store(path)

    pushed = False
    push_error: Optional[str] = None
    backend_response: Optional[dict] = None

    if not skip_push:
        try:
            client = get_client()
            try:
                store["agent_name"] = resolve_agent_name(client=client)
            except Exception:
                pass
            pushed, push_error, backend_response = _try_push(
                client,
                status=normalized_status,
                note=note,
                cadence_seconds=cadence,
            )
        except Exception as exc:
            push_error = f"client unavailable: {exc}"

    record = _record_heartbeat(
        store,
        status=normalized_status,
        note=note,
        cadence_seconds=cadence,
        pushed=pushed,
        push_error=push_error,
        backend_response=backend_response,
        now=_now(),
    )
    _save_store(path, store)

    if as_json:
        print_json(
            {
                "record": record,
                "file": str(path),
                "store": {
                    "current_status": store["current_status"],
                    "next_due_at": store["next_due_at"],
                    "cadence_seconds": store["cadence_seconds"],
                },
            }
        )
        return

    if pushed:
        console.print(
            f"[green]heartbeat[/green] {record['id']} status={normalized_status} pushed=yes "
            f"(ttl={record.get('backend_ttl_seconds')}s)"
        )
    else:
        marker = "[yellow]queued[/yellow]" if push_error else "[cyan]local-only[/cyan]"
        reason = f" reason={push_error}" if push_error else ""
        console.print(f"{marker} heartbeat {record['id']} status={normalized_status}{reason}")


@app.command("list")
def list_history(
    limit: int = typer.Option(10, "--limit", help="Max records to show"),
    only_unpushed: bool = typer.Option(False, "--unpushed", help="Show only queued (unpushed) heartbeats"),
    store_file: Optional[str] = typer.Option(None, "--file", help="Local heartbeats JSON file"),
    as_json: bool = JSON_OPTION,
) -> None:
    """List local heartbeat history (most recent first)."""
    path = _store_file(store_file)
    store = _load_store(path)
    history = list(reversed(store.get("history") or []))
    if only_unpushed:
        history = [h for h in history if not h.get("pushed")]
    history = history[:limit]
    if as_json:
        print_json({"file": str(path), "history": history})
        return
    if not history:
        console.print(f"No heartbeats in {path}")
        return
    rows = [
        {
            "id": h.get("id", ""),
            "status": h.get("status", ""),
            "sent_at": h.get("sent_at", ""),
            "pushed": "yes" if h.get("pushed") else "no",
            "ttl": h.get("backend_ttl_seconds") or "-",
            "note": (h.get("note") or "")[:40],
            "error": (h.get("push_error") or "")[:40],
        }
        for h in history
    ]
    print_table(
        ["ID", "Status", "Sent", "Pushed", "TTL", "Note", "Error"],
        rows,
        keys=["id", "status", "sent_at", "pushed", "ttl", "note", "error"],
    )


def _probe_online(timeout: float = 2.0) -> tuple[bool, str | None]:
    """Cheap online probe by attempting a real heartbeat."""
    try:
        client = get_client()
    except Exception as exc:
        return False, f"client unavailable: {exc}"
    base = getattr(client, "_base_url", None) or getattr(client, "base_url", None)
    if not base:
        return False, "no base_url configured"
    try:
        resp = httpx.get(f"{str(base).rstrip('/')}/health", timeout=timeout)
        if resp.status_code < 500:
            return True, None
        return False, f"backend status {resp.status_code}"
    except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
        return False, f"network: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


@app.command("status")
def status_cmd(
    skip_probe: bool = typer.Option(False, "--skip-probe", help="Skip online probe (assume offline)"),
    store_file: Optional[str] = typer.Option(None, "--file", help="Local heartbeats JSON file"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Show current heartbeat state + queue depth."""
    path = _store_file(store_file)
    store = _load_store(path)

    history = store.get("history") or []
    unpushed = [h for h in history if not h.get("pushed")]
    last_sent_at = store.get("last_sent_at")
    last_pushed_at = store.get("last_pushed_at")
    next_due_at = store.get("next_due_at")
    cadence = int(store.get("cadence_seconds", _DEFAULT_CADENCE_SECONDS))

    is_due = False
    if next_due_at:
        try:
            is_due = _parse_iso(next_due_at) <= _now()
        except Exception:
            is_due = False

    if skip_probe:
        is_online, offline_reason = False, "probe skipped"
    else:
        is_online, offline_reason = _probe_online()

    snapshot = {
        "online": is_online,
        "offline_reason": offline_reason,
        "agent_name": store.get("agent_name"),
        "current_status": store.get("current_status"),
        "current_note": store.get("current_note"),
        "cadence_seconds": cadence,
        "last_sent_at": last_sent_at,
        "last_pushed_at": last_pushed_at,
        "next_due_at": next_due_at,
        "is_due_now": is_due,
        "queued_unpushed": len(unpushed),
        "file": str(path),
    }

    if as_json:
        print_json(snapshot)
        return

    state_label = "[bold green]ONLINE[/bold green]" if is_online else "[bold yellow]OFFLINE[/bold yellow]"
    console.print(f"State: {state_label}")
    if not is_online and offline_reason:
        console.print(f"  reason: {offline_reason}")
    console.print(f"Agent: {store.get('agent_name') or '(unknown)'}")
    console.print(f"Status: {store.get('current_status')} (cadence={cadence}s)")
    console.print(f"Last sent: {last_sent_at or '(never)'}")
    console.print(f"Last pushed: {last_pushed_at or '(never)'}")
    if next_due_at:
        marker = "[yellow]DUE NOW[/yellow]" if is_due else "queued"
        console.print(f"Next due: {next_due_at} ({marker})")
    else:
        console.print("Next due: (no heartbeats sent yet)")
    if unpushed:
        console.print(f"Queued (unpushed): [yellow]{len(unpushed)}[/yellow] — run `ax heartbeat push` to drain")


@app.command("push")
def push(
    store_file: Optional[str] = typer.Option(None, "--file", help="Local heartbeats JSON file"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Push queued (unpushed) heartbeats to the backend.

    Sends only the latest unpushed heartbeat — older ones are local-only
    history. Backend presence is a TTL ping, so replaying stale heartbeats
    isn't useful. If you need a richer push-history protocol later, this
    command is the natural extension point.
    """
    path = _store_file(store_file)
    store = _load_store(path)
    history = list(store.get("history") or [])
    unpushed = [h for h in history if not h.get("pushed")]

    if not unpushed:
        if as_json:
            print_json({"file": str(path), "pushed": [], "reason": "no_queued_heartbeats"})
            return
        console.print(f"No queued heartbeats in {path}")
        return

    latest = unpushed[-1]
    try:
        client = get_client()
    except Exception as exc:
        if as_json:
            print_json({"file": str(path), "pushed": [], "error": f"client unavailable: {exc}"})
            return
        console.print(f"[red]error[/red]: client unavailable ({exc})")
        raise typer.Exit(1)

    pushed, err, resp = _try_push(
        client,
        status=str(latest.get("status") or "active"),
        note=latest.get("note"),
        cadence_seconds=int(store.get("cadence_seconds", _DEFAULT_CADENCE_SECONDS)),
    )

    if pushed:
        # Mark this record AND any older unpushed records as pushed (presence is latest-wins).
        for record in history:
            if not record.get("pushed"):
                record["pushed"] = True
                record["pushed_at"] = _iso(_now())
                record["push_error"] = None
                if record["id"] == latest["id"]:
                    record["backend_ttl_seconds"] = (resp or {}).get("ttl_seconds")
        store["history"] = history
        store["last_pushed_at"] = _iso(_now())
        _save_store(path, store)
        if as_json:
            print_json({"file": str(path), "pushed": [latest["id"]], "drained_count": len(unpushed), "backend": resp})
            return
        console.print(
            f"[green]pushed[/green] {latest['id']} (drained {len(unpushed)} queued, "
            f"backend ttl={resp.get('ttl_seconds') if resp else '-'}s)"
        )
    else:
        latest["push_error"] = err
        _save_store(path, store)
        if as_json:
            print_json({"file": str(path), "pushed": [], "error": err})
        else:
            console.print(f"[red]push failed[/red]: {err}")
        raise typer.Exit(1)


@app.command("watch")
def watch(
    interval: int = typer.Option(_DEFAULT_CADENCE_SECONDS, "--interval", "-i", help="Tick interval in seconds"),
    status: str = typer.Option(
        "active", "--status", "-s", help=f"Status to send each tick: {' | '.join(_VALID_STATUSES)}"
    ),
    note: Optional[str] = typer.Option(None, "--note", "-n", help="Optional context note"),
    max_ticks: int = typer.Option(0, "--max-ticks", help="Stop after N ticks (0 = run forever)"),
    store_file: Optional[str] = typer.Option(None, "--file", help="Local heartbeats JSON file"),
) -> None:
    """Tick a heartbeat at the given interval. Use Ctrl-C to stop."""
    if interval < 1:
        raise typer.BadParameter("--interval must be at least 1 second")
    normalized_status = _normalize_status(status)
    path = _store_file(store_file)

    tick = 0
    console.print(f"Heartbeat watch started — interval={interval}s status={normalized_status} file={path}")
    while True:
        tick += 1
        try:
            client = get_client()
            agent_name = None
            try:
                agent_name = resolve_agent_name(client=client)
            except Exception:
                pass
            pushed, err, resp = _try_push(client, status=normalized_status, note=note, cadence_seconds=interval)
        except Exception as exc:
            pushed, err, resp, agent_name = False, f"client unavailable: {exc}", None, None

        store = _load_store(path)
        if agent_name and not store.get("agent_name"):
            store["agent_name"] = agent_name
        record = _record_heartbeat(
            store,
            status=normalized_status,
            note=note,
            cadence_seconds=interval,
            pushed=pushed,
            push_error=err,
            backend_response=resp,
            now=_now(),
        )
        _save_store(path, store)

        if pushed:
            console.print(f"[green]tick {tick}[/green] heartbeat {record['id']} pushed")
        else:
            console.print(f"[yellow]tick {tick}[/yellow] heartbeat {record['id']} queued (err={err})")

        if max_ticks and tick >= max_ticks:
            console.print(f"Reached --max-ticks ({max_ticks}), stopping.")
            return
        time.sleep(interval)
