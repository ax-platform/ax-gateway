import json

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
                    {"id": "agent-123", "name": "demo-agent"},
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
        ["tasks", "create", "Review the spec", "--assign", "@demo-agent", "--no-notify", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls["list_agents"] == {"space_id": "space-1", "limit": 500}
    assert calls["create_task"]["assignee_id"] == "agent-123"


def test_tasks_create_accepts_space_slug(monkeypatch):
    calls = {}

    class FakeClient:
        def list_spaces(self):
            return {
                "spaces": [
                    {"id": "private-space", "slug": "madtank-workspace", "name": "madtank's Workspace"},
                    {"id": "team-space", "slug": "ax-cli-dev", "name": "ax-cli-dev"},
                ]
            }

        def create_task(self, space_id, title, *, description=None, priority="medium", assignee_id=None):
            calls["create_task"] = {"space_id": space_id, "title": title}
            return {"task": {"id": "task-1", "title": title, "priority": priority}}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())

    result = runner.invoke(
        app,
        ["tasks", "create", "Fix routing", "--space", "ax-cli-dev", "--no-notify", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls["create_task"]["space_id"] == "team-space"
    payload = json.loads(result.output)
    assert payload["space_id"] == "team-space"
    assert payload["space_slug"] == "ax-cli-dev"


def test_tasks_create_uses_gateway_local_identity(monkeypatch):
    calls = {}

    monkeypatch.setattr(
        "ax_cli.commands.tasks.resolve_gateway_config",
        lambda: {
            "url": "http://127.0.0.1:8765",
            "agent_name": "codex-pass-through",
            "registry_ref": None,
            "workdir": "/repo",
            "space_id": "space-from-config",
        },
    )
    monkeypatch.setattr(
        "ax_cli.commands.tasks._gateway_local_connect",
        lambda **kwargs: {
            "status": "approved",
            "session_token": "session-123",
            "registry_ref": "#5",
            "agent": {"name": "codex-pass-through"},
        },
    )

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"task": {"id": "task-1", "title": "Lock specs", "priority": "high"}}

    def fake_post(url, *, json=None, headers=None, timeout=None):
        calls["post"] = {"url": url, "json": json, "headers": headers, "timeout": timeout}
        return FakeResponse()

    monkeypatch.setattr("ax_cli.commands.tasks.httpx.post", fake_post)

    result = runner.invoke(
        app,
        [
            "tasks",
            "create",
            "Lock specs",
            "--description",
            "Make Gateway boring.",
            "--priority",
            "high",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["post"]["url"] == "http://127.0.0.1:8765/local/tasks"
    assert calls["post"]["headers"]["X-Gateway-Session"] == "session-123"
    assert calls["post"]["json"] == {
        "title": "Lock specs",
        "description": "Make Gateway boring.",
        "priority": "high",
        "space_id": "space-from-config",
    }
    payload = json.loads(result.output)
    assert payload["task"]["id"] == "task-1"


def test_tasks_create_human_output_includes_resolved_space(monkeypatch):
    class FakeClient:
        def list_spaces(self):
            return {"spaces": [{"id": "team-space", "slug": "ax-cli-dev", "name": "ax-cli-dev"}]}

        def create_task(self, space_id, title, *, description=None, priority="medium", assignee_id=None):
            return {"task": {"id": "task-1", "title": title, "priority": priority}}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())

    result = runner.invoke(
        app,
        ["tasks", "create", "Fix routing", "--space", "ax-cli-dev", "--no-notify"],
    )

    assert result.exit_code == 0, result.output
    assert "in ax-cli-dev (team-space)" in result.output


def test_tasks_create_assign_to_accepts_uuid_without_agent_lookup(monkeypatch):
    calls = {}
    agent_id = "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"

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
        ["tasks", "create", "Review the spec", "--assign", "demo-agent", "--no-notify"],
    )

    assert result.exit_code == 1
    assert "No visible agent found" in result.output


def test_tasks_create_mention_prefixes_notification(monkeypatch):
    calls = {}

    class FakeClient:
        def create_task(self, space_id, title, *, description=None, priority="medium", assignee_id=None):
            return {"task": {"id": "task-1", "title": title, "priority": priority}}

        def send_message(self, space_id, content, *, metadata=None, message_type="text"):
            calls["message"] = {
                "space_id": space_id,
                "content": content,
                "metadata": metadata,
                "message_type": message_type,
            }
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
    assert calls["message"]["message_type"] == "system"
    metadata = calls["message"]["metadata"]
    assert metadata["ui"]["cards"][0]["type"] == "task"
    assert metadata["ui"]["cards"][0]["payload"]["source"] == "axctl_tasks_create"
    assert metadata["ui"]["widget"]["resource_uri"] == "ui://tasks/detail"
    assert metadata["ui"]["widget"]["initial_data"]["items"][0]["title"] == "Run smoke tests"


def test_tasks_create_assign_handle_mentions_assignee_by_default(monkeypatch):
    calls = {}

    class FakeClient:
        def list_agents(self, *, space_id=None, limit=None):
            return {"agents": [{"id": "agent-123", "name": "demo-agent"}]}

        def create_task(self, space_id, title, *, description=None, priority="medium", assignee_id=None):
            calls["create_task"] = {"assignee_id": assignee_id}
            return {"task": {"id": "task-1", "title": title, "priority": priority}}

        def send_message(self, space_id, content, *, metadata=None, message_type="text"):
            calls["message"] = {
                "space_id": space_id,
                "content": content,
                "metadata": metadata,
                "message_type": message_type,
            }
            return {"id": "msg-1"}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(
        app,
        ["tasks", "create", "Run smoke tests", "--assign", "demo-agent", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls["create_task"]["assignee_id"] == "agent-123"
    assert calls["message"]["content"].startswith("@demo-agent New task created:")
    assert calls["message"]["metadata"]["ui"]["cards"][0]["payload"]["assignee"] == {
        "id": "agent-123",
        "name": "demo-agent",
    }
