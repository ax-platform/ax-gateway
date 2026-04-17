"""Tests for ax alerts — metadata shape + state transitions.

These lock down the contract the frontend AlertCardBody reads. If any of
these fields drift, the alert card will silently render wrong or fall
back to a generic result card.
"""

from __future__ import annotations

import json
import re
from typing import Any

from typer.testing import CliRunner

from ax_cli.main import app

runner = CliRunner()
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


class _FakeClient:
    """Captures send_message / PATCH calls for assertion."""

    _base_headers: dict[str, str] = {}

    def __init__(self, preloaded_message: dict[str, Any] | None = None) -> None:
        self.sent: dict[str, Any] = {}
        self.patched: dict[str, Any] = {}
        self._preloaded_message = preloaded_message or {}

        # Fake http client so _post_state_change can use client._http.patch
        fake_self = self

        class _Http:
            def patch(self, path: str, *, json: dict, headers: dict) -> Any:  # noqa: A002
                fake_self.patched = {"path": path, "json": json, "headers": headers}

                class _R:
                    status_code = 200

                    def raise_for_status(self_inner) -> None:
                        return None

                    def json(self_inner) -> dict:
                        return {"message": {"id": path.rsplit("/", 1)[-1], **json}}

                return _R()

            def get(self, path: str, *, headers: dict) -> Any:
                class _R:
                    status_code = 200

                    def raise_for_status(self_inner) -> None:
                        return None

                    def json(self_inner) -> dict:
                        return {"message": fake_self._preloaded_message}

                return _R()

        self._http = _Http()

    def _with_agent(self, _agent_id: str | None) -> dict[str, str]:
        return {}

    def _parse_json(self, r: Any) -> Any:
        return r.json()

    def send_message(
        self,
        space_id: str,
        content: str,
        *,
        channel: str = "main",
        parent_id: str | None = None,
        attachments: list[dict] | None = None,
        metadata: dict | None = None,
        message_type: str = "text",
        **_kwargs: Any,
    ) -> dict:
        self.sent = {
            "space_id": space_id,
            "content": content,
            "channel": channel,
            "parent_id": parent_id,
            "attachments": attachments,
            "metadata": metadata,
            "message_type": message_type,
        }
        return {"id": "msg-42"}

    def get_message(self, message_id: str) -> dict:
        return {"message": self._preloaded_message}


def _install_fake_client(monkeypatch, client: _FakeClient) -> None:
    monkeypatch.setattr("ax_cli.commands.alerts.get_client", lambda: client)
    monkeypatch.setattr(
        "ax_cli.commands.alerts.resolve_space_id",
        lambda _client, *, explicit=None: "space-abc",
    )
    monkeypatch.setattr(
        "ax_cli.commands.alerts.resolve_agent_name",
        lambda client=None: "orion",
    )


def test_send_builds_alert_metadata_with_ui_card_type_alert(monkeypatch):
    fake = _FakeClient()
    _install_fake_client(monkeypatch, fake)

    result = runner.invoke(
        app,
        [
            "alerts",
            "send",
            "ALB /auth/me is 5xx",
            "--target",
            "@orion",
            "--severity",
            "critical",
        ],
    )
    assert result.exit_code == 0, _strip_ansi(result.stdout)

    metadata = fake.sent["metadata"]
    assert metadata is not None, "alert metadata must be sent"

    # Frontend contract: metadata.alert must carry kind + severity
    alert = metadata["alert"]
    assert alert["kind"] == "alert"
    assert alert["severity"] == "critical"
    assert alert["target_agent"] == "orion"
    assert alert["state"] == "triggered"
    assert alert["response_required"] is False
    assert "fired_at" in alert

    # The card type must be "alert" — this is what triggers AlertCardBody
    # to render instead of the generic result/signal card.
    cards = metadata["ui"]["cards"]
    assert len(cards) == 1
    card = cards[0]
    assert card["type"] == "alert", "card type must be 'alert' so AxMessageWidgets renders AlertCardBody"
    assert card["payload"]["intent"] == "alert"
    assert card["payload"]["alert"]["severity"] == "critical"

    # Content should @-mention the target so notification routing fires
    assert fake.sent["content"].startswith("@orion ")
    # message_type = "alert" so stream filters can distinguish from text
    assert fake.sent["message_type"] == "alert"


def test_reminder_requires_source_task_and_marks_kind_task_reminder(monkeypatch):
    fake = _FakeClient()
    _install_fake_client(monkeypatch, fake)

    # reminder kind without source_task should fail with a clear error
    result_no_task = runner.invoke(
        app,
        ["alerts", "send", "followup", "--kind", "reminder"],
    )
    assert result_no_task.exit_code != 0
    assert "source-task" in _strip_ansi(result_no_task.stdout + (result_no_task.stderr or ""))

    # With source_task, reminder goes through with kind task_reminder
    result = runner.invoke(
        app,
        [
            "alerts",
            "send",
            "review launch board",
            "--kind",
            "reminder",
            "--source-task",
            "dfef4c92",
            "--target",
            "@orion",
            "--remind-at",
            "2026-04-16T17:00:00Z",
            "--due-at",
            "2026-04-16T20:00:00Z",
        ],
    )
    assert result.exit_code == 0, _strip_ansi(result.stdout)

    alert = fake.sent["metadata"]["alert"]
    # Frontend treats task_reminder as a reminder variant of alert kind.
    assert alert["kind"] == "task_reminder"
    assert alert["source_task_id"] == "dfef4c92"
    assert alert["remind_at"] == "2026-04-16T17:00:00Z"
    assert alert["due_at"] == "2026-04-16T20:00:00Z"

    # Compact: reminder must NOT embed the task board as initial_data
    # (the dogfood gap ChatGPT flagged).
    card = fake.sent["metadata"]["ui"]["cards"][0]
    assert card["type"] == "alert"
    assert "initial_data" not in card.get("payload", {}), "reminder card must not embed task-board initial_data"
    assert "widget" not in fake.sent["metadata"], "no mcp_app widget hydration for the first slice"
    # resource_uri should point at the linked task so the card is clickable
    assert card["payload"]["resource_uri"] == "ui://tasks/dfef4c92"

    # message_type distinguishes reminders in the stream
    assert fake.sent["message_type"] == "reminder"


def test_reminder_shortcut_command_equivalent_to_send_kind_reminder(monkeypatch):
    fake = _FakeClient()
    _install_fake_client(monkeypatch, fake)

    result = runner.invoke(
        app,
        [
            "alerts",
            "reminder",
            "check dev smoke",
            "--source-task",
            "dfef4c92",
            "--target",
            "orion",
        ],
    )
    assert result.exit_code == 0, _strip_ansi(result.stdout)
    assert fake.sent["metadata"]["alert"]["kind"] == "task_reminder"
    assert fake.sent["metadata"]["alert"]["source_task_id"] == "dfef4c92"


def test_severity_normalization_rejects_garbage(monkeypatch):
    fake = _FakeClient()
    _install_fake_client(monkeypatch, fake)

    bad = runner.invoke(
        app,
        ["alerts", "send", "oops", "--severity", "bogus"],
    )
    assert bad.exit_code != 0

    # warning is normalized to warn; error → critical
    ok = runner.invoke(
        app,
        ["alerts", "send", "watch it", "--severity", "warning"],
    )
    assert ok.exit_code == 0
    assert fake.sent["metadata"]["alert"]["severity"] == "warn"


def test_ack_posts_state_change_reply_linked_to_parent(monkeypatch):
    existing = {
        "id": "msg-99",
        "space_id": "space-abc",
        "metadata": {
            "alert": {
                "kind": "alert",
                "severity": "warn",
                "state": "triggered",
                "target_agent": "orion",
            },
        },
    }
    fake = _FakeClient(preloaded_message=existing)
    _install_fake_client(monkeypatch, fake)

    result = runner.invoke(app, ["alerts", "ack", "msg-99"])
    assert result.exit_code == 0, _strip_ansi(result.stdout)

    # Reply goes through send_message, not PATCH, because backend PATCH
    # drops metadata. State-change reply links to parent via parent_id.
    assert fake.sent["parent_id"] == "msg-99", (
        "state-change must be a reply (parent_id set) so the stream links to the original alert"
    )
    assert fake.sent["message_type"] == "alert_state_change"

    meta = fake.sent["metadata"]
    change = meta["alert_state_change"]
    assert change["parent_message_id"] == "msg-99"
    assert change["new_state"] == "acknowledged"
    assert change["previous_state"] == "triggered"
    assert change["kind"] == "alert"

    # Mirror alert block so card renderers that read metadata.alert still
    # see this as a lightweight event in the stream.
    assert meta["alert"]["kind"] == "alert_state_change"
    assert meta["alert"]["state"] == "acknowledged"
    assert meta["alert"]["severity"] == "warn", "inherits parent severity"
    assert meta["alert"]["parent_message_id"] == "msg-99"


def test_resolve_transitions_state_to_resolved(monkeypatch):
    existing = {
        "id": "msg-100",
        "space_id": "space-abc",
        "metadata": {"alert": {"kind": "alert", "state": "acknowledged"}},
    }
    fake = _FakeClient(preloaded_message=existing)
    _install_fake_client(monkeypatch, fake)

    result = runner.invoke(app, ["alerts", "resolve", "msg-100"])
    assert result.exit_code == 0, _strip_ansi(result.stdout)
    assert fake.sent["metadata"]["alert_state_change"]["new_state"] == "resolved"
    assert fake.sent["metadata"]["alert_state_change"]["previous_state"] == "acknowledged"


def test_state_rejects_unknown_value(monkeypatch):
    existing = {"id": "x", "space_id": "space-abc", "metadata": {"alert": {"kind": "alert"}}}
    fake = _FakeClient(preloaded_message=existing)
    _install_fake_client(monkeypatch, fake)

    bad = runner.invoke(app, ["alerts", "state", "x", "zombie"])
    assert bad.exit_code != 0


def test_snooze_transitions_to_snoozed_state(monkeypatch):
    existing = {
        "id": "msg-snz",
        "space_id": "space-abc",
        "metadata": {"alert": {"kind": "task_reminder", "state": "triggered"}},
    }
    fake = _FakeClient(preloaded_message=existing)
    _install_fake_client(monkeypatch, fake)

    result = runner.invoke(app, ["alerts", "snooze", "msg-snz"])
    assert result.exit_code == 0, _strip_ansi(result.stdout)
    assert fake.sent["metadata"]["alert_state_change"]["new_state"] == "snoozed"


def test_source_task_auto_targets_assignee_when_target_omitted(monkeypatch):
    """When --source-task is given and --target is not, default to the
    task's assignee (preferred) or creator (fallback). This keeps tasks
    as source-of-truth and stops manual --target typing for task-linked
    reminders."""
    fake = _FakeClient()

    # Stub out the http helpers _resolve_target_from_task uses
    task_payload = {
        "task": {
            "id": "dfef4c92",
            "assignee_id": "agent-assignee-id",
            "creator_id": "agent-creator-id",
        }
    }
    agent_payloads = {
        "agent-assignee-id": {"agent": {"id": "agent-assignee-id", "name": "orion"}},
        "agent-creator-id": {"agent": {"id": "agent-creator-id", "name": "chatgpt"}},
    }

    class _TaskAwareHttp:
        def patch(self, *a, **k):  # not used in send path
            raise AssertionError("send should not PATCH")

        def get(self, path: str, *, headers: dict) -> Any:
            class _R:
                def __init__(self, data):
                    self._data = data

                def raise_for_status(self):
                    return None

                def json(self):
                    return self._data

            if "/tasks/" in path:
                return _R(task_payload)
            if "/agents/" in path:
                aid = path.rsplit("/", 1)[-1]
                return _R(agent_payloads.get(aid, {}))
            return _R({})

    fake._http = _TaskAwareHttp()
    _install_fake_client(monkeypatch, fake)

    result = runner.invoke(
        app,
        ["alerts", "send", "check this", "--kind", "reminder", "--source-task", "dfef4c92"],
    )
    assert result.exit_code == 0, _strip_ansi(result.stdout)

    # Auto-resolved to assignee (orion)
    assert fake.sent["metadata"]["alert"]["target_agent"] == "orion"
    assert fake.sent["content"].startswith("@orion "), "auto-target must @-mention assignee"


def test_source_task_falls_back_to_creator_when_no_assignee(monkeypatch):
    fake = _FakeClient()

    task_payload = {
        "task": {
            "id": "t-noa",
            "assignee_id": None,  # no assignee
            "creator_id": "agent-creator-id",
        }
    }
    agent_payloads = {
        "agent-creator-id": {"agent": {"id": "agent-creator-id", "name": "madtank"}},
    }

    class _TaskAwareHttp:
        def patch(self, *a, **k):
            raise AssertionError("unreachable")

        def get(self, path: str, *, headers: dict) -> Any:
            class _R:
                def __init__(self, data):
                    self._data = data

                def raise_for_status(self):
                    return None

                def json(self):
                    return self._data

            if "/tasks/" in path:
                return _R(task_payload)
            if "/agents/" in path:
                aid = path.rsplit("/", 1)[-1]
                return _R(agent_payloads.get(aid, {}))
            return _R({})

    fake._http = _TaskAwareHttp()
    _install_fake_client(monkeypatch, fake)

    result = runner.invoke(
        app,
        ["alerts", "send", "check", "--kind", "reminder", "--source-task", "t-noa"],
    )
    assert result.exit_code == 0, _strip_ansi(result.stdout)
    assert fake.sent["metadata"]["alert"]["target_agent"] == "madtank"


def test_explicit_target_beats_task_auto_resolution(monkeypatch):
    """--target should win over task assignee/creator — explicit override
    is important for escalation scenarios."""
    fake = _FakeClient()

    # If the task lookup runs we'd see these values; but --target should short-circuit.
    class _ShortCircuitHttp:
        def patch(self, *a, **k):
            raise AssertionError("unreachable")

        def get(self, path: str, *, headers: dict) -> Any:
            # If this is called, auto-resolution is leaking past an explicit --target.
            raise AssertionError(f"explicit --target should skip task lookup, but got GET {path}")

    fake._http = _ShortCircuitHttp()
    _install_fake_client(monkeypatch, fake)

    result = runner.invoke(
        app,
        [
            "alerts",
            "send",
            "escalation",
            "--kind",
            "reminder",
            "--source-task",
            "dfef4c92",
            "--target",
            "@madtank",
        ],
    )
    assert result.exit_code == 0, _strip_ansi(result.stdout)
    assert fake.sent["metadata"]["alert"]["target_agent"] == "madtank"


def test_state_change_on_non_alert_message_errors_clearly(monkeypatch):
    existing = {
        "id": "msg-plain",
        "space_id": "space-abc",
        "metadata": {},  # no alert block
    }
    fake = _FakeClient(preloaded_message=existing)
    _install_fake_client(monkeypatch, fake)

    bad = runner.invoke(app, ["alerts", "ack", "msg-plain"])
    assert bad.exit_code != 0, "ack on a non-alert message must fail loudly"


def test_rejects_pre_2020_timestamps_as_clock_skew(monkeypatch):
    """Guard against the 2000-01-01 remind_at class of bugs — a runner with
    a frozen/unset clock was producing epoch-adjacent timestamps that
    landed as the user-facing reminder time (real case: msg b9fb15b6)."""
    fake = _FakeClient()
    _install_fake_client(monkeypatch, fake)

    bad_remind = runner.invoke(
        app,
        [
            "alerts",
            "send",
            "clock-skew test",
            "--target",
            "orion",
            "--remind-at",
            "2000-01-01T00:00:00Z",
        ],
    )
    assert bad_remind.exit_code != 0, "must reject remind_at before 2020 — caller has a broken clock"
    assert "broken clock" in _strip_ansi(bad_remind.stdout + (bad_remind.stderr or ""))

    bad_due = runner.invoke(
        app,
        [
            "alerts",
            "send",
            "clock-skew test",
            "--target",
            "orion",
            "--due-at",
            "1999-12-31T23:59:59Z",
        ],
    )
    assert bad_due.exit_code != 0

    # Gibberish timestamps also get rejected with a clear message
    malformed = runner.invoke(
        app,
        ["alerts", "send", "bad iso", "--target", "orion", "--remind-at", "not-a-date"],
    )
    assert malformed.exit_code != 0
    assert "ISO-8601" in _strip_ansi(malformed.stdout + (malformed.stderr or ""))


def test_valid_future_timestamps_accepted(monkeypatch):
    fake = _FakeClient()
    _install_fake_client(monkeypatch, fake)

    ok = runner.invoke(
        app,
        [
            "alerts",
            "send",
            "ok",
            "--kind",
            "reminder",
            "--source-task",
            "t1",
            "--target",
            "orion",
            "--remind-at",
            "2026-04-16T17:00:00Z",
            "--due-at",
            "2026-04-16T20:00:00Z",
        ],
    )
    assert ok.exit_code == 0, _strip_ansi(ok.stdout)
    assert fake.sent["metadata"]["alert"]["remind_at"] == "2026-04-16T17:00:00Z"
    assert fake.sent["metadata"]["alert"]["due_at"] == "2026-04-16T20:00:00Z"


def test_reminder_defaults_response_required_true(monkeypatch):
    """Reminders are work nudges — the recipient is expected to ack or
    snooze. Default response_required=true so the card shows a Required
    chip. Alerts (--kind alert) stay opt-in."""
    fake = _FakeClient()
    _install_fake_client(monkeypatch, fake)

    # Reminder with no explicit --response-required
    r1 = runner.invoke(
        app,
        [
            "alerts",
            "send",
            "nudge",
            "--kind",
            "reminder",
            "--source-task",
            "t1",
            "--target",
            "orion",
        ],
    )
    assert r1.exit_code == 0
    assert fake.sent["metadata"]["alert"]["response_required"] is True, (
        "reminders should default to response_required=true"
    )

    # Plain alert should NOT auto-set response_required
    fake2 = _FakeClient()
    _install_fake_client(monkeypatch, fake2)
    r2 = runner.invoke(
        app,
        ["alerts", "send", "heads up", "--target", "orion"],
    )
    assert r2.exit_code == 0
    assert fake2.sent["metadata"]["alert"]["response_required"] is False, (
        "alerts stay opt-in — only reminders default-true"
    )


def test_json_output_returns_send_response(monkeypatch):
    fake = _FakeClient()
    _install_fake_client(monkeypatch, fake)

    result = runner.invoke(
        app,
        ["alerts", "send", "test", "--target", "@orion", "--json"],
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["id"] == "msg-42"
