"""Tests for the local reminder policy runner."""

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
        lambda client=None: "chatgpt",
    )


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def test_add_creates_local_policy_file(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"

    result = runner.invoke(
        app,
        [
            "reminders",
            "add",
            "task-1",
            "--reason",
            "check this task",
            "--target",
            "demo-agent",
            "--first-in-minutes",
            "0",
            "--max-fires",
            "2",
            "--file",
            str(policy_file),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    store = _load(policy_file)
    assert store["version"] == 1
    assert len(store["policies"]) == 1
    policy = store["policies"][0]
    assert policy["source_task_id"] == "task-1"
    assert policy["reason"] == "check this task"
    assert policy["target"] == "demo-agent"
    assert policy["max_fires"] == 2
    assert policy["enabled"] is True


def test_run_once_fires_due_policy_and_disables_at_max(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-test",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-1",
                        "reason": "review task state",
                        "target": "demo-agent",
                        "severity": "info",
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
    assert len(fake.sent) == 1
    sent = fake.sent[0]
    assert sent["message_type"] == "reminder"
    assert sent["content"].startswith("@demo-agent Reminder:")
    metadata = sent["metadata"]
    assert metadata["alert"]["kind"] == "task_reminder"
    assert metadata["alert"]["source_task_id"] == "task-1"
    assert metadata["alert"]["target_agent"] == "demo-agent"
    assert metadata["alert"]["response_required"] is True
    assert metadata["reminder_policy"]["policy_id"] == "rem-test"

    stored = _load(policy_file)["policies"][0]
    assert stored["enabled"] is False
    assert stored["disabled_reason"] == "max_fires reached"
    assert stored["fired_count"] == 1
    assert stored["last_message_id"] == "msg-1"


def test_run_once_skips_future_policy(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-future",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-1",
                        "reason": "not yet",
                        "target": "demo-agent",
                        "cadence_seconds": 300,
                        "next_fire_at": "2999-01-01T00:00:00Z",
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
    assert fake.sent == []
    stored = _load(policy_file)["policies"][0]
    assert stored["enabled"] is True
    assert stored["fired_count"] == 0


def test_run_once_enriches_alert_with_task_snapshot(monkeypatch, tmp_path):
    """Task e55be7c8: task reminder alerts should carry a task snapshot
    (title/priority/status/assignee) so the frontend renders task context
    without a second round-trip."""

    class _TaskAwareHttp:
        def get(self, path: str, *, headers: dict) -> Any:
            class _R:
                def __init__(self, data):
                    self._data = data

                def raise_for_status(self):
                    return None

                def json(self):
                    return self._data

            if path.endswith("/tasks/task-snap"):
                return _R(
                    {
                        "task": {
                            "id": "task-snap",
                            "title": "Ship delivery receipts",
                            "priority": "urgent",
                            "status": "in_progress",
                            "assignee_id": "agent-demo-agent",
                            "creator_id": "agent-chatgpt",
                            "deadline": "2026-04-17T00:00:00Z",
                        }
                    }
                )
            if path.endswith("/agents/agent-demo-agent"):
                return _R({"agent": {"id": "agent-demo-agent", "name": "demo-agent"}})
            return _R({})

    fake = _FakeClient()
    fake._http = _TaskAwareHttp()  # type: ignore[attr-defined]
    fake._with_agent = lambda _: {}  # type: ignore[attr-defined]
    fake._parse_json = lambda r: r.json()  # type: ignore[attr-defined]
    _install_fake_runtime(monkeypatch, fake)

    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-snap",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-snap",
                        "reason": "review delivery receipts",
                        "target": "demo-agent",
                        "severity": "info",
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
    assert len(fake.sent) == 1
    metadata = fake.sent[0]["metadata"]

    task = metadata["alert"].get("task")
    assert task is not None, "alert.task should be embedded when source_task resolves"
    assert task["id"] == "task-snap"
    assert task["title"] == "Ship delivery receipts"
    assert task["priority"] == "urgent"
    assert task["status"] == "in_progress"
    assert task["assignee_id"] == "agent-demo-agent"
    assert task["assignee_name"] == "demo-agent"
    assert task["deadline"] == "2026-04-17T00:00:00Z"

    card_payload = metadata["ui"]["cards"][0]["payload"]
    assert card_payload.get("task") == task, "card_payload.task should mirror alert.task"
    assert card_payload.get("resource_uri") == "ui://tasks/task-snap"


def test_run_once_without_task_snapshot_still_fires(monkeypatch, tmp_path):
    """If the task fetch fails (404, network), the reminder still fires
    without a task snapshot — the existing source_task_id link is the fallback."""
    fake = _FakeClient()

    class _FailingHttp:
        def get(self, path: str, *, headers: dict) -> Any:
            raise RuntimeError("simulated network failure")

    fake._http = _FailingHttp()  # type: ignore[attr-defined]
    fake._with_agent = lambda _: {}  # type: ignore[attr-defined]
    fake._parse_json = lambda r: r.json()  # type: ignore[attr-defined]
    _install_fake_runtime(monkeypatch, fake)

    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-fail",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-nope",
                        "reason": "fallback path",
                        "target": "demo-agent",
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
    assert len(fake.sent) == 1
    metadata = fake.sent[0]["metadata"]
    assert "task" not in metadata["alert"], "fallback: no task snapshot embedded on failure"
    assert metadata["alert"]["source_task_id"] == "task-nope", "source_task_id link still present"


def _http_stub(routes: dict[str, dict]):
    """Build a minimal _http stub that serves fixed responses per path suffix."""

    class _R:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    class _Stub:
        def get(self, path: str, *, headers: dict) -> Any:
            for suffix, payload in routes.items():
                if path.endswith(suffix):
                    return _R(payload)
            return _R({})

    return _Stub()


def _install_task_aware_client(monkeypatch, routes: dict[str, dict]) -> _FakeClient:
    fake = _FakeClient()
    fake._http = _http_stub(routes)  # type: ignore[attr-defined]
    fake._with_agent = lambda _: {}  # type: ignore[attr-defined]
    fake._parse_json = lambda r: r.json()  # type: ignore[attr-defined]
    _install_fake_runtime(monkeypatch, fake)
    return fake


def test_run_once_skips_and_disables_when_source_task_is_terminal(monkeypatch, tmp_path):
    """Task e032bc49: if source task is completed/closed/done/cancelled,
    reminder must not fire and the policy must be disabled so it stops
    flooding the Activity Stream."""
    fake = _install_task_aware_client(
        monkeypatch,
        {
            "/tasks/task-done": {
                "task": {
                    "id": "task-done",
                    "title": "Already shipped",
                    "status": "completed",
                    "assignee_id": "agent-demo-agent",
                    "creator_id": "agent-chatgpt",
                }
            },
            "/agents/agent-demo-agent": {"agent": {"id": "agent-demo-agent", "name": "demo-agent"}},
        },
    )

    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-done",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-done",
                        "reason": "old reminder for a finished task",
                        "target": "demo-agent",
                        "severity": "info",
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
    assert fake.sent == [], "terminal task must not produce a reminder message"

    payload = json.loads(result.output)
    assert len(payload["fired"]) == 1
    skipped = payload["fired"][0]
    assert skipped.get("skipped") is True
    assert skipped.get("reason") == "source_task_terminal:completed"

    stored = _load(policy_file)["policies"][0]
    assert stored["enabled"] is False
    assert stored["disabled_reason"] == "source task task-done is completed"
    assert stored["fired_count"] == 0, "disabled skip must NOT advance fired_count"


def test_run_once_reroutes_pending_review_to_review_owner(monkeypatch, tmp_path):
    """Task f00e36ac: if task is pending_review with a review_owner in
    requirements, reminder must route to the reviewer — not the worker/assignee."""
    fake = _install_task_aware_client(
        monkeypatch,
        {
            "/tasks/task-review": {
                "task": {
                    "id": "task-review",
                    "title": "PR awaiting review",
                    "status": "pending_review",
                    "assignee_id": "agent-demo-agent",
                    "creator_id": "agent-chatgpt",
                    "requirements": {"review_owner": "alex"},
                }
            },
            "/agents/agent-demo-agent": {"agent": {"id": "agent-demo-agent", "name": "demo-agent"}},
            "/agents/agent-chatgpt": {"agent": {"id": "agent-chatgpt", "name": "chatgpt"}},
        },
    )

    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-review",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-review",
                        "reason": "merge this PR",
                        "severity": "info",
                        "cadence_seconds": 300,
                        "next_fire_at": "2026-04-16T00:00:00Z",
                        "max_fires": 2,
                        "fired_count": 0,
                        "fired_keys": [],
                    }
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file), "--json"])

    assert result.exit_code == 0, result.output
    assert len(fake.sent) == 1, "reminder still fires — just reroutes to reviewer"
    sent = fake.sent[0]
    assert sent["content"].startswith("@alex Reminder:")
    assert "[pending review]" in sent["content"], "reason should be prefixed with [pending review]"
    metadata = sent["metadata"]
    assert metadata["alert"]["target_agent"] == "alex"
    assert metadata["reminder_policy"]["target_resolved_from"] == "review_owner"
    # Policy continues (not disabled) — the review owner can still be reminded
    stored = _load(policy_file)["policies"][0]
    assert stored["enabled"] is True
    assert stored["fired_count"] == 1


def test_run_once_pending_review_falls_back_to_creator_when_no_owner(monkeypatch, tmp_path):
    """Task f00e36ac: if pending_review is flagged but no review_owner is
    listed, fall back to the task creator (per spec escalation ladder)."""
    fake = _install_task_aware_client(
        monkeypatch,
        {
            "/tasks/task-review2": {
                "task": {
                    "id": "task-review2",
                    "title": "PR awaiting review — no owner",
                    "status": "in_progress",
                    "assignee_id": "agent-demo-agent",
                    "creator_id": "agent-chatgpt",
                    "requirements": {"pending_review": True},
                }
            },
            "/agents/agent-demo-agent": {"agent": {"id": "agent-demo-agent", "name": "demo-agent"}},
            "/agents/agent-chatgpt": {"agent": {"id": "agent-chatgpt", "name": "chatgpt"}},
        },
    )

    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-review2",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-review2",
                        "reason": "review this",
                        "severity": "info",
                        "cadence_seconds": 300,
                        "next_fire_at": "2026-04-16T00:00:00Z",
                        "max_fires": 2,
                        "fired_count": 0,
                        "fired_keys": [],
                    }
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file), "--json"])

    assert result.exit_code == 0, result.output
    assert len(fake.sent) == 1
    sent = fake.sent[0]
    assert sent["content"].startswith("@chatgpt Reminder:"), "falls back to creator"
    metadata = sent["metadata"]
    assert metadata["reminder_policy"]["target_resolved_from"] == "creator_fallback"


def test_pause_skips_due_policy_and_resume_reactivates(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-pause",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-1",
                        "reason": "review task state",
                        "target": "demo-agent",
                        "severity": "info",
                        "cadence_seconds": 300,
                        "next_fire_at": "2026-04-16T00:00:00Z",
                        "max_fires": 2,
                        "fired_count": 0,
                        "fired_keys": [],
                    }
                ],
            }
        )
    )

    pause_result = runner.invoke(
        app,
        [
            "reminders",
            "pause",
            "rem-pause",
            "--reason",
            "blocked until review",
            "--paused-by",
            "cli_sentinel",
            "--file",
            str(policy_file),
            "--json",
        ],
    )
    assert pause_result.exit_code == 0, pause_result.output
    stored = _load(policy_file)["policies"][0]
    assert stored["paused"] is True
    assert stored["paused_reason"] == "blocked until review"
    assert stored["paused_by"] == "cli_sentinel"

    run_result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file), "--json"])
    assert run_result.exit_code == 0, run_result.output
    assert fake.sent == []
    assert _load(policy_file)["policies"][0]["fired_count"] == 0

    resume_result = runner.invoke(
        app,
        ["reminders", "resume", "rem-pause", "--fire-in-minutes", "0", "--file", str(policy_file), "--json"],
    )
    assert resume_result.exit_code == 0, resume_result.output
    resumed = _load(policy_file)["policies"][0]
    assert resumed["paused"] is False
    assert resumed["enabled"] is True
    assert resumed["resume_at"] is None

    fired_result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file), "--json"])
    assert fired_result.exit_code == 0, fired_result.output
    assert len(fake.sent) == 1


def test_list_json_groups_policies_by_operational_state(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-due",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-1",
                        "next_fire_at": "2026-04-27T06:00:00Z",
                        "max_fires": 5,
                        "fired_count": 0,
                    },
                    {
                        "id": "rem-paused",
                        "enabled": True,
                        "paused": True,
                        "paused_reason": "too noisy",
                        "resume_at": "2999-01-01T00:00:00Z",
                        "source_task_id": "task-2",
                        "next_fire_at": "2026-04-27T06:00:00Z",
                        "max_fires": 5,
                        "fired_count": 0,
                    },
                    {
                        "id": "rem-disabled",
                        "enabled": False,
                        "source_task_id": "task-3",
                        "next_fire_at": "2999-01-01T00:00:00Z",
                        "max_fires": 5,
                        "fired_count": 0,
                    },
                    {
                        "id": "rem-complete",
                        "enabled": False,
                        "source_task_id": "task-4",
                        "next_fire_at": "2999-01-01T00:00:00Z",
                        "max_fires": 1,
                        "fired_count": 1,
                    },
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "list", "--file", str(policy_file), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [p["id"] for p in payload["groups"]["paused"]] == ["rem-paused"]
    assert [p["id"] for p in payload["groups"]["disabled"]] == ["rem-disabled"]
    assert [p["id"] for p in payload["groups"]["completed"]] == ["rem-complete"]
    assert "summary" in payload
    assert payload["policies"][0]["id"] in {"rem-due", "rem-paused", "rem-disabled", "rem-complete"}


def test_groom_reports_terminal_source_task_and_apply_disables(monkeypatch, tmp_path):
    _install_task_aware_client(
        monkeypatch,
        {
            "/tasks/task-done": {
                "task": {
                    "id": "task-done",
                    "title": "Already shipped",
                    "status": "completed",
                    "assignee_id": "agent-demo-agent",
                }
            },
            "/agents/agent-demo-agent": {"agent": {"id": "agent-demo-agent", "name": "demo-agent"}},
        },
    )
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-groom",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-done",
                        "reason": "finished work",
                        "target": "demo-agent",
                        "cadence_seconds": 300,
                        "next_fire_at": "2999-01-01T00:00:00Z",
                        "max_fires": 5,
                        "fired_count": 0,
                        "fired_keys": [],
                    }
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "groom", "--file", str(policy_file), "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["summary"]["needs_attention"] == 1
    assert report["items"][0]["reasons"] == ["source_task_terminal:completed"]
    assert report["items"][0]["recommendation"] == "disable_or_remove_completed"
    assert any("Pause blocked/noisy" in item for item in report["hygiene"])

    apply_result = runner.invoke(app, ["reminders", "groom", "--apply", "--file", str(policy_file), "--json"])
    assert apply_result.exit_code == 0, apply_result.output
    assert json.loads(apply_result.output)["changed"] == ["rem-groom"]
    stored = _load(policy_file)["policies"][0]
    assert stored["enabled"] is False
    assert stored["disabled_reason"] == "source_task_terminal:completed"
