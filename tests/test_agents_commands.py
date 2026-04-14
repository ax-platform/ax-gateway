import json

from typer.testing import CliRunner

from ax_cli.main import app

runner = CliRunner()


def test_agents_ping_classifies_reply_as_event_listener(monkeypatch):
    calls = {}

    class FakeClient:
        def list_agents(self, *, space_id=None, limit=None):
            calls["list_agents"] = {"space_id": space_id, "limit": limit}
            return {
                "agents": [
                    {
                        "id": "agent-1",
                        "name": "orion",
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
        return {"id": "reply-1", "content": f"received {kwargs['token']}", "display_name": "orion"}

    monkeypatch.setattr("ax_cli.commands.agents.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.agents.resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr("ax_cli.commands.agents.resolve_agent_name", lambda client=None: "ChatGPT")
    monkeypatch.setattr("ax_cli.commands.agents._wait_for_handoff_reply", fake_wait)

    result = runner.invoke(app, ["agents", "ping", "orion", "--timeout", "5", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["contact_mode"] == "event_listener"
    assert data["listener_status"] == "replied"
    assert data["agent_id"] == "agent-1"
    assert calls["message"]["content"].startswith("@orion Contact-mode ping")
    assert calls["wait"]["agent_name"] == "orion"
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
            return {"agents": [{"id": "agent-1", "name": "orion"}]}

    monkeypatch.setattr("ax_cli.commands.agents.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.agents.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(app, ["agents", "ping", "missing"])

    assert result.exit_code == 1
    assert "No visible agent found" in result.output
