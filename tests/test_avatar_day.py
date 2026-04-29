"""Tests for the 'avatar day' fixes — see shared/state/axctl-friction-2026-04-17.md
sections 7, 9, 10, 11, and 2 (effective-config printer)."""

from __future__ import annotations

import base64

import click
import httpx
import pytest
import typer
from typer.testing import CliRunner

from ax_cli.commands import agents as agents_cmd
from ax_cli.commands.agents import (
    AVATAR_URL_MAX_LENGTH,
    _build_avatar_data_uri_from_file,
    _check_avatar_url_length,
)
from ax_cli.main import app

# Click 8.3+ always separates stdout/stderr on CliRunner.Result. We assert
# on result.stderr for the effective-config-line tests so the preamble
# is proved to NOT leak into stdout (which --json consumers / pipes read).
runner = CliRunner()
split_runner = runner


# ── helpers ─────────────────────────────────────────────────────────────────


class _RecordingHttp:
    """Minimal httpx.Client stand-in that records method+url+json."""

    def __init__(self, status_code: int = 200, response_json: dict | None = None):
        self.calls: list[dict] = []
        self._status_code = status_code
        self._response_json = response_json or {}

    def _make_response(self) -> httpx.Response:
        request = httpx.Request("PUT", "http://test.local/")
        return httpx.Response(self._status_code, json=self._response_json, request=request)

    def put(self, url: str, json=None, **kwargs):
        self.calls.append({"method": "PUT", "url": url, "json": json})
        return self._make_response()

    def patch(self, url: str, json=None, **kwargs):
        self.calls.append({"method": "PATCH", "url": url, "json": json})
        return self._make_response()


class _FakeClient:
    def __init__(self, http: _RecordingHttp):
        self._http = http
        self.updated = None

    def list_agents(self, *, space_id=None, limit=None):
        return {"agents": [{"id": "agent-1", "name": "axolotl"}]}

    def update_agent(self, identifier, **fields):
        self.updated = {"identifier": identifier, **fields}
        return {"id": "agent-1", "name": identifier, **fields}


# ── #9 — avatar_url length cap is enforced client-side ─────────────────────


def test_avatar_url_length_ok_under_cap():
    # Should not raise for a URL at exactly the cap
    _check_avatar_url_length("x" * AVATAR_URL_MAX_LENGTH)


def test_avatar_url_length_rejected_over_cap():
    long = "x" * (AVATAR_URL_MAX_LENGTH + 1)
    with pytest.raises((typer.Exit, click.exceptions.Exit)) as exc:
        _check_avatar_url_length(long)
    # Both typer.Exit and click.exceptions.Exit expose .exit_code
    assert getattr(exc.value, "exit_code", getattr(exc.value, "code", None)) == 1


# ── #10 — `--avatar-file` encodes & wires through update_agent ─────────────


def test_build_avatar_data_uri_from_file_svg(tmp_path):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    f = tmp_path / "tiny.svg"
    f.write_bytes(svg)
    uri = _build_avatar_data_uri_from_file(str(f))
    assert uri.startswith("data:image/svg+xml;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == svg


def test_build_avatar_data_uri_from_file_png(tmp_path):
    f = tmp_path / "tiny.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    uri = _build_avatar_data_uri_from_file(str(f))
    assert uri.startswith("data:image/png;base64,")


def test_update_avatar_url_flag_passes_through(monkeypatch, tmp_path):
    fake = _FakeClient(_RecordingHttp())
    monkeypatch.setattr(agents_cmd, "get_client", lambda: fake)

    small_uri = "data:image/svg+xml;base64," + base64.b64encode(b"<svg/>").decode()
    assert len(small_uri) <= AVATAR_URL_MAX_LENGTH

    result = runner.invoke(app, ["agents", "update", "axolotl", "--avatar-url", small_uri])
    assert result.exit_code == 0, result.output
    assert fake.updated == {"identifier": "axolotl", "avatar_url": small_uri}


def test_update_avatar_file_flag_reads_and_encodes(monkeypatch, tmp_path):
    fake = _FakeClient(_RecordingHttp())
    monkeypatch.setattr(agents_cmd, "get_client", lambda: fake)

    svg = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    f = tmp_path / "a.svg"
    f.write_bytes(svg)

    result = runner.invoke(app, ["agents", "update", "axolotl", "--avatar-file", str(f)])
    assert result.exit_code == 0, result.output
    assert fake.updated["identifier"] == "axolotl"
    assert fake.updated["avatar_url"].startswith("data:image/svg+xml;base64,")


def test_update_avatar_mutually_exclusive(monkeypatch):
    result = runner.invoke(
        app,
        [
            "agents",
            "update",
            "axolotl",
            "--avatar-url",
            "data:image/svg+xml;base64,X",
            "--avatar-file",
            "/tmp/x.svg",
        ],
    )
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output


def test_update_avatar_file_rejected_over_cap(monkeypatch, tmp_path):
    fake = _FakeClient(_RecordingHttp())
    monkeypatch.setattr(agents_cmd, "get_client", lambda: fake)

    big = tmp_path / "big.svg"
    big.write_bytes(b"x" * 4096)  # > cap when encoded

    result = runner.invoke(app, ["agents", "update", "axolotl", "--avatar-file", str(big)])
    assert result.exit_code == 1, result.output
    assert "backend caps at" in result.output
    assert fake.updated is None, "update_agent must not be called if cap is exceeded"


# ── #7 — `agents avatar --set` uses PUT, not PATCH ─────────────────────────


def test_avatar_set_uses_put_not_patch(monkeypatch):
    http = _RecordingHttp(status_code=200, response_json={"id": "agent-1", "name": "axolotl"})
    fake = _FakeClient(http)
    monkeypatch.setattr(agents_cmd, "get_client", lambda: fake)

    # Provide a tiny in-process avatar generator to avoid drawing a real SVG
    tiny_svg = b"<svg/>"
    tiny_data_uri = "data:image/svg+xml;base64," + base64.b64encode(tiny_svg).decode()

    def _fake_generate(agent, agent_type, size):
        return tiny_svg.decode()

    def _fake_data_uri(agent, agent_type, size):
        return tiny_data_uri

    monkeypatch.setattr("ax_cli.avatar.generate_avatar", _fake_generate)
    monkeypatch.setattr("ax_cli.avatar.avatar_data_uri", _fake_data_uri)

    result = runner.invoke(app, ["agents", "avatar", "axolotl", "--set"])
    assert result.exit_code == 0, result.output

    # Exactly one call, and it's a PUT
    assert len(http.calls) == 1, http.calls
    call = http.calls[0]
    assert call["method"] == "PUT", f"must use PUT, got {call['method']}"
    assert call["url"] == "/api/v1/agents/agent-1"
    assert call["json"] == {"avatar_url": tiny_data_uri}


def test_avatar_set_rejects_oversized_uri(monkeypatch):
    http = _RecordingHttp()
    fake = _FakeClient(http)
    monkeypatch.setattr(agents_cmd, "get_client", lambda: fake)

    monkeypatch.setattr("ax_cli.avatar.generate_avatar", lambda *a, **k: "<svg/>")
    # Intentionally produce an over-cap data URI
    oversized = "data:image/svg+xml;base64," + ("A" * (AVATAR_URL_MAX_LENGTH + 1))
    monkeypatch.setattr("ax_cli.avatar.avatar_data_uri", lambda *a, **k: oversized)

    result = runner.invoke(app, ["agents", "avatar", "axolotl", "--set"])
    assert result.exit_code == 1, result.output
    assert "backend caps at" in result.output
    assert http.calls == [], "no HTTP call should be made when cap is exceeded"


# ── #2 — effective-config line is printed by mutating commands ─────────────


def test_effective_config_line_format():
    line = agents_cmd._effective_config_line()
    # Stripped of Rich markup for simplicity of assertion
    assert "base_url=" in line
    assert "user_env=" in line
    assert "source=" in line


def test_update_prints_effective_config_to_stderr(monkeypatch):
    """The config preamble MUST land on stderr so --json consumers and
    pipes don't see it. See friction §2 + PR #65 review."""
    fake = _FakeClient(_RecordingHttp())
    monkeypatch.setattr(agents_cmd, "get_client", lambda: fake)

    result = split_runner.invoke(app, ["agents", "update", "axolotl", "--bio", "hi"])
    assert result.exit_code == 0, result.stderr
    assert "base_url=" in result.stderr
    assert "user_env=" in result.stderr
    # Critical: the preamble must NOT leak into stdout
    assert "base_url=" not in result.stdout
    assert "user_env=" not in result.stdout


def test_avatar_set_prints_effective_config_to_stderr(monkeypatch):
    """Same stderr invariant for `ax agents avatar --set`."""
    http = _RecordingHttp(status_code=200, response_json={"id": "agent-1", "name": "axolotl"})
    fake = _FakeClient(http)
    monkeypatch.setattr(agents_cmd, "get_client", lambda: fake)
    monkeypatch.setattr("ax_cli.avatar.generate_avatar", lambda *a, **k: "<svg/>")
    small = "data:image/svg+xml;base64," + base64.b64encode(b"<svg/>").decode()
    monkeypatch.setattr("ax_cli.avatar.avatar_data_uri", lambda *a, **k: small)

    result = split_runner.invoke(app, ["agents", "avatar", "axolotl", "--set"])
    assert result.exit_code == 0, result.stderr
    assert "base_url=" in result.stderr
    assert "base_url=" not in result.stdout


def test_update_json_stdout_is_clean_of_config_preamble(monkeypatch):
    """With --json, stdout must be parseable as JSON alone — the preamble
    must stay on stderr."""
    import json as _json

    fake = _FakeClient(_RecordingHttp())
    monkeypatch.setattr(agents_cmd, "get_client", lambda: fake)

    result = split_runner.invoke(app, ["agents", "update", "axolotl", "--bio", "hi", "--json"])
    assert result.exit_code == 0, result.stderr
    # stdout should be parseable as JSON with no preamble contamination
    parsed = _json.loads(result.stdout)
    assert parsed["name"] == "axolotl"
    assert parsed["bio"] == "hi"
    assert "base_url=" in result.stderr
