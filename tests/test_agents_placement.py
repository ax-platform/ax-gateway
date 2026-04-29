"""Tests for ``ax agents placement get/set`` — GATEWAY-PLACEMENT-POLICY-001 CLI consumer."""

from __future__ import annotations

import json

import httpx
from typer.testing import CliRunner

from ax_cli.main import app

runner = CliRunner()


class _FakeClient:
    def __init__(self, agent_record=None, set_response=None, raise_set=None):
        self._agent = agent_record or {"agent": {"id": "a-1", "name": "test", "space_id": "s-1", "pinned": False}}
        self._set_response = set_response or {"agent": {"id": "a-1", "name": "test", "space_id": "s-2", "pinned": True}}
        self._raise_set = raise_set
        self.set_calls: list[dict] = []

    def get_agent(self, identifier):
        return self._agent

    def get_agent_placement(self, name_or_id):
        # Mirror the real client method: extract placement-relevant fields from the record
        record = self._agent.get("agent", self._agent) if isinstance(self._agent, dict) else {}
        return {
            "agent_id": record.get("id"),
            "name": record.get("name"),
            "space_id": record.get("space_id"),
            "pinned": record.get("pinned"),
            "allowed_spaces": record.get("allowed_spaces"),
            "placement": record.get("placement"),
            "placement_state": record.get("placement_state"),
            "_record": record,
        }

    def set_agent_placement(self, name_or_id, *, space_id, pinned=False):
        if self._raise_set:
            raise self._raise_set
        self.set_calls.append({"name": name_or_id, "space_id": space_id, "pinned": pinned})
        return self._set_response


def _install(monkeypatch, client):
    monkeypatch.setattr("ax_cli.commands.agents.get_client", lambda: client)


def test_get_renders_basic_placement(monkeypatch):
    fake = _FakeClient(agent_record={"agent": {
        "id": "a-1", "name": "frontend_sentinel",
        "space_id": "space-prod", "pinned": True,
    }})
    _install(monkeypatch, fake)

    result = runner.invoke(app, ["agents", "placement", "get", "frontend_sentinel", "--json"])
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    assert record["name"] == "frontend_sentinel"
    assert record["space_id"] == "space-prod"
    assert record["pinned"] is True


def test_get_human_output_renders_table(monkeypatch):
    fake = _FakeClient(agent_record={"agent": {
        "id": "a-1", "name": "demo-bot",
        "space_id": "space-abc", "pinned": False,
    }})
    _install(monkeypatch, fake)
    result = runner.invoke(app, ["agents", "placement", "get", "demo-bot"])
    assert result.exit_code == 0, result.output
    assert "@demo-bot" in result.output
    assert "space-abc" in result.output


def test_get_renders_allowed_spaces_when_present(monkeypatch):
    fake = _FakeClient(agent_record={"agent": {
        "id": "a-1", "name": "multi-space",
        "space_id": "space-default",
        "pinned": False,
        "allowed_spaces": ["space-a", "space-b", "space-c"],
    }})
    _install(monkeypatch, fake)
    result = runner.invoke(app, ["agents", "placement", "get", "multi-space"])
    assert result.exit_code == 0, result.output
    assert "allowed_spaces" in result.output
    assert "space-a" in result.output


def test_get_forward_compat_v4_placement_fields(monkeypatch):
    """When backend ships GATEWAY-PLACEMENT-POLICY-001 fields, CLI renders them transparently."""
    fake = _FakeClient(agent_record={"agent": {
        "id": "a-1", "name": "future-bot",
        "space_id": "space-curr",
        "pinned": False,
        "placement": {
            "policy_kind": "allowed",
            "current_space": "space-curr",
            "current_space_set_by": "ax_ui",
            "policy_revision": 3,
        },
        "placement_state": "acked",
    }})
    _install(monkeypatch, fake)
    result = runner.invoke(app, ["agents", "placement", "get", "future-bot"])
    assert result.exit_code == 0, result.output
    assert "policy_kind" in result.output
    assert "allowed" in result.output
    assert "ax_ui" in result.output
    assert "acked" in result.output


def test_set_calls_endpoint_with_pinned(monkeypatch):
    fake = _FakeClient()
    _install(monkeypatch, fake)
    result = runner.invoke(
        app,
        ["agents", "placement", "set", "demo-bot", "--space-id", "s-2", "--pinned", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert len(fake.set_calls) == 1
    call = fake.set_calls[0]
    assert call["space_id"] == "s-2"
    assert call["pinned"] is True


def test_set_default_unpinned(monkeypatch):
    fake = _FakeClient()
    _install(monkeypatch, fake)
    result = runner.invoke(
        app,
        ["agents", "placement", "set", "demo-bot", "--space-id", "s-2", "--json"],
    )
    assert result.exit_code == 0, result.output
    call = fake.set_calls[0]
    assert call["pinned"] is False


def test_set_human_output_confirms(monkeypatch):
    fake = _FakeClient(set_response={"agent": {
        "id": "a-1", "name": "demo-bot", "space_id": "s-2", "pinned": True,
    }})
    _install(monkeypatch, fake)
    result = runner.invoke(
        app,
        ["agents", "placement", "set", "demo-bot", "--space-id", "s-2", "--pinned"],
    )
    assert result.exit_code == 0, result.output
    assert "Updated" in result.output
    assert "@demo-bot" in result.output
    assert "s-2" in result.output
    assert "pinned" in result.output


def test_set_surfaces_403_clearly(monkeypatch):
    """Backend returns 403 when user isn't a member of target space — CLI surfaces it."""
    class _Resp:
        status_code = 403
        text = '{"detail":"User is not a member of target space"}'

    err = httpx.HTTPStatusError("403", request=None, response=_Resp())
    fake = _FakeClient(raise_set=err)
    _install(monkeypatch, fake)
    result = runner.invoke(
        app,
        ["agents", "placement", "set", "demo-bot", "--space-id", "s-other"],
    )
    assert result.exit_code != 0


def test_get_missing_space_id_renders_dash(monkeypatch):
    fake = _FakeClient(agent_record={"agent": {
        "id": "a-1", "name": "no-space-bot",
    }})
    _install(monkeypatch, fake)
    result = runner.invoke(app, ["agents", "placement", "get", "no-space-bot"])
    assert result.exit_code == 0, result.output
    assert "—" in result.output  # em-dash for missing space


def test_set_agent_placement_client_method():
    """The client method itself: POSTs the right body shape."""
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
                    return {"agent": {"id": "x", "space_id": json["space_id"], "pinned": json["pinned"]}}

            return _R()

    client = AxClient.__new__(AxClient)
    client._http = _FakeHttp()
    client._parse_json = lambda r: r.json()

    client.set_agent_placement("demo-bot", space_id="s-1", pinned=True)
    assert captured["path"] == "/api/v1/agents/demo-bot/placement"
    assert captured["body"] == {"space_id": "s-1", "pinned": True}
