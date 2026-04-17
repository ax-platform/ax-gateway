import json
import re

from typer.testing import CliRunner

from ax_cli.commands.messages import _processing_status_from_event
from ax_cli.main import app

runner = CliRunner()
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def test_send_file_stores_context_and_includes_context_key(monkeypatch, tmp_path):
    calls = {}
    sample = tmp_path / "WidgetContractProbe.java"
    sample.write_text(
        'public final class WidgetContractProbe { String status() { return "ok"; } }\n',
        encoding="utf-8",
    )

    class FakeClient:
        _base_headers = {}

        def upload_file(self, path, *, space_id=None):
            calls["upload"] = {"path": path, "space_id": space_id}
            return {
                "id": "att-1",
                "attachment_id": "att-1",
                "url": "/api/v1/uploads/files/probe.java",
                "content_type": "text/plain",
                "size": sample.stat().st_size,
                "original_filename": sample.name,
            }

        def set_context(self, space_id, key, value):
            calls["context"] = {"space_id": space_id, "key": key, "value": value}
            return {"ok": True}

        def send_message(
            self,
            space_id,
            content,
            *,
            channel="main",
            parent_id=None,
            attachments=None,
        ):
            calls["message"] = {
                "space_id": space_id,
                "content": content,
                "channel": channel,
                "parent_id": parent_id,
                "attachments": attachments,
            }
            return {"id": "msg-1"}

    monkeypatch.setattr("ax_cli.commands.messages.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.messages.resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr("ax_cli.commands.messages.resolve_agent_name", lambda client=None: None)

    result = runner.invoke(
        app,
        [
            "send",
            "sharing source",
            "--file",
            str(sample),
            "--skip-ax",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["upload"]["space_id"] == "space-1"

    context_key = calls["context"]["key"]
    context_value = json.loads(calls["context"]["value"])
    assert context_key.startswith("upload:")
    assert context_value["type"] == "file_upload"
    assert context_value["context_key"] == context_key
    assert context_value["source"] == "message_attachment"
    assert "WidgetContractProbe" in context_value["content"]

    attachment = calls["message"]["attachments"][0]
    assert attachment["context_key"] == context_key
    assert attachment["filename"] == sample.name
    assert attachment["content_type"] == "text/plain"
    assert attachment["size"] == sample.stat().st_size
    assert attachment["size_bytes"] == sample.stat().st_size


def test_messages_list_shows_short_ids_but_json_keeps_full_ids(monkeypatch):
    message_id = "12345678-90ab-cdef-1234-567890abcdef"
    calls = {}

    class FakeClient:
        def list_messages(self, limit=20, channel="main", *, space_id=None):
            calls["space_id"] = space_id
            return {
                "messages": [
                    {
                        "id": message_id,
                        "content": "hello",
                        "display_name": "orion",
                        "created_at": "2026-04-13T15:00:00Z",
                    }
                ]
            }

    monkeypatch.setattr("ax_cli.commands.messages.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.messages.resolve_space_id", lambda client, explicit=None: "space-1")

    table_result = runner.invoke(app, ["messages", "list"])
    assert table_result.exit_code == 0, table_result.output
    assert "12345678" in table_result.output
    assert message_id not in table_result.output
    assert calls["space_id"] == "space-1"

    json_result = runner.invoke(app, ["messages", "list", "--json"])
    assert json_result.exit_code == 0, json_result.output
    assert json.loads(json_result.output)[0]["id"] == message_id


def test_messages_list_can_request_unread_and_mark_read(monkeypatch):
    calls = {}

    class FakeClient:
        def list_messages(self, limit=20, channel="main", *, space_id=None, unread_only=False, mark_read=False):
            calls["list"] = {
                "limit": limit,
                "channel": channel,
                "space_id": space_id,
                "unread_only": unread_only,
                "mark_read": mark_read,
            }
            return {
                "messages": [
                    {
                        "id": "12345678-90ab-cdef-1234-567890abcdef",
                        "content": "unread update",
                        "display_name": "orion",
                        "created_at": "2026-04-13T15:00:00Z",
                    }
                ],
                "unread_count": 3,
                "marked_read_count": 1,
            }

    monkeypatch.setattr("ax_cli.commands.messages.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.messages.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(app, ["messages", "list", "--unread", "--mark-read"])

    assert result.exit_code == 0, result.output
    assert calls["list"] == {
        "limit": 20,
        "channel": "main",
        "space_id": "space-1",
        "unread_only": True,
        "mark_read": True,
    }
    assert "Unread: 3" in result.output
    assert "Marked read: 1" in result.output


def test_messages_read_marks_single_message(monkeypatch):
    calls = {}

    class FakeClient:
        def list_messages(self, limit=20, channel="main", *, space_id=None):
            return {"messages": [{"id": "12345678-90ab-cdef-1234-567890abcdef"}]}

        def mark_message_read(self, message_id):
            calls["message_id"] = message_id
            return {"status": "success", "message_id": message_id}

    monkeypatch.setattr("ax_cli.commands.messages.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.messages.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(app, ["messages", "read", "12345678", "--json"])

    assert result.exit_code == 0, result.output
    assert calls["message_id"] == "12345678-90ab-cdef-1234-567890abcdef"


def test_messages_read_all_marks_space_read(monkeypatch):
    calls = {}

    class FakeClient:
        def mark_all_messages_read(self):
            calls["all"] = True
            return {"status": "success", "marked_read": 2}

    monkeypatch.setattr("ax_cli.commands.messages.get_client", lambda: FakeClient())

    result = runner.invoke(app, ["messages", "read", "--all", "--json"])

    assert result.exit_code == 0, result.output
    assert calls["all"] is True
    assert json.loads(result.output)["marked_read"] == 2


def test_send_help_prefers_no_wait_language(monkeypatch):
    result = runner.invoke(app, ["send", "--help"], terminal_width=80)
    output = _strip_ansi(result.output)

    assert result.exit_code == 0, result.output
    assert "--no-wait" in output
    assert "--skip-ax" not in output
    assert "--to" in output
    assert "intercom" in output

    calls = {}

    def fake_send(**kwargs):
        calls.update(kwargs)

    monkeypatch.setattr("ax_cli.main.messages.send", fake_send)

    no_wait_result = runner.invoke(app, ["send", "notify only", "--no-wait"])

    assert no_wait_result.exit_code == 0, no_wait_result.output
    assert calls["wait"] is False
    assert calls["skip_ax"] is False


def test_messages_get_resolves_short_id_prefix(monkeypatch):
    message_id = "12345678-90ab-cdef-1234-567890abcdef"
    calls = {}

    class FakeClient:
        def list_messages(self, limit=20, channel="main", *, space_id=None):
            calls["list_limit"] = limit
            calls["space_id"] = space_id
            return {"messages": [{"id": message_id}]}

        def get_message(self, requested_id):
            calls["get_id"] = requested_id
            return {"id": requested_id, "content": "hello"}

    monkeypatch.setattr("ax_cli.commands.messages.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.messages.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(app, ["messages", "get", "12345678", "--json"])
    assert result.exit_code == 0, result.output
    assert calls["list_limit"] == 100
    assert calls["space_id"] == "space-1"
    assert calls["get_id"] == message_id
    assert json.loads(result.output)["id"] == message_id


def test_messages_send_resolves_short_parent_id(monkeypatch):
    parent_id = "abcdef12-3456-7890-abcd-ef1234567890"
    calls = {}

    class FakeClient:
        _base_headers = {}

        def list_messages(self, limit=20, channel="main", *, space_id=None):
            calls["list_limit"] = limit
            calls["space_id"] = space_id
            return {"messages": [{"id": parent_id}]}

        def send_message(self, space_id, content, *, channel="main", parent_id=None, attachments=None):
            calls["message"] = {
                "space_id": space_id,
                "content": content,
                "channel": channel,
                "parent_id": parent_id,
                "attachments": attachments,
            }
            return {"id": "reply-message-id"}

    monkeypatch.setattr("ax_cli.commands.messages.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.messages.resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr("ax_cli.commands.messages.resolve_agent_name", lambda client=None: None)

    result = runner.invoke(app, ["messages", "send", "reply", "--parent", "abcdef12", "--skip-ax", "--json"])
    assert result.exit_code == 0, result.output
    assert calls["list_limit"] == 100
    assert calls["space_id"] == "space-1"
    assert calls["message"]["parent_id"] == parent_id


def test_top_level_send_accepts_parent_alias(monkeypatch):
    calls = {}

    def fake_send(**kwargs):
        calls.update(kwargs)

    monkeypatch.setattr("ax_cli.main.messages.send", fake_send)

    result = runner.invoke(app, ["send", "reply", "--parent", "abcdef12", "--skip-ax"])
    assert result.exit_code == 0, result.output
    assert calls["content"] == "reply"
    assert calls["parent"] == "abcdef12"


def test_send_to_prepends_missing_mention(monkeypatch):
    calls = {}

    class FakeClient:
        _base_headers = {}

        def send_message(self, space_id, content, *, channel="main", parent_id=None, attachments=None):
            calls["message"] = {
                "space_id": space_id,
                "content": content,
                "channel": channel,
                "parent_id": parent_id,
                "attachments": attachments,
            }
            return {"id": "msg-1"}

    monkeypatch.setattr("ax_cli.commands.messages.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.messages.resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr("ax_cli.commands.messages.resolve_agent_name", lambda client=None: None)

    result = runner.invoke(app, ["send", "checkpoint", "--to", "orion", "--no-wait", "--json"])

    assert result.exit_code == 0, result.output
    assert calls["message"]["content"] == "@orion checkpoint"


def test_send_ask_ax_prepends_ax_mention(monkeypatch):
    calls = {}

    class FakeClient:
        _base_headers = {}

        def send_message(self, space_id, content, *, channel="main", parent_id=None, attachments=None):
            calls["message"] = {
                "space_id": space_id,
                "content": content,
                "channel": channel,
                "parent_id": parent_id,
                "attachments": attachments,
            }
            return {"id": "msg-1"}

    monkeypatch.setattr("ax_cli.commands.messages.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.messages.resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr("ax_cli.commands.messages.resolve_agent_name", lambda client=None: None)

    result = runner.invoke(app, ["send", "please summarize unread", "--ask-ax", "--no-wait", "--json"])

    assert result.exit_code == 0, result.output
    assert calls["message"]["content"] == "@aX please summarize unread"


def test_send_ask_ax_does_not_duplicate_existing_ax_mention(monkeypatch):
    calls = {}

    class FakeClient:
        _base_headers = {}

        def send_message(self, space_id, content, *, channel="main", parent_id=None, attachments=None):
            calls["message"] = {"content": content}
            return {"id": "msg-1"}

    monkeypatch.setattr("ax_cli.commands.messages.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.messages.resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr("ax_cli.commands.messages.resolve_agent_name", lambda client=None: None)

    result = runner.invoke(app, ["send", "@aX please summarize unread", "--ask-ax", "--no-wait", "--json"])

    assert result.exit_code == 0, result.output
    assert calls["message"]["content"] == "@aX please summarize unread"


def test_send_ask_ax_rejects_to_combination():
    result = runner.invoke(app, ["send", "route this", "--ask-ax", "--to", "orion"])

    assert result.exit_code == 1
    assert "use either --ask-ax or --to" in result.output


def test_send_to_does_not_duplicate_existing_mention_and_waits_for_target(monkeypatch):
    calls = {}

    class FakeClient:
        _base_headers = {}

        def send_message(self, space_id, content, *, channel="main", parent_id=None, attachments=None):
            calls["message"] = {
                "space_id": space_id,
                "content": content,
                "channel": channel,
                "parent_id": parent_id,
                "attachments": attachments,
            }
            return {"id": "msg-1"}

    def fake_wait(client, message_id, timeout=60, wait_label="reply", **kwargs):
        calls["wait"] = {
            "message_id": message_id,
            "timeout": timeout,
            "wait_label": wait_label,
            "processing_watcher": kwargs.get("processing_watcher"),
        }
        return {"id": "reply-1", "content": "ack"}

    monkeypatch.setattr("ax_cli.commands.messages.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.messages.resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr("ax_cli.commands.messages.resolve_agent_name", lambda client=None: None)
    monkeypatch.setattr("ax_cli.commands.messages._wait_for_reply", fake_wait)

    result = runner.invoke(app, ["send", "@orion checkpoint", "--to", "orion", "--json"])

    assert result.exit_code == 0, result.output
    assert calls["message"]["content"] == "@orion checkpoint"
    assert calls["wait"]["wait_label"] == "@orion"
    assert calls["wait"]["processing_watcher"] is not None


def test_processing_status_from_event_matches_message():
    event = _processing_status_from_event(
        "msg-1",
        "agent_processing",
        {
            "message_id": "msg-1",
            "status": "working",
            "agent_id": "agent-1",
            "agent_name": "orion",
        },
    )

    assert event == {
        "message_id": "msg-1",
        "status": "working",
        "agent_id": "agent-1",
        "agent_name": "orion",
    }
    assert _processing_status_from_event("msg-2", "agent_processing", {"message_id": "msg-1"}) is None
    assert _processing_status_from_event("msg-1", "message", {"message_id": "msg-1"}) is None


def test_messages_edit_and_delete_resolve_short_id_prefix(monkeypatch):
    message_id = "12345678-90ab-cdef-1234-567890abcdef"
    calls = {}

    class FakeClient:
        def list_messages(self, limit=20, channel="main", *, space_id=None):
            calls["list_calls"] = calls.get("list_calls", 0) + 1
            calls.setdefault("space_ids", []).append(space_id)
            return {"messages": [{"id": message_id}]}

        def edit_message(self, requested_id, content):
            calls["edit"] = {"id": requested_id, "content": content}
            return {"id": requested_id, "content": content}

        def delete_message(self, requested_id):
            calls["delete_id"] = requested_id

    monkeypatch.setattr("ax_cli.commands.messages.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.messages.resolve_space_id", lambda client, explicit=None: "space-1")

    edit_result = runner.invoke(app, ["messages", "edit", "12345678", "updated", "--json"])
    assert edit_result.exit_code == 0, edit_result.output
    assert calls["edit"] == {"id": message_id, "content": "updated"}

    delete_result = runner.invoke(app, ["messages", "delete", "12345678", "--json"])
    assert delete_result.exit_code == 0, delete_result.output
    assert calls["delete_id"] == message_id
    assert calls["space_ids"] == ["space-1", "space-1"]
    assert json.loads(delete_result.output)["message_id"] == message_id
