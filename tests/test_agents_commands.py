import json

from typer.testing import CliRunner

from ax_cli.main import app

runner = CliRunner()


def test_agents_list_surfaces_control_state(monkeypatch):
    class FakeClient:
        def list_agents(self, *, space_id=None, limit=None):
            return {
                "agents": [
                    {
                        "id": "agent-1",
                        "name": "aX",
                        "status": "active",
                        "control": {
                            "is_disabled": True,
                            "disabled_reason": "manual safety pause",
                        },
                    }
                ]
            }

    monkeypatch.setattr("ax_cli.commands.agents.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.agents.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(app, ["agents", "list"])

    assert result.exit_code == 0, result.output
    assert "aX" in result.output
    assert "active" in result.output
    assert "disabled" in result.output
    assert "manual safety pause" in result.output


def test_agents_ping_does_not_send_when_control_blocks_delivery(monkeypatch):
    calls = {"sent": False}

    class FakeClient:
        def list_agents(self, *, space_id=None, limit=None):
            return {
                "agents": [
                    {
                        "id": "agent-1",
                        "name": "aX",
                        "origin": "space_agent",
                        "agent_type": "space_agent",
                        "status": "active",
                        "control": {
                            "is_disabled": True,
                            "disabled_reason": "manual safety pause",
                        },
                    }
                ]
            }

        def send_message(self, space_id, content):
            calls["sent"] = True
            return {"message": {"id": "msg-1"}}

    monkeypatch.setattr("ax_cli.commands.agents.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.agents.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(app, ["agents", "ping", "aX", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert calls["sent"] is False
    assert data["contact_mode"] == "blocked_by_control"
    assert data["control_status"] == "disabled"
    assert data["control_reason"] == "manual safety pause"


def test_agents_ping_classifies_reply_as_event_listener(monkeypatch):
    calls = {}

    class FakeClient:
        def list_agents(self, *, space_id=None, limit=None):
            calls["list_agents"] = {"space_id": space_id, "limit": limit}
            return {
                "agents": [
                    {
                        "id": "agent-1",
                        "name": "demo-agent",
                        "origin": "mcp",
                        "agent_type": "mcp",
                        "status": "active",
                    }
                ]
            }

        def send_message(self, space_id, content):
            calls["message"] = {"space_id": space_id, "content": content}
            return {"message": {"id": "msg-1"}}

    def fake_wait(client, **kwargs):
        calls["wait"] = kwargs
        return {"id": "reply-1", "content": f"received {kwargs['token']}", "display_name": "demo-agent"}

    monkeypatch.setattr("ax_cli.commands.agents.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.agents.resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr("ax_cli.commands.agents.resolve_agent_name", lambda client=None: "ChatGPT")
    monkeypatch.setattr("ax_cli.commands.agents._wait_for_handoff_reply", fake_wait)

    result = runner.invoke(app, ["agents", "ping", "demo-agent", "--timeout", "5", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["contact_mode"] == "event_listener"
    assert data["listener_status"] == "replied"
    assert data["agent_id"] == "agent-1"
    assert calls["message"]["content"].startswith("@demo-agent Contact-mode ping")
    assert calls["wait"]["agent_name"] == "demo-agent"
    assert calls["wait"]["sent_message_id"] == "msg-1"


def test_agents_ping_classifies_timeout_as_unknown(monkeypatch):
    class FakeClient:
        def list_agents(self, *, space_id=None, limit=None):
            return {
                "agents": [
                    {
                        "id": "agent-1",
                        "name": "mcp_sentinel",
                        "origin": "mcp",
                        "agent_type": "mcp",
                        "status": "active",
                    }
                ]
            }

        def send_message(self, space_id, content):
            return {"message": {"id": "msg-1"}}

    monkeypatch.setattr("ax_cli.commands.agents.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.agents.resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr("ax_cli.commands.agents.resolve_agent_name", lambda client=None: "ChatGPT")
    monkeypatch.setattr("ax_cli.commands.agents._wait_for_handoff_reply", lambda client, **kwargs: None)

    result = runner.invoke(app, ["agents", "ping", "@mcp_sentinel", "--timeout", "5", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["contact_mode"] == "unknown_or_not_listening"
    assert data["listener_status"] == "no_reply"


def test_agents_ping_unknown_agent_fails(monkeypatch):
    class FakeClient:
        def list_agents(self, *, space_id=None, limit=None):
            return {"agents": [{"id": "agent-1", "name": "demo-agent"}]}

    monkeypatch.setattr("ax_cli.commands.agents.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.agents.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(app, ["agents", "ping", "missing"])

    assert result.exit_code == 1
    assert "No visible agent found" in result.output


def test_agents_discover_infers_roles_without_ping(monkeypatch):
    calls = {}

    class FakeClient:
        def list_agents(self, *, space_id=None, limit=None):
            calls["list_agents"] = {"space_id": space_id, "limit": limit}
            return {
                "agents": [
                    {
                        "id": "agent-1",
                        "name": "supervisor_sentinel",
                        "origin": "mcp",
                        "agent_type": "mcp",
                        "status": "active",
                    },
                    {
                        "id": "agent-2",
                        "name": "aX",
                        "origin": "space_agent",
                        "agent_type": "space_agent",
                        "status": "active",
                    },
                    {
                        "id": "agent-3",
                        "name": "night_owl",
                        "origin": "cli",
                        "agent_type": "on_demand",
                        "status": "active",
                    },
                ]
            }

    monkeypatch.setattr("ax_cli.commands.agents.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.agents.resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr("ax_cli.commands.agents.resolve_agent_name", lambda client=None: "ChatGPT")

    result = runner.invoke(app, ["agents", "discover", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    rows = {row["name"]: row for row in data["agents"]}
    assert calls["list_agents"] == {"space_id": "space-1", "limit": 500}
    assert rows["supervisor_sentinel"]["mesh_role"] == "supervisor_candidate"
    assert rows["supervisor_sentinel"]["contact_mode"] == "unknown"
    assert rows["supervisor_sentinel"]["warning"] == "supervisor_candidate_not_live"
    assert rows["aX"]["contact_mode"] == "space_agent"
    assert rows["night_owl"]["contact_mode"] == "on_demand"


def test_agents_discover_marks_control_blocked_agents_without_ping(monkeypatch):
    class FakeClient:
        def list_agents(self, *, space_id=None, limit=None):
            return {
                "agents": [
                    {
                        "id": "agent-1",
                        "name": "aX",
                        "origin": "space_agent",
                        "agent_type": "space_agent",
                        "status": "active",
                        "control": {
                            "is_disabled": True,
                            "disabled_reason": "manual safety pause",
                        },
                    }
                ]
            }

    monkeypatch.setattr("ax_cli.commands.agents.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.agents.resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr("ax_cli.commands.agents.resolve_agent_name", lambda client=None: "ChatGPT")

    result = runner.invoke(app, ["agents", "discover", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    row = data["agents"][0]
    assert data["summary"]["blocked_by_control"] == 1
    assert row["roster_status"] == "active"
    assert row["control_status"] == "disabled"
    assert row["control_reason"] == "manual safety pause"
    assert row["listener_status"] == "disabled"
    assert row["contact_mode"] == "blocked_by_control"
    assert row["recommended_contact"] == "reenable_before_contact"
    assert row["warning"] == "agent_control_blocks_delivery"


def test_agents_discover_with_ping_classifies_listener(monkeypatch):
    calls = {"messages": []}

    class FakeClient:
        def list_agents(self, *, space_id=None, limit=None):
            return {
                "agents": [
                    {
                        "id": "agent-1",
                        "name": "backend_sentinel",
                        "origin": "mcp",
                        "agent_type": "mcp",
                        "status": "active",
                    }
                ]
            }

        def send_message(self, space_id, content):
            calls["messages"].append({"space_id": space_id, "content": content})
            return {"message": {"id": "msg-1"}}

    def fake_wait(client, **kwargs):
        calls["wait"] = kwargs
        return {"id": "reply-1", "content": kwargs["token"], "display_name": "backend_sentinel"}

    monkeypatch.setattr("ax_cli.commands.agents.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.agents.resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr("ax_cli.commands.agents.resolve_agent_name", lambda client=None: "ChatGPT")
    monkeypatch.setattr("ax_cli.commands.agents._wait_for_handoff_reply", fake_wait)

    result = runner.invoke(app, ["agents", "discover", "backend_sentinel", "--ping", "--timeout", "5", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["summary"]["event_listeners"] == 1
    row = data["agents"][0]
    assert row["mesh_role"] == "domain_sentinel"
    assert row["listener_status"] == "replied"
    assert row["contact_mode"] == "event_listener"
    assert row["recommended_contact"] == "handoff_or_send_wait"
    assert calls["messages"][0]["content"].startswith("@backend_sentinel Contact-mode ping")
