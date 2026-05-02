import json
from pathlib import Path

from typer.testing import CliRunner

from ax_cli.commands import context
from ax_cli.context_keys import build_upload_context_key
from ax_cli.main import app

runner = CliRunner()


def test_context_download_uses_base_url_and_auth_headers(monkeypatch, tmp_path):
    calls = {}

    class FakeClient:
        base_url = "https://paxai.app"

        def get_context(self, key, *, space_id=None):
            assert key == "image.png"
            assert space_id == "space-1"
            return {
                "value": {
                    "type": "file_upload",
                    "filename": "image.png",
                    "url": "/api/v1/uploads/files/image.png",
                }
            }

        def _auth_headers(self):
            return {
                "Authorization": "Bearer exchanged.jwt",
                "Content-Type": "application/json",
                "X-AX-FP": "fp",
            }

    class FakeResponse:
        content = b"png-bytes"

        def raise_for_status(self):
            return None

    class FakeHttpClient:
        def __init__(self, *, headers, timeout, follow_redirects):
            calls["headers"] = headers
            calls["timeout"] = timeout
            calls["follow_redirects"] = follow_redirects

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def get(self, url, params=None):
            calls["url"] = url
            calls["params"] = params
            return FakeResponse()

    monkeypatch.setattr(context, "get_client", lambda: FakeClient())
    monkeypatch.setattr(context, "resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr(context.httpx, "Client", FakeHttpClient)

    output = tmp_path / "downloaded.png"
    result = runner.invoke(app, ["context", "download", "image.png", "--output", str(output)])

    assert result.exit_code == 0
    assert "Downloaded:" in result.output
    assert "[green]" not in result.output
    assert output.read_bytes() == b"png-bytes"
    assert calls["url"] == "https://paxai.app/api/v1/uploads/files/image.png"
    assert calls["params"] is None
    assert calls["headers"] == {
        "Authorization": "Bearer exchanged.jwt",
        "X-AX-FP": "fp",
    }
    assert calls["follow_redirects"] is True


def test_context_download_rejects_html_shell_for_binary_payload(monkeypatch, tmp_path):
    class FakeClient:
        base_url = "https://paxai.app"

        def get_context(self, key, *, space_id=None):
            assert key == "image.png"
            return {
                "value": {
                    "type": "file_upload",
                    "filename": "image.png",
                    "content_type": "image/png",
                    "url": "/api/v1/uploads/files/image.png",
                }
            }

        def _auth_headers(self):
            return {"Authorization": "Bearer exchanged.jwt"}

    class FakeResponse:
        headers = {"content-type": "text/html; charset=utf-8"}
        content = b"<!DOCTYPE html><html><body>app shell</body></html>"

        def raise_for_status(self):
            return None

    class FakeHttpClient:
        def __init__(self, *, headers, timeout, follow_redirects):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def get(self, url, params=None):
            return FakeResponse()

    monkeypatch.setattr(context, "get_client", lambda: FakeClient())
    monkeypatch.setattr(context, "resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr(context.httpx, "Client", FakeHttpClient)

    output = tmp_path / "downloaded.png"
    result = runner.invoke(app, ["context", "download", "image.png", "--output", str(output)])

    assert result.exit_code == 1
    assert "returned text/html instead" in result.output
    assert "app shell" in result.output
    assert not output.exists()


def test_context_load_fetches_to_preview_cache(monkeypatch, tmp_path):
    calls = {}

    class FakeClient:
        base_url = "https://paxai.app"

        def get_context(self, key, *, space_id=None):
            assert key == "upload-key"
            assert space_id == "space-1"
            return {
                "value": {
                    "type": "file_upload",
                    "filename": "../image.png",
                    "content_type": "image/png",
                    "url": "/api/v1/uploads/files/image.png",
                }
            }

        def _auth_headers(self):
            return {
                "Authorization": "Bearer exchanged.jwt",
                "Content-Type": "application/json",
            }

    class FakeResponse:
        content = b"png-bytes"

        def raise_for_status(self):
            return None

    class FakeHttpClient:
        def __init__(self, *, headers, timeout, follow_redirects):
            calls["headers"] = headers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def get(self, url, params=None):
            calls["url"] = url
            calls["params"] = params
            return FakeResponse()

    monkeypatch.setattr(context, "get_client", lambda: FakeClient())
    monkeypatch.setattr(context, "resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr(context.httpx, "Client", FakeHttpClient)

    result = runner.invoke(
        app,
        [
            "context",
            "load",
            "upload-key",
            "--cache-dir",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    preview_files = list(tmp_path.glob("*/image.png"))
    assert len(preview_files) == 1
    assert preview_files[0].read_bytes() == b"png-bytes"
    assert calls["url"] == "https://paxai.app/api/v1/uploads/files/image.png"
    assert calls["params"] is None
    assert calls["headers"] == {"Authorization": "Bearer exchanged.jwt"}
    assert '"text_like": false' in result.output


def test_context_load_can_include_text_content(monkeypatch, tmp_path):
    class FakeClient:
        base_url = "https://paxai.app"

        def get_context(self, key, *, space_id=None):
            return {
                "value": {
                    "type": "file_upload",
                    "filename": "notes.md",
                    "content_type": "text/markdown",
                    "url": "/api/v1/uploads/files/notes.md",
                }
            }

        def _auth_headers(self):
            return {"Authorization": "Bearer exchanged.jwt"}

    class FakeResponse:
        content = b"# Notes\nUseful context."

        def raise_for_status(self):
            return None

    class FakeHttpClient:
        def __init__(self, *, headers, timeout, follow_redirects):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def get(self, url, params=None):
            return FakeResponse()

    monkeypatch.setattr(context, "get_client", lambda: FakeClient())
    monkeypatch.setattr(context, "resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr(context.httpx, "Client", FakeHttpClient)

    result = runner.invoke(
        app,
        [
            "context",
            "load",
            "notes-key",
            "--cache-dir",
            str(tmp_path),
            "--content",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"text_like": true' in result.output
    assert "# Notes" in result.output


def test_default_upload_context_key_is_unique(monkeypatch):
    monkeypatch.setattr("ax_cli.context_keys.time.time", lambda: 1775880839.429)

    first = build_upload_context_key("image.png", "df9b1d15-e9c5-4e60-851e-53ea35b4f5e7")
    second = build_upload_context_key("image.png", "774758d4-8451-4570-bca4-e4c4d34706ac")

    assert first == "upload:1775880839429:image.png:df9b1d15-e9c5-4e60-851e-53ea35b4f5e7"
    assert second == "upload:1775880839429:image.png:774758d4-8451-4570-bca4-e4c4d34706ac"
    assert first != second


def test_context_upload_file_vault_stores_context_before_promote(monkeypatch, tmp_path):
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

        def set_context(self, space_id, key, value, *, ttl=None):
            calls["context"] = {"space_id": space_id, "key": key, "value": value, "ttl": ttl}
            return {"status": "stored"}

        def promote_context(self, space_id, key, *, artifact_type="RESEARCH", agent_id=None):
            calls["promote"] = {
                "space_id": space_id,
                "key": key,
                "artifact_type": artifact_type,
                "agent_id": agent_id,
            }
            return {"status": "created", "key": key}

    monkeypatch.setattr(context, "get_client", lambda: FakeClient())
    monkeypatch.setattr(context, "resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(
        app,
        [
            "context",
            "upload-file",
            str(sample),
            "--vault",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert calls["upload"]["space_id"] == "space-1"
    assert calls["context"]["space_id"] == "space-1"
    assert calls["promote"]["space_id"] == "space-1"
    assert calls["promote"]["key"] == calls["context"]["key"]
    assert calls["promote"]["artifact_type"] == "RESEARCH"


def test_context_upload_file_mention_sends_context_signal(monkeypatch, tmp_path):
    calls = {}
    sample = tmp_path / "notes.md"
    sample.write_text("# Notes\n")

    class FakeClient:
        def upload_file(self, path, *, space_id=None):
            return {
                "attachment_id": "att-1",
                "url": "/api/v1/uploads/files/notes.md",
                "content_type": "text/markdown",
                "size": sample.stat().st_size,
                "original_filename": "notes.md",
            }

        def set_context(self, space_id, key, value, *, ttl=None):
            calls["context"] = {"space_id": space_id, "key": key, "value": value, "ttl": ttl}
            return {"status": "stored"}

        def send_message(self, space_id, content):
            calls["message"] = {"space_id": space_id, "content": content}
            return {"id": "msg-1"}

    monkeypatch.setattr(context, "get_client", lambda: FakeClient())
    monkeypatch.setattr(context, "resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(
        app,
        ["context", "upload-file", str(sample), "--mention", "@demo-agent", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls["message"]["space_id"] == "space-1"
    assert calls["message"]["content"].startswith("@demo-agent Context uploaded:")
    assert calls["context"]["key"] in calls["message"]["content"]
    assert '"message_id": "msg-1"' in result.output


def test_context_set_mention_sends_context_signal(monkeypatch):
    calls = {}

    class FakeClient:
        def set_context(self, space_id, key, value, *, ttl=None):
            calls["context"] = {"space_id": space_id, "key": key, "value": value, "ttl": ttl}
            return {"status": "stored", "key": key}

        def send_message(self, space_id, content):
            calls["message"] = {"space_id": space_id, "content": content}
            return {"id": "msg-1"}

    monkeypatch.setattr(context, "get_client", lambda: FakeClient())
    monkeypatch.setattr(context, "resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(
        app,
        ["context", "set", "spec:cli", "ready", "--mention", "mcp_sentinel", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls["message"]["content"] == "@mcp_sentinel Context updated: `spec:cli`"
    assert '"message_id": "msg-1"' in result.output


def test_context_fetch_url_upload_stores_renderable_file_upload(monkeypatch):
    calls = {}

    class FakeResponse:
        headers = {"content-type": "text/markdown; charset=utf-8"}
        content = b"# Article\nFetched markdown.\n"
        text = "# Article\nFetched markdown.\n"

        def raise_for_status(self):
            return None

    class FakeClient:
        def upload_file(self, path, *, space_id=None):
            calls["upload"] = {"path": path, "space_id": space_id}
            assert Path(path).name == "article.md"
            assert Path(path).read_bytes() == FakeResponse.content
            return {
                "attachment_id": "att-1",
                "url": "/api/v1/uploads/files/article.md",
                "content_type": "text/markdown",
                "size": len(FakeResponse.content),
                "original_filename": "article.md",
            }

        def set_context(self, space_id, key, value, *, ttl=None):
            calls["context"] = {"space_id": space_id, "key": key, "value": value, "ttl": ttl}
            return {"status": "stored"}

    monkeypatch.setattr(context, "get_client", lambda: FakeClient())
    monkeypatch.setattr(context, "resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr(context.httpx, "get", lambda *args, **kwargs: FakeResponse())

    result = runner.invoke(
        app,
        [
            "context",
            "fetch-url",
            "https://example.com/article.md",
            "--upload",
            "--key",
            "article",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    stored = json.loads(calls["context"]["value"])
    assert stored["type"] == "file_upload"
    assert stored["filename"] == "article.md"
    assert stored["source"] == "url_fetch"
    assert stored["source_url"] == "https://example.com/article.md"
    assert stored["content"] == FakeResponse.text
    assert calls["context"]["key"] == "article"
    output = json.loads(result.output[result.output.index("{") :])
    assert output["type"] == "file_upload"
