"""Tests for HEARTBEAT-001: local-first heartbeat CLI primitive."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from typer.testing import CliRunner

from ax_cli.main import app

runner = CliRunner()


class _FakeClient:
    def __init__(self, *, raise_on_send: Exception | None = None) -> None:
        self.heartbeats: list[dict[str, Any]] = []
        self._raise_on_send = raise_on_send

    def send_heartbeat(self, *, agent_id=None, status=None, note=None, cadence_seconds=None) -> dict:
        if self._raise_on_send is not None:
            raise self._raise_on_send
        record = {
            "agent_id": "agent-orion",
            "status": status,
            "note": note,
            "cadence_seconds": cadence_seconds,
            "ttl_seconds": 30,
            "last_heartbeat": "2026-04-25T00:00:00Z",
            "presence": "online",
        }
        self.heartbeats.append(record)
        return record


def _install_runtime(monkeypatch, client: _FakeClient) -> None:
    monkeypatch.setattr("ax_cli.commands.heartbeat.get_client", lambda: client)
    monkeypatch.setattr("ax_cli.commands.heartbeat.resolve_agent_name", lambda client=None: "orion")


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def test_send_records_and_pushes_when_online(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"

    result = runner.invoke(
        app,
        [
            "heartbeat", "send",
            "--status", "active",
            "--note", "starting up",
            "--cadence", "30",
            "--file", str(store_file),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    assert payload["record"]["status"] == "active"
    assert payload["record"]["pushed"] is True
    assert payload["record"]["backend_ttl_seconds"] == 30

    assert len(fake.heartbeats) == 1
    assert fake.heartbeats[0]["status"] == "active"
    assert fake.heartbeats[0]["cadence_seconds"] == 30

    store = _load(store_file)
    assert store["current_status"] == "active"
    assert store["cadence_seconds"] == 30
    assert store["last_pushed_at"] is not None
    assert len(store["history"]) == 1


def test_send_queues_locally_on_network_error(monkeypatch, tmp_path):
    fake = _FakeClient(raise_on_send=httpx.ConnectError("offline (test)"))
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"

    result = runner.invoke(
        app,
        ["heartbeat", "send", "--status", "active", "--cadence", "60",
         "--file", str(store_file), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["record"]["pushed"] is False
    assert "network" in payload["record"]["push_error"]

    store = _load(store_file)
    assert store["last_pushed_at"] is None
    assert len(store["history"]) == 1
    assert store["history"][0]["pushed"] is False


def test_send_skip_push_records_local_only(monkeypatch, tmp_path):
    """--skip-push records locally without attempting a network call."""
    class _ExplodingClient:
        def send_heartbeat(self, *_a, **_kw):
            raise AssertionError("should not be called when --skip-push")

    monkeypatch.setattr("ax_cli.commands.heartbeat.get_client", lambda: _ExplodingClient())
    monkeypatch.setattr("ax_cli.commands.heartbeat.resolve_agent_name", lambda client=None: "orion")

    store_file = tmp_path / "heartbeats.json"
    result = runner.invoke(
        app,
        ["heartbeat", "send", "--status", "busy", "--skip-push",
         "--file", str(store_file), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["record"]["pushed"] is False
    assert payload["record"]["push_error"] is None  # not queued, just local-only


def test_send_rejects_invalid_cadence(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"

    result = runner.invoke(
        app,
        ["heartbeat", "send", "--cadence", "0", "--file", str(store_file)],
    )
    assert result.exit_code != 0
    assert "cadence" in result.output.lower()


def test_send_passes_through_unknown_status(monkeypatch, tmp_path):
    """Unknown status values pass through so the protocol can evolve."""
    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"

    result = runner.invoke(
        app,
        ["heartbeat", "send", "--status", "future_value_xyz", "--cadence", "30",
         "--skip-push",  # don't depend on backend accepting it
         "--file", str(store_file), "--json"],
    )
    assert result.exit_code == 0, result.output
    store = _load(store_file)
    assert store["current_status"] == "future_value_xyz"


def test_status_reports_queued_unpushed(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"
    store_file.write_text(
        json.dumps(
            {
                "version": 1,
                "agent_name": "orion",
                "agent_id": "abc",
                "cadence_seconds": 60,
                "current_status": "active",
                "current_note": None,
                "last_sent_at": "2026-04-25T15:00:00Z",
                "last_pushed_at": "2026-04-25T15:00:00Z",
                "next_due_at": "2026-04-25T15:01:00Z",
                "history": [
                    {"id": "hb-1", "status": "active", "sent_at": "2026-04-25T15:00:00Z", "pushed": True},
                    {"id": "hb-2", "status": "active", "sent_at": "2026-04-25T15:01:00Z", "pushed": False, "push_error": "network"},
                    {"id": "hb-3", "status": "busy", "sent_at": "2026-04-25T15:02:00Z", "pushed": False, "push_error": "network"},
                ],
            }
        )
    )

    result = runner.invoke(
        app, ["heartbeat", "status", "--file", str(store_file), "--skip-probe", "--json"]
    )
    assert result.exit_code == 0, result.output
    snapshot = json.loads(result.output)
    assert snapshot["online"] is False
    assert snapshot["offline_reason"] == "probe skipped"
    assert snapshot["queued_unpushed"] == 2
    assert snapshot["agent_name"] == "orion"
    assert snapshot["current_status"] == "active"
    assert snapshot["cadence_seconds"] == 60
    assert snapshot["next_due_at"] == "2026-04-25T15:01:00Z"


def test_status_reports_no_history_gracefully(tmp_path):
    """Empty store: status returns sensible defaults, never crashes."""
    store_file = tmp_path / "heartbeats.json"
    result = runner.invoke(
        app, ["heartbeat", "status", "--file", str(store_file), "--skip-probe", "--json"]
    )
    assert result.exit_code == 0, result.output
    snapshot = json.loads(result.output)
    assert snapshot["queued_unpushed"] == 0
    assert snapshot["last_sent_at"] is None
    assert snapshot["next_due_at"] is None


def test_list_filters_unpushed_only(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"
    store_file.write_text(
        json.dumps(
            {
                "version": 1,
                "history": [
                    {"id": "hb-1", "status": "active", "sent_at": "T1", "pushed": True},
                    {"id": "hb-2", "status": "active", "sent_at": "T2", "pushed": False},
                    {"id": "hb-3", "status": "busy", "sent_at": "T3", "pushed": False},
                ],
            }
        )
    )

    result = runner.invoke(
        app, ["heartbeat", "list", "--unpushed", "--file", str(store_file), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    ids = [h["id"] for h in payload["history"]]
    assert ids == ["hb-3", "hb-2"], "most recent first, only unpushed"


def test_push_drains_queue_when_online(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"
    store_file.write_text(
        json.dumps(
            {
                "version": 1,
                "agent_name": "orion",
                "cadence_seconds": 30,
                "current_status": "busy",
                "history": [
                    {"id": "hb-1", "status": "active", "sent_at": "T1", "pushed": False, "push_error": "old"},
                    {"id": "hb-2", "status": "busy", "sent_at": "T2", "pushed": False, "push_error": "old"},
                ],
            }
        )
    )

    result = runner.invoke(
        app, ["heartbeat", "push", "--file", str(store_file), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["pushed"] == ["hb-2"]
    assert payload["drained_count"] == 2

    store = _load(store_file)
    assert all(h["pushed"] for h in store["history"]), "all queued records marked pushed"
    assert store["history"][-1]["backend_ttl_seconds"] == 30
    assert store["last_pushed_at"] is not None
    # The push uses the LATEST (busy) status, not the oldest
    assert fake.heartbeats[0]["status"] == "busy"


def test_push_no_queued_returns_clean(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"
    store_file.write_text(
        json.dumps({"version": 1, "history": [{"id": "hb-1", "pushed": True}]})
    )

    result = runner.invoke(
        app, ["heartbeat", "push", "--file", str(store_file), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["pushed"] == []
    assert payload["reason"] == "no_queued_heartbeats"
    assert fake.heartbeats == []


def test_push_returns_error_when_offline(monkeypatch, tmp_path):
    fake = _FakeClient(raise_on_send=httpx.ConnectError("offline"))
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"
    store_file.write_text(
        json.dumps({"version": 1, "history": [{"id": "hb-1", "status": "active", "pushed": False}]})
    )

    result = runner.invoke(
        app, ["heartbeat", "push", "--file", str(store_file), "--json"]
    )
    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["pushed"] == []
    assert "network" in payload["error"]

    # Record updated with push_error but still queued
    store = _load(store_file)
    assert store["history"][0]["pushed"] is False
    assert "network" in store["history"][0]["push_error"]
