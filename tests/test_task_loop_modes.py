"""Tests for TASK-LOOP-001: priority + mode (auto/draft/manual) on reminder policies."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from ax_cli.main import app

runner = CliRunner()


class _FakeClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send_message(
        self,
        space_id: str,
        content: str,
        *,
        channel: str = "main",
        metadata: dict | None = None,
        message_type: str = "text",
        **_kwargs: Any,
    ) -> dict:
        message_id = f"msg-{len(self.sent) + 1}"
        self.sent.append(
            {
                "id": message_id,
                "space_id": space_id,
                "content": content,
                "channel": channel,
                "metadata": metadata,
                "message_type": message_type,
            }
        )
        return {"id": message_id}


def _install_fake_runtime(monkeypatch, client: _FakeClient) -> None:
    monkeypatch.setattr("ax_cli.commands.reminders.get_client", lambda: client)
    monkeypatch.setattr(
        "ax_cli.commands.reminders.resolve_space_id",
        lambda _client, *, explicit=None: explicit or "space-abc",
    )
    monkeypatch.setattr(
        "ax_cli.commands.reminders.resolve_agent_name",
        lambda client=None: "orion",
    )


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def test_add_accepts_priority_and_mode(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"

    result = runner.invoke(
        app,
        [
            "reminders", "add", "task-A",
            "--target", "orion",
            "--priority", "10",
            "--mode", "draft",
            "--first-in-minutes", "0",
            "--file", str(policy_file),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    policy = _load(policy_file)["policies"][0]
    assert policy["priority"] == 10
    assert policy["mode"] == "draft"


def test_add_rejects_invalid_priority(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"

    result = runner.invoke(
        app,
        ["reminders", "add", "task-A", "--target", "orion", "--priority", "999",
         "--first-in-minutes", "0", "--file", str(policy_file), "--json"],
    )
    assert result.exit_code != 0
    assert "priority" in result.output.lower()


def test_add_rejects_invalid_mode(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"

    result = runner.invoke(
        app,
        ["reminders", "add", "task-A", "--target", "orion", "--mode", "weird",
         "--first-in-minutes", "0", "--file", str(policy_file), "--json"],
    )
    assert result.exit_code != 0
    assert "mode" in result.output.lower()


def test_due_policies_sort_by_priority(monkeypatch, tmp_path):
    """Higher-priority (lower number) policy fires first when multiple are due."""
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    base_policy = {
        "enabled": True,
        "space_id": "space-abc",
        "source_task_id": "task-1",
        "reason": "review",
        "target": "orion",
        "severity": "info",
        "mode": "auto",
        "cadence_seconds": 300,
        "next_fire_at": "2026-04-16T00:00:00Z",
        "max_fires": 1,
        "fired_count": 0,
        "fired_keys": [],
    }
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "drafts": [],
                "policies": [
                    {**base_policy, "id": "rem-low", "priority": 80},
                    {**base_policy, "id": "rem-high", "priority": 10},
                    {**base_policy, "id": "rem-mid", "priority": 50},
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    fired_order = [item["policy_id"] for item in payload["fired"]]
    # All three should fire since all are due, but in priority order
    assert fired_order == ["rem-high", "rem-mid", "rem-low"]


def test_draft_mode_creates_draft_does_not_send(monkeypatch, tmp_path):
    """A due draft-mode policy creates a draft record and does not call send_message."""
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "drafts": [],
                "policies": [
                    {
                        "id": "rem-draft",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-D",
                        "reason": "needs HITL review",
                        "target": "orion",
                        "severity": "info",
                        "priority": 10,
                        "mode": "draft",
                        "cadence_seconds": 600,
                        "next_fire_at": "2026-04-16T00:00:00Z",
                        "max_fires": 3,
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

    # send_message NOT called
    assert fake.sent == [], "draft mode must not send to the API"

    # Draft was created
    fired = payload["fired"][0]
    assert fired.get("drafted") is True
    assert fired.get("draft_id", "").startswith("draft-")

    store = _load(policy_file)
    assert len(store["drafts"]) == 1
    draft = store["drafts"][0]
    assert draft["status"] == "pending"
    assert draft["target"] == "orion"
    assert draft["content"].startswith("@orion Reminder:")

    # Policy advanced (drafted fires count toward max_fires)
    policy = store["policies"][0]
    assert policy["fired_count"] == 1
    assert policy["last_draft_id"] == draft["id"]


def test_manual_mode_excluded_from_due_run(monkeypatch, tmp_path):
    """Manual-mode policies are skipped by ``run --once``."""
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "drafts": [],
                "policies": [
                    {
                        "id": "rem-manual",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-M",
                        "reason": "manual only",
                        "target": "orion",
                        "severity": "info",
                        "priority": 50,
                        "mode": "manual",
                        "cadence_seconds": 300,
                        "next_fire_at": "2026-04-16T00:00:00Z",
                        "max_fires": 5,
                        "fired_count": 0,
                        "fired_keys": [],
                    }
                ],
            }
        )
    )
    result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file), "--json"])
    assert result.exit_code == 0, result.output
    assert fake.sent == []
    payload = json.loads(result.output)
    # Manual policies excluded from due — fired list empty
    assert payload["fired"] == []
    # Policy unchanged
    policy = _load(policy_file)["policies"][0]
    assert policy["fired_count"] == 0


def test_drafts_send_dispatches_via_api(monkeypatch, tmp_path):
    """``drafts send <id>`` dispatches a pending draft and marks it sent."""
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    # Seed a pending draft directly
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "policies": [],
                "drafts": [
                    {
                        "id": "draft-abc1234567",
                        "policy_id": "rem-x",
                        "space_id": "space-abc",
                        "channel": "main",
                        "target": "orion",
                        "content": "@orion Reminder: test draft",
                        "metadata": {"alert": {"kind": "task_reminder"}},
                        "status": "pending",
                        "created_at": "2026-04-25T00:00:00Z",
                    }
                ],
            }
        )
    )

    result = runner.invoke(
        app, ["reminders", "drafts", "send", "draft-abc", "--file", str(policy_file), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert len(fake.sent) == 1
    sent = fake.sent[0]
    assert sent["content"] == "@orion Reminder: test draft"
    assert sent["message_type"] == "reminder"
    assert sent["metadata"]["alert"]["kind"] == "task_reminder"

    draft = _load(policy_file)["drafts"][0]
    assert draft["status"] == "sent"
    assert draft["message_id"] == "msg-1"


def test_drafts_cancel_does_not_send(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "policies": [],
                "drafts": [
                    {
                        "id": "draft-xyz9876543",
                        "policy_id": "rem-y",
                        "status": "pending",
                        "target": "orion",
                        "content": "@orion test",
                    }
                ],
            }
        )
    )
    result = runner.invoke(
        app, ["reminders", "drafts", "cancel", "draft-xyz", "--file", str(policy_file), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert fake.sent == []
    draft = _load(policy_file)["drafts"][0]
    assert draft["status"] == "cancelled"


def test_drafts_edit_updates_body_with_mention_prefix(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "policies": [],
                "drafts": [
                    {
                        "id": "draft-edit000001",
                        "policy_id": "rem-z",
                        "status": "pending",
                        "target": "orion",
                        "content": "@orion Reminder: original body",
                    }
                ],
            }
        )
    )
    result = runner.invoke(
        app,
        ["reminders", "drafts", "edit", "draft-edit", "--body", "revised text",
         "--file", str(policy_file), "--json"],
    )
    assert result.exit_code == 0, result.output
    draft = _load(policy_file)["drafts"][0]
    assert draft["edited"] is True
    assert "revised text" in draft["content"]
    assert draft["content"].startswith("@orion ")


def test_pause_resume_cycle(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "drafts": [],
                "policies": [
                    {
                        "id": "rem-pause",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-P",
                        "reason": "pausable",
                        "target": "orion",
                        "priority": 50,
                        "mode": "auto",
                        "cadence_seconds": 300,
                        "next_fire_at": "2099-01-01T00:00:00Z",
                        "max_fires": 5,
                        "fired_count": 0,
                        "fired_keys": [],
                    }
                ],
            }
        )
    )
    # Pause
    result = runner.invoke(app, ["reminders", "pause", "rem-pause", "--file", str(policy_file), "--json"])
    assert result.exit_code == 0, result.output
    policy = _load(policy_file)["policies"][0]
    assert policy["enabled"] is False
    assert policy["disabled_reason"] == "paused"

    # Resume
    result = runner.invoke(app, ["reminders", "resume", "rem-pause", "--file", str(policy_file), "--json"])
    assert result.exit_code == 0, result.output
    policy = _load(policy_file)["policies"][0]
    assert policy["enabled"] is True
    assert "disabled_reason" not in policy


def test_update_priority_reorders_queue(monkeypatch, tmp_path):
    """``update --priority`` re-orders the queue without re-creating policies."""
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    base = {
        "enabled": True,
        "space_id": "space-abc",
        "source_task_id": "task-1",
        "reason": "r",
        "target": "orion",
        "mode": "auto",
        "cadence_seconds": 300,
        "next_fire_at": "2026-04-16T00:00:00Z",
        "max_fires": 1,
        "fired_count": 0,
        "fired_keys": [],
    }
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "drafts": [],
                "policies": [
                    {**base, "id": "rem-A", "priority": 50},
                    {**base, "id": "rem-B", "priority": 50},
                ],
            }
        )
    )
    # Bump rem-B to top
    result = runner.invoke(
        app,
        ["reminders", "update", "rem-B", "--priority", "5", "--file", str(policy_file), "--json"],
    )
    assert result.exit_code == 0, result.output

    # Verify sort order on run
    result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file), "--json"])
    payload = json.loads(result.output)
    fired_order = [item["policy_id"] for item in payload["fired"]]
    assert fired_order[0] == "rem-B", "rem-B should fire first after priority bump"
