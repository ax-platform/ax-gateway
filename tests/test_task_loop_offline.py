"""Tests for TASK-LOOP-001 offline-first follow-up:

- ``add --space-id`` works without backend (no get_client call)
- ``auto`` mode degrades to draft on network errors (auto_degraded flag)
- ``status`` command reports queue depth + pending drafts
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from typer.testing import CliRunner

from ax_cli.main import app

runner = CliRunner()


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def test_add_works_fully_offline_with_explicit_space_id(monkeypatch, tmp_path):
    """``ax reminders add --space-id X`` must NOT call get_client/resolve_space_id."""
    policy_file = tmp_path / "reminders.json"

    def _explode(*_a, **_kw):
        raise RuntimeError("get_client should not be called when --space-id is provided")

    monkeypatch.setattr("ax_cli.commands.reminders.get_client", _explode)
    monkeypatch.setattr("ax_cli.commands.reminders.resolve_space_id", _explode)

    result = runner.invoke(
        app,
        [
            "reminders", "add", "task-O",
            "--target", "orion",
            "--space-id", "space-explicit",
            "--first-in-minutes", "0",
            "--mode", "manual",
            "--file", str(policy_file),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    policy = _load(policy_file)["policies"][0]
    assert policy["space_id"] == "space-explicit"
    assert policy["mode"] == "manual"


class _ConnectErrorClient:
    """Client whose send_message always raises httpx.ConnectError."""
    def __init__(self):
        self.send_attempts = 0

    def send_message(self, *args, **kwargs):
        self.send_attempts += 1
        raise httpx.ConnectError("connection refused (test)")


def _install_offline_runtime(monkeypatch, client):
    monkeypatch.setattr("ax_cli.commands.reminders.get_client", lambda: client)
    monkeypatch.setattr(
        "ax_cli.commands.reminders.resolve_space_id",
        lambda _client, *, explicit=None: explicit or "space-abc",
    )
    monkeypatch.setattr("ax_cli.commands.reminders.resolve_agent_name", lambda client=None: "orion")


def test_auto_mode_degrades_to_draft_on_connect_error(monkeypatch, tmp_path):
    """When the backend is unreachable, auto-mode fires save as drafts with
    auto_degraded=true so the operator can dispatch them once online."""
    fake = _ConnectErrorClient()
    _install_offline_runtime(monkeypatch, fake)

    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "drafts": [],
                "policies": [
                    {
                        "id": "rem-auto-off",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-N",
                        "reason": "auto fire while offline",
                        "target": "orion",
                        "severity": "info",
                        "priority": 50,
                        "mode": "auto",
                        "cadence_seconds": 300,
                        "next_fire_at": "2026-04-16T00:00:00Z",
                        "max_fires": 1,
                        "fired_count": 0,
                        "fired_keys": [],
                    }
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    # send_message was attempted (auto mode tried) but failed
    assert fake.send_attempts == 1

    # Auto-degraded into a draft
    fired = payload["fired"][0]
    assert fired.get("drafted") is True
    assert fired.get("auto_degraded") is True

    store = _load(policy_file)
    assert len(store["drafts"]) == 1
    draft = store["drafts"][0]
    assert draft["auto_degraded"] is True
    assert draft["status"] == "pending"
    assert "connection refused" in draft["auto_degrade_reason"]

    # Policy still advances (the fire happened, just landed as a draft)
    policy = store["policies"][0]
    assert policy["fired_count"] == 1
    assert policy["last_draft_id"] == draft["id"]


def test_status_command_shows_queue_and_drafts(monkeypatch, tmp_path):
    """``ax reminders status --skip-probe --json`` returns a structured snapshot."""
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "policies": [
                    {
                        "id": "rem-A",
                        "enabled": True,
                        "priority": 10,
                        "mode": "auto",
                        "target": "orion",
                        "next_fire_at": "2026-04-26T00:00:00Z",
                        "max_fires": 5,
                        "fired_count": 0,
                        "cadence_seconds": 300,
                    },
                    {
                        "id": "rem-B",
                        "enabled": True,
                        "priority": 50,
                        "mode": "draft",
                        "target": "cipher",
                        "next_fire_at": "2026-04-27T00:00:00Z",
                        "max_fires": 5,
                        "fired_count": 0,
                        "cadence_seconds": 600,
                    },
                    {
                        "id": "rem-C",
                        "enabled": False,
                        "priority": 90,
                        "mode": "auto",
                        "target": "anvil",
                        "next_fire_at": "2026-04-28T00:00:00Z",
                        "max_fires": 1,
                        "fired_count": 1,
                    },
                ],
                "drafts": [
                    {"id": "draft-1", "policy_id": "rem-B", "status": "pending"},
                    {"id": "draft-2", "policy_id": "rem-A", "status": "pending", "auto_degraded": True},
                    {"id": "draft-3", "policy_id": "rem-A", "status": "sent"},
                ],
            }
        )
    )

    result = runner.invoke(
        app, ["reminders", "status", "--file", str(policy_file), "--skip-probe", "--json"]
    )
    assert result.exit_code == 0, result.output
    snapshot = json.loads(result.output)

    assert snapshot["online"] is False
    assert snapshot["offline_reason"] == "probe skipped"
    assert snapshot["policies_total"] == 3
    assert snapshot["policies_enabled"] == 2
    assert snapshot["policies_paused_or_disabled"] == 1
    assert snapshot["drafts_pending"] == 2
    assert snapshot["drafts_auto_degraded"] == 1

    # Next-due is the highest-priority enabled policy (rem-A, priority 10)
    next_due = snapshot["next_due"]
    assert next_due is not None
    assert next_due["id"] == "rem-A"
    assert next_due["priority"] == 10
    assert next_due["mode"] == "auto"


def test_status_with_no_policies(monkeypatch, tmp_path):
    """Empty store: status still works, next_due is null."""
    policy_file = tmp_path / "reminders.json"
    # File doesn't exist — _load_store returns _empty_store

    result = runner.invoke(
        app, ["reminders", "status", "--file", str(policy_file), "--skip-probe", "--json"]
    )
    assert result.exit_code == 0, result.output
    snapshot = json.loads(result.output)
    assert snapshot["policies_total"] == 0
    assert snapshot["drafts_pending"] == 0
    assert snapshot["next_due"] is None
