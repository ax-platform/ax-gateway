"""Tests for ``ax agents check`` — AVAIL-CONTRACT-001 forward-compat CLI consumer."""

from __future__ import annotations

import json
from typing import Any

from typer.testing import CliRunner

from ax_cli.main import app

runner = CliRunner()


class _FakeClient:
    """Stub client that returns a configurable presence record."""

    def __init__(self, presence_record: dict[str, Any], list_payload: dict | list | None = None) -> None:
        self._presence = presence_record
        self._list = list_payload

    def list_agents(self, *, space_id: str | None = None, limit: int | None = None) -> dict | list:
        return self._list if self._list is not None else {"agents": []}

    def get_agent_presence(self, name_or_id: str) -> dict:
        # Mirror the real client's pass-through behavior; tests inject the record directly.
        return self._presence


def _install(monkeypatch, client: _FakeClient) -> None:
    monkeypatch.setattr("ax_cli.commands.agents.get_client", lambda: client)


def test_check_renders_basic_presence_shape(monkeypatch):
    """Today's backend returns the basic presence shape — CLI renders it cleanly."""
    fake = _FakeClient({
        "agent_id": "abc-123",
        "name": "frontend_sentinel",
        "presence": "online",
        "responsive": True,
        "last_active": "2026-04-25T15:00:00Z",
        "agent_type": "assistant",
    })
    _install(monkeypatch, fake)

    result = runner.invoke(app, ["agents", "check", "frontend_sentinel", "--json"])
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    assert record["name"] == "frontend_sentinel"
    assert record["presence"] == "online"
    assert record["responsive"] is True


def test_check_renders_avail_contract_v4_dto_forward_compat(monkeypatch):
    """When backend ships the AVAIL-CONTRACT v4 fields, CLI renders them transparently."""
    fake = _FakeClient({
        "agent_id": "abc-123",
        "name": "backend_sentinel",
        "presence": "offline",
        "responsive": False,
        "last_active": "2026-04-25T14:00:00Z",
        "agent_type": "assistant",
        # Forward-compat AVAIL-CONTRACT v4 fields
        "expected_response": "warming",
        "badge_state": "routable_delayed",
        "badge_label": "Warming",
        "badge_color": "info",
        "connection_path": "gateway_managed",
        "confidence": "medium",
        "unavailable_reason": None,
        "status_explanation": "On-demand. Last warmed 12 min ago. A new mention will spawn the runtime.",
        "pre_send_warning": {
            "severity": "info",
            "title": "Delivery may be delayed",
            "body": "This agent is on-demand; a new mention will warm the runtime.",
        },
    })
    _install(monkeypatch, fake)

    result = runner.invoke(app, ["agents", "check", "backend_sentinel", "--json"])
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    # All v4 fields preserved in --json output
    assert record["expected_response"] == "warming"
    assert record["badge_state"] == "routable_delayed"
    assert record["badge_label"] == "Warming"
    assert record["connection_path"] == "gateway_managed"
    assert record["confidence"] == "medium"
    assert record["pre_send_warning"]["severity"] == "info"
    assert record["pre_send_warning"]["title"] == "Delivery may be delayed"


def test_check_human_output_renders_badge_label_when_present(monkeypatch):
    """Human-readable output uses the rich badge when backend provides it."""
    fake = _FakeClient({
        "agent_id": "abc-123",
        "name": "night_owl",
        "presence": "online",
        "responsive": True,
        "last_active": "2026-04-25T15:00:00Z",
        "badge_state": "live",
        "badge_label": "Live",
        "expected_response": "immediate",
        "connection_path": "gateway_managed",
        "confidence": "high",
    })
    _install(monkeypatch, fake)

    result = runner.invoke(app, ["agents", "check", "night_owl"])
    assert result.exit_code == 0, result.output
    assert "Live" in result.output
    assert "@night_owl" in result.output
    assert "gateway_managed" in result.output
    assert "high" in result.output


def test_check_human_output_falls_back_to_basic_presence(monkeypatch):
    """When backend has no v4 fields, render basic ONLINE/OFFLINE."""
    fake = _FakeClient({
        "agent_id": "abc-123",
        "name": "old_agent",
        "presence": "offline",
        "responsive": False,
        "last_active": None,
    })
    _install(monkeypatch, fake)

    result = runner.invoke(app, ["agents", "check", "old_agent"])
    assert result.exit_code == 0, result.output
    assert "OFFLINE" in result.output
    assert "@old_agent" in result.output


def test_check_renders_pre_send_warning_when_present(monkeypatch):
    """pre_send_warning renders as a colored callout below the fields table."""
    fake = _FakeClient({
        "agent_id": "abc-123",
        "name": "stuck_agent",
        "presence": "online",
        "badge_state": "blocked",
        "badge_label": "Stuck",
        "expected_response": "unlikely",
        "unavailable_reason": "runtime_stuck",
        "pre_send_warning": {
            "severity": "warning",
            "title": "Agent appears stuck",
            "body": "Heartbeat hasn't fired in 10 minutes. Send anyway?",
        },
    })
    _install(monkeypatch, fake)

    result = runner.invoke(app, ["agents", "check", "stuck_agent"])
    assert result.exit_code == 0, result.output
    assert "Agent appears stuck" in result.output
    assert "Heartbeat hasn't fired" in result.output


def test_check_unknown_agent_returns_nonzero(monkeypatch):
    """Agent not found surfaces a clean error, not a stack trace."""

    class _NotFoundClient:
        def list_agents(self, **_kw):
            return {"agents": []}

        def get_agent_presence(self, _id):
            raise RuntimeError("agent not found: ghost")

    monkeypatch.setattr("ax_cli.commands.agents.get_client", lambda: _NotFoundClient())
    result = runner.invoke(app, ["agents", "check", "ghost", "--json"])
    assert result.exit_code != 0
    assert "ghost" in result.output


def test_get_agent_presence_resolves_name_to_id():
    """The client method itself: name → id lookup via list_agents, then GET state/presence."""
    import httpx

    from ax_cli.client import AxClient

    list_called = {"n": 0}
    presence_id = {"v": None}

    class _FakeHttp:
        def get(self, path, **_kw):
            class _R:
                def __init__(self, data, status=200):
                    self._data = data
                    self.status_code = status

                def raise_for_status(self):
                    if self.status_code >= 400:
                        raise httpx.HTTPStatusError("err", request=None, response=self)

                def json(self):
                    return self._data

            if path == "/api/v1/agents":
                list_called["n"] += 1
                return _R({"agents": [{"id": "uuid-aaa-1", "name": "foo"}]})
            # Simulate legacy backend (no /state) — falls back to /presence
            if path.startswith("/api/v1/agents/uuid-aaa-1/state"):
                return _R({}, status=404)
            if path.startswith("/api/v1/agents/uuid-aaa-1/presence"):
                presence_id["v"] = "uuid-aaa-1"
                return _R({"agent_id": "uuid-aaa-1", "name": "foo", "presence": "online"})
            raise AssertionError(f"unexpected path {path}")

    client = AxClient.__new__(AxClient)
    client._http = _FakeHttp()
    client._parse_json = lambda r: r.json()

    record = client.get_agent_presence("foo")
    assert record["name"] == "foo"
    assert record["presence"] == "online"
    assert list_called["n"] == 1
    assert presence_id["v"] == "uuid-aaa-1"


def test_get_agent_presence_uses_uuid_directly():
    """If name_or_id is already a UUID, skip the list lookup."""
    import httpx

    from ax_cli.client import AxClient

    list_called = {"n": 0}

    class _FakeHttp:
        def get(self, path, **_kw):
            class _R:
                def __init__(self, data, status=200):
                    self._data = data
                    self.status_code = status

                def raise_for_status(self):
                    if self.status_code >= 400:
                        raise httpx.HTTPStatusError("err", request=None, response=self)

                def json(self):
                    return self._data

            if path == "/api/v1/agents":
                list_called["n"] += 1
                return _R({"agents": []})
            # Pretend /state returns 404 (legacy backend) so we fall back to /presence.
            if path.endswith("/state"):
                return _R({}, status=404)
            if path.endswith("/presence"):
                return _R({"agent_id": "12345678-1234-1234-1234-123456789abc", "presence": "online"})
            raise AssertionError(f"unexpected path {path}")

    client = AxClient.__new__(AxClient)
    client._http = _FakeHttp()
    client._parse_json = lambda r: r.json()

    record = client.get_agent_presence("12345678-1234-1234-1234-123456789abc")
    assert record["presence"] == "online"
    assert list_called["n"] == 0, "UUID input should NOT trigger a list_agents call"


def test_get_agent_presence_prefers_state_endpoint_and_unwraps_envelope():
    """When /state is available, prefer it and unwrap the agent_state envelope."""
    from ax_cli.client import AxClient

    paths_seen = []

    class _FakeHttp:
        def get(self, path, **_kw):
            paths_seen.append(path)

            class _R:
                def __init__(self, data):
                    self._data = data
                    self.status_code = 200

                def raise_for_status(self):
                    return None

                def json(self):
                    return self._data

            if path.endswith("/state"):
                return _R({
                    "agent_state": {
                        "agent_id": "uuid-zzz",
                        "name": "richy",
                        "expected_response": "immediate",
                        "badge_state": "live",
                        "badge_label": "Live",
                        "connection_path": "gateway_managed",
                        "confidence": "high",
                    },
                    "raw_presence": {"sources": ["gateway"]},
                    "control": {"enabled": True, "quarantined": False},
                })
            raise AssertionError(f"unexpected path {path}")

    client = AxClient.__new__(AxClient)
    client._http = _FakeHttp()
    client._parse_json = lambda r: r.json()

    record = client.get_agent_presence("12345678-1234-1234-1234-123456789abc")
    # Top-level fields are unwrapped from agent_state
    assert record["expected_response"] == "immediate"
    assert record["badge_state"] == "live"
    # Envelope siblings preserved with underscore prefix for diagnostics
    assert record["_raw_presence"]["sources"] == ["gateway"]
    assert record["_control"]["enabled"] is True
    # /presence was NOT hit — /state succeeded
    assert "/presence" not in " ".join(paths_seen)
