import json

from typer.testing import CliRunner

from ax_cli.main import app

runner = CliRunner()


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

    class FakeClient:
        def list_messages(self, limit=20, channel="main"):
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

    table_result = runner.invoke(app, ["messages", "list"])
    assert table_result.exit_code == 0, table_result.output
    assert "12345678" in table_result.output
    assert message_id not in table_result.output

    json_result = runner.invoke(app, ["messages", "list", "--json"])
    assert json_result.exit_code == 0, json_result.output
    assert json.loads(json_result.output)[0]["id"] == message_id


def test_messages_get_resolves_short_id_prefix(monkeypatch):
    message_id = "12345678-90ab-cdef-1234-567890abcdef"
    calls = {}

    class FakeClient:
        def list_messages(self, limit=20, channel="main"):
            calls["list_limit"] = limit
            return {"messages": [{"id": message_id}]}

        def get_message(self, requested_id):
            calls["get_id"] = requested_id
            return {"id": requested_id, "content": "hello"}

    monkeypatch.setattr("ax_cli.commands.messages.get_client", lambda: FakeClient())

    result = runner.invoke(app, ["messages", "get", "12345678", "--json"])
    assert result.exit_code == 0, result.output
    assert calls["list_limit"] == 100
    assert calls["get_id"] == message_id
    assert json.loads(result.output)["id"] == message_id


def test_messages_send_resolves_short_parent_id(monkeypatch):
    parent_id = "abcdef12-3456-7890-abcd-ef1234567890"
    calls = {}

    class FakeClient:
        _base_headers = {}

        def list_messages(self, limit=20, channel="main"):
            calls["list_limit"] = limit
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


def test_messages_edit_and_delete_resolve_short_id_prefix(monkeypatch):
    message_id = "12345678-90ab-cdef-1234-567890abcdef"
    calls = {}

    class FakeClient:
        def list_messages(self, limit=20, channel="main"):
            calls["list_calls"] = calls.get("list_calls", 0) + 1
            return {"messages": [{"id": message_id}]}

        def edit_message(self, requested_id, content):
            calls["edit"] = {"id": requested_id, "content": content}
            return {"id": requested_id, "content": content}

        def delete_message(self, requested_id):
            calls["delete_id"] = requested_id

    monkeypatch.setattr("ax_cli.commands.messages.get_client", lambda: FakeClient())

    edit_result = runner.invoke(app, ["messages", "edit", "12345678", "updated", "--json"])
    assert edit_result.exit_code == 0, edit_result.output
    assert calls["edit"] == {"id": message_id, "content": "updated"}

    delete_result = runner.invoke(app, ["messages", "delete", "12345678", "--json"])
    assert delete_result.exit_code == 0, delete_result.output
    assert calls["delete_id"] == message_id
    assert json.loads(delete_result.output)["message_id"] == message_id
