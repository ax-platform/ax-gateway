from typer.testing import CliRunner

from ax_cli.main import app

runner = CliRunner()


def test_tasks_create_assign_accepts_agent_handle(monkeypatch):
    calls = {}

    class FakeClient:
        def list_agents(self, *, space_id=None, limit=None):
            calls["list_agents"] = {"space_id": space_id, "limit": limit}
            return {
                "agents": [
                    {"id": "agent-123", "name": "orion"},
                    {"id": "agent-456", "name": "cipher"},
                ]
            }

        def create_task(self, space_id, title, *, description=None, priority="medium", assignee_id=None):
            calls["create_task"] = {
                "space_id": space_id,
                "title": title,
                "description": description,
                "priority": priority,
                "assignee_id": assignee_id,
            }
            return {"task": {"id": "task-1", "title": title, "assignee_id": assignee_id, "priority": priority}}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(
        app,
        ["tasks", "create", "Review the spec", "--assign", "@orion", "--no-notify", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls["list_agents"] == {"space_id": "space-1", "limit": 500}
    assert calls["create_task"]["assignee_id"] == "agent-123"


def test_tasks_create_assign_to_accepts_uuid_without_agent_lookup(monkeypatch):
    calls = {}
    agent_id = "076af365-dadc-4e92-a82d-79e855e5776e"

    class FakeClient:
        def list_agents(self, *, space_id=None, limit=None):
            calls["list_agents"] = {"space_id": space_id, "limit": limit}
            return {"agents": []}

        def create_task(self, space_id, title, *, description=None, priority="medium", assignee_id=None):
            calls["create_task"] = {"assignee_id": assignee_id}
            return {"task": {"id": "task-1", "title": title, "assignee_id": assignee_id}}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(
        app,
        ["tasks", "create", "Review the spec", "--assign-to", agent_id, "--no-notify", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert "list_agents" not in calls
    assert calls["create_task"]["assignee_id"] == agent_id


def test_tasks_create_assign_unknown_handle_fails(monkeypatch):
    class FakeClient:
        def list_agents(self, *, space_id=None, limit=None):
            return {"agents": [{"id": "agent-456", "name": "cipher"}]}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(
        app,
        ["tasks", "create", "Review the spec", "--assign", "orion", "--no-notify"],
    )

    assert result.exit_code == 1
    assert "No visible agent found" in result.output


def test_tasks_create_mention_prefixes_notification(monkeypatch):
    calls = {}

    class FakeClient:
        def create_task(self, space_id, title, *, description=None, priority="medium", assignee_id=None):
            return {"task": {"id": "task-1", "title": title, "priority": priority}}

        def send_message(self, space_id, content):
            calls["message"] = {"space_id": space_id, "content": content}
            return {"id": "msg-1"}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(
        app,
        ["tasks", "create", "Run smoke tests", "--mention", "cipher", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls["message"]["space_id"] == "space-1"
    assert calls["message"]["content"].startswith("@cipher New task created:")


def test_tasks_create_assign_handle_mentions_assignee_by_default(monkeypatch):
    calls = {}

    class FakeClient:
        def list_agents(self, *, space_id=None, limit=None):
            return {"agents": [{"id": "agent-123", "name": "orion"}]}

        def create_task(self, space_id, title, *, description=None, priority="medium", assignee_id=None):
            calls["create_task"] = {"assignee_id": assignee_id}
            return {"task": {"id": "task-1", "title": title, "priority": priority}}

        def send_message(self, space_id, content):
            calls["message"] = {"space_id": space_id, "content": content}
            return {"id": "msg-1"}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(
        app,
        ["tasks", "create", "Run smoke tests", "--assign", "orion", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls["create_task"]["assignee_id"] == "agent-123"
    assert calls["message"]["content"].startswith("@orion New task created:")
