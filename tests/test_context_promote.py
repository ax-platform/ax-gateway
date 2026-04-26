"""Tests for ``ax context promote <key>`` — closes the late-promotion gap.

Per CONTEXT-VAULT-AUDIT-2026-04-26 finding #1 + cipher 02:05 UTC greenlight.
"""

from __future__ import annotations

import json

import httpx
from typer.testing import CliRunner

from ax_cli.main import app

runner = CliRunner()


class _FakeClient:
    def __init__(self, response=None, raise_err=None):
        self._response = response or {
            "key": "test-key",
            "artifact_type": "RESEARCH",
            "promoted_at": "2026-04-26T02:00:00Z",
            "storage": "vault",
        }
        self._raise = raise_err
        self.calls: list[dict] = []

    def promote_context(self, space_id, key, *, artifact_type="RESEARCH", agent_id=None):
        if self._raise:
            raise self._raise
        self.calls.append({
            "space_id": space_id,
            "key": key,
            "artifact_type": artifact_type,
            "agent_id": agent_id,
        })
        return self._response


def _install(monkeypatch, client):
    monkeypatch.setattr("ax_cli.commands.context.get_client", lambda: client)
    monkeypatch.setattr(
        "ax_cli.commands.context.resolve_space_id",
        lambda c, explicit=None: explicit or "space-default",
    )


def test_promote_default_artifact_type(monkeypatch):
    fake = _FakeClient()
    _install(monkeypatch, fake)
    result = runner.invoke(app, ["context", "promote", "q1-report"])
    assert result.exit_code == 0, result.output
    assert "Promoted: q1-report" in result.output
    assert "RESEARCH" in result.output
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["space_id"] == "space-default"
    assert call["key"] == "q1-report"
    assert call["artifact_type"] == "RESEARCH"
    assert call["agent_id"] is None


def test_promote_with_explicit_artifact_type(monkeypatch):
    fake = _FakeClient()
    _install(monkeypatch, fake)
    result = runner.invoke(app, ["context", "promote", "design-doc", "--artifact-type", "DESIGN"])
    assert result.exit_code == 0, result.output
    assert fake.calls[0]["artifact_type"] == "DESIGN"
    assert "DESIGN" in result.output


def test_promote_with_agent_id_attribution(monkeypatch):
    fake = _FakeClient()
    _install(monkeypatch, fake)
    result = runner.invoke(
        app,
        ["context", "promote", "shared-state", "--agent-id", "6acc502d-xyz"],
    )
    assert result.exit_code == 0, result.output
    assert fake.calls[0]["agent_id"] == "6acc502d-xyz"


def test_promote_passes_through_unknown_artifact_type(monkeypatch):
    """Forward-compat: unknown artifact_type values pass through to backend."""
    fake = _FakeClient()
    _install(monkeypatch, fake)
    result = runner.invoke(
        app,
        ["context", "promote", "evolving-key", "--artifact-type", "FUTURE_TYPE_XYZ"],
    )
    assert result.exit_code == 0, result.output
    assert fake.calls[0]["artifact_type"] == "FUTURE_TYPE_XYZ"


def test_promote_with_explicit_space_id(monkeypatch):
    fake = _FakeClient()
    _install(monkeypatch, fake)
    result = runner.invoke(
        app,
        ["context", "promote", "k", "--space-id", "space-other"],
    )
    assert result.exit_code == 0, result.output
    assert fake.calls[0]["space_id"] == "space-other"


def test_promote_json_output(monkeypatch):
    fake = _FakeClient(response={
        "key": "report-2024",
        "artifact_type": "RESEARCH",
        "promoted_at": "2026-04-26T02:00:00Z",
        "storage": "vault",
        "version": 1,
    })
    _install(monkeypatch, fake)
    result = runner.invoke(app, ["context", "promote", "report-2024", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["key"] == "report-2024"
    assert payload["storage"] == "vault"


def test_promote_404_when_key_doesnt_exist(monkeypatch):
    """If the ephemeral key isn't there, backend returns 404 — surface cleanly."""
    class _Resp:
        status_code = 404
        text = '{"detail":"Key not found in ephemeral context"}'

    err = httpx.HTTPStatusError("404", request=None, response=_Resp())
    fake = _FakeClient(raise_err=err)
    _install(monkeypatch, fake)
    result = runner.invoke(app, ["context", "promote", "nonexistent-key"])
    assert result.exit_code != 0


def test_promote_403_when_unauthorized(monkeypatch):
    """If user lacks promote permission for the space, backend returns 403."""
    class _Resp:
        status_code = 403
        text = '{"detail":"Forbidden"}'

    err = httpx.HTTPStatusError("403", request=None, response=_Resp())
    fake = _FakeClient(raise_err=err)
    _install(monkeypatch, fake)
    result = runner.invoke(app, ["context", "promote", "k"])
    assert result.exit_code != 0


def test_promote_short_form_artifact_type_flag(monkeypatch):
    """``-t`` is the short alias for ``--artifact-type``."""
    fake = _FakeClient()
    _install(monkeypatch, fake)
    result = runner.invoke(app, ["context", "promote", "k", "-t", "CODE"])
    assert result.exit_code == 0, result.output
    assert fake.calls[0]["artifact_type"] == "CODE"


def test_promote_client_method_request_body():
    """The client method itself: POSTs the right body shape with optional agent_id."""
    from ax_cli.client import AxClient

    captured = {}

    class _FakeHttp:
        def post(self, path, json=None, **_kw):
            captured["path"] = path
            captured["body"] = json

            class _R:
                status_code = 200

                def raise_for_status(self):
                    return None

                def json(self):
                    return {"key": json["key"], "storage": "vault"}

            return _R()

    client = AxClient.__new__(AxClient)
    client._http = _FakeHttp()
    client._parse_json = lambda r: r.json()

    # Without agent_id
    client.promote_context("space-1", "key-1", artifact_type="RESEARCH")
    assert captured["path"] == "/api/v1/spaces/space-1/intelligence/promote"
    assert captured["body"] == {"key": "key-1", "artifact_type": "RESEARCH"}

    # With agent_id
    client.promote_context("space-1", "key-2", artifact_type="CODE", agent_id="agent-x")
    assert captured["body"] == {"key": "key-2", "artifact_type": "CODE", "agent_id": "agent-x"}
