import json

from typer.testing import CliRunner

from ax_cli.commands.upload import _message_attachment_ref
from ax_cli.main import app

runner = CliRunner()


def test_upload_message_attachment_ref_keeps_preview_pointers():
    assert _message_attachment_ref(
        attachment_id="att-1",
        content_type="image/png",
        filename="mockup.png",
        size_bytes=123,
        url="/api/v1/uploads/files/mockup.png",
        context_key="upload:123:mockup.png:att-1",
    ) == {
        "id": "att-1",
        "content_type": "image/png",
        "filename": "mockup.png",
        "size_bytes": 123,
        "url": "/api/v1/uploads/files/mockup.png",
        "context_key": "upload:123:mockup.png:att-1",
    }


def test_upload_file_passes_resolved_space_to_upload_api(monkeypatch, tmp_path):
    calls = {}
    sample = tmp_path / "sample.py"
    sample.write_text("print('hello')\n")

    class FakeClient:
        def upload_file(self, path, *, space_id=None):
            calls["upload"] = {"path": path, "space_id": space_id}
            return {
                "attachment_id": "att-1",
                "url": "/api/v1/uploads/files/sample.py",
                "content_type": "text/x-python",
                "size": 15,
                "original_filename": "sample.py",
            }

        def set_context(self, space_id, key, value):
            calls["context"] = {"space_id": space_id, "key": key, "value": value}

        def send_message(self, space_id, content, attachments=None):
            calls["message"] = {
                "space_id": space_id,
                "content": content,
                "attachments": attachments,
            }
            return {"id": "msg-1"}

    monkeypatch.setattr("ax_cli.commands.upload.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.upload.resolve_space_id", lambda client: "space-1")

    result = runner.invoke(
        app,
        [
            "upload",
            "file",
            str(sample),
            "--key",
            "sample-key",
            "--message",
            "@madtank sample",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert calls["upload"]["space_id"] == "space-1"
    assert calls["context"]["space_id"] == "space-1"
    assert calls["message"]["space_id"] == "space-1"


def test_upload_file_vault_stores_context_before_promote(monkeypatch, tmp_path):
    calls = {}
    sample = tmp_path / "vault.md"
    sample.write_text("# Vault\nkeep this\n")

    class FakeClient:
        def upload_file(self, path, *, space_id=None):
            calls["upload"] = {"path": path, "space_id": space_id}
            return {
                "attachment_id": "att-1",
                "url": "/api/v1/uploads/files/vault.md",
                "content_type": "text/markdown",
                "size": sample.stat().st_size,
                "original_filename": "vault.md",
            }

        def set_context(self, space_id, key, value):
            calls["context"] = {"space_id": space_id, "key": key, "value": value}

        def promote_context(self, space_id, key, *, artifact_type="RESEARCH", agent_id=None):
            calls["promote"] = {
                "space_id": space_id,
                "key": key,
                "artifact_type": artifact_type,
                "agent_id": agent_id,
            }
            return {"status": "created", "key": key}

        def send_message(self, space_id, content, attachments=None):
            calls["message"] = {
                "space_id": space_id,
                "content": content,
                "attachments": attachments,
            }
            return {"id": "msg-1"}

    monkeypatch.setattr("ax_cli.commands.upload.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.upload.resolve_space_id", lambda client: "space-1")

    result = runner.invoke(
        app,
        [
            "upload",
            "file",
            str(sample),
            "--vault",
            "--message",
            "@madtank vault smoke",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert calls["context"]["space_id"] == "space-1"
    assert calls["promote"]["space_id"] == "space-1"
    assert calls["promote"]["key"] == calls["context"]["key"]
    assert calls["promote"]["artifact_type"] == "RESEARCH"
    assert json.loads(calls["context"]["value"])["content"] == "# Vault\nkeep this\n"
    assert calls["message"]["attachments"][0]["context_key"] == calls["context"]["key"]
