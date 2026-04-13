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
