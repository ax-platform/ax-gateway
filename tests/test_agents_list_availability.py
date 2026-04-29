"""Tests for ``ax agents list --availability`` — bulk AVAIL-CONTRACT consumer."""

from __future__ import annotations

import json

import httpx
from typer.testing import CliRunner

from ax_cli.main import app

runner = CliRunner()


class _FakeClient:
    def __init__(self, availability_payload=None, list_payload=None, raise_404_on_availability=False):
        self._availability = availability_payload
        self._list = list_payload or {"agents": []}
        self._raise_404 = raise_404_on_availability

    def list_agents_availability(self, **_kw):
        if self._raise_404:
            class _Resp:
                status_code = 404
                text = "not found"

            raise httpx.HTTPStatusError("not found", request=None, response=_Resp())
        return self._availability

    def list_agents(self, **_kw):
        return self._list


def _install(monkeypatch, client):
    monkeypatch.setattr("ax_cli.commands.agents.get_client", lambda: client)
    monkeypatch.setattr("ax_cli.commands.agents.resolve_space_id", lambda c, explicit=None: explicit or "space-abc")


def test_list_default_path_uses_legacy_endpoint(monkeypatch):
    """No --availability: hits /agents and renders the legacy 3-column table."""
    fake = _FakeClient(list_payload={"agents": [
        {"id": "a-1", "name": "frontend_sentinel", "status": "active"},
    ]})
    _install(monkeypatch, fake)

    result = runner.invoke(app, ["agents", "list", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert rows[0]["name"] == "frontend_sentinel"


def test_list_availability_renders_v4_fields(monkeypatch):
    """--availability: hits /availability, unwraps agent_state envelopes, renders v4 columns."""
    fake = _FakeClient(availability_payload=[
        {
            "agent_state": {
                "agent_id": "a-1",
                "name": "frontend_sentinel",
                "badge_state": "live",
                "badge_label": "Live",
                "connection_path": "gateway_managed",
                "expected_response": "immediate",
                "confidence": "high",
                "last_seen_at": "2026-04-25T17:00:00Z",
            },
            "raw_presence": {"sources": ["gateway"]},
            "control": {"enabled": True},
        },
        {
            "agent_state": {
                "agent_id": "a-2",
                "name": "backend_sentinel",
                "badge_state": "routable_delayed",
                "badge_label": "Warming",
                "connection_path": "gateway_managed",
                "expected_response": "warming",
                "confidence": "medium",
                "last_seen_at": "2026-04-25T16:55:00Z",
            },
        },
    ])
    _install(monkeypatch, fake)

    result = runner.invoke(app, ["agents", "list", "--availability", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert len(rows) == 2
    assert rows[0]["name"] == "frontend_sentinel"
    assert rows[0]["badge_label"] == "Live"
    assert rows[0]["connection_path"] == "gateway_managed"
    assert rows[0]["_raw_presence"]["sources"] == ["gateway"]
    assert rows[0]["_control"]["enabled"] is True
    assert rows[1]["badge_label"] == "Warming"


def test_list_availability_human_output_renders_columns(monkeypatch):
    """Human-readable --availability output includes badge + path columns."""
    fake = _FakeClient(availability_payload=[
        {
            "agent_state": {
                "agent_id": "a-1",
                "name": "night_owl",
                "badge_state": "live",
                "badge_label": "Live",
                "connection_path": "gateway_managed",
                "expected_response": "immediate",
                "confidence": "high",
            },
        },
        {
            "agent_state": {
                "agent_id": "a-2",
                "name": "ax_concierge",
                "badge_state": "routable_delayed",
                "badge_label": "Dispatch",
                "connection_path": "mcp_only",
                "expected_response": "dispatch_delayed",
                "confidence": "medium",
            },
        },
    ])
    _install(monkeypatch, fake)

    result = runner.invoke(app, ["agents", "list", "--availability"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Live" in out
    assert "Dispatch" in out
    assert "Gateway" in out  # short-form connection_path
    assert "Cloud" in out  # mcp_only short-form
    assert "night_owl" in out
    assert "ax_concierge" in out


def test_list_availability_falls_back_to_legacy_on_404(monkeypatch):
    """When /availability returns 404, fall back to /agents and mark rows _legacy."""
    fake = _FakeClient(
        raise_404_on_availability=True,
        list_payload={"agents": [{"id": "a-1", "name": "old_school", "status": "active"}]},
    )
    _install(monkeypatch, fake)

    result = runner.invoke(app, ["agents", "list", "--availability", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert rows[0]["name"] == "old_school"
    # Fallback path marks the row legacy so downstream renderers know to use status
    assert rows[0]["_legacy"] is True


def test_list_availability_filter_requires_availability_flag(monkeypatch):
    """--filter without --availability fails fast with a clear error."""
    fake = _FakeClient()
    _install(monkeypatch, fake)

    result = runner.invoke(app, ["agents", "list", "--filter", "available_now"])
    assert result.exit_code != 0
    assert "filter" in result.output.lower()


def test_list_agents_availability_passes_filter_query_params():
    """The client method itself: filter parameter goes into query string."""
    from ax_cli.client import AxClient

    captured = {}

    class _FakeHttp:
        def get(self, path, params=None, **_kw):
            captured["path"] = path
            captured["params"] = params

            class _R:
                status_code = 200

                def raise_for_status(self):
                    return None

                def json(self):
                    return []

            return _R()

    client = AxClient.__new__(AxClient)
    client._http = _FakeHttp()
    client._parse_json = lambda r: r.json()

    client.list_agents_availability(
        space_id="s-1",
        connection_path="gateway_managed",
        filter_="available_now",
    )
    assert captured["path"] == "/api/v1/agents/availability"
    assert captured["params"]["space_id"] == "s-1"
    assert captured["params"]["connection_path"] == "gateway_managed"
    assert captured["params"]["filter"] == "available_now"


def test_normalize_availability_handles_flat_list_without_envelope(monkeypatch):
    """Backward-compat: if backend returns flat agent_state objects directly (no envelope)."""
    from ax_cli.commands.agents import _normalize_availability_rows

    payload = [
        {"name": "flat", "badge_state": "live", "badge_label": "Live"},
    ]
    rows = _normalize_availability_rows(payload)
    assert len(rows) == 1
    assert rows[0]["name"] == "flat"
    assert rows[0]["badge_label"] == "Live"
    assert "_raw_presence" not in rows[0]


def test_normalize_availability_handles_dict_wrapped_payload():
    """Backward-compat: backend may return ``{agents: [...]}`` or ``{availability: [...]}``."""
    from ax_cli.commands.agents import _normalize_availability_rows

    rows = _normalize_availability_rows({"availability": [{"name": "x"}]})
    assert len(rows) == 1
    assert rows[0]["name"] == "x"

    rows = _normalize_availability_rows({"agents": [{"name": "y"}]})
    assert len(rows) == 1
    assert rows[0]["name"] == "y"
