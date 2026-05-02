import json

from typer.testing import CliRunner

from ax_cli.main import app

runner = CliRunner()


def test_apps_list_includes_context_surface():
    result = runner.invoke(app, ["apps", "list", "--json"])

    assert result.exit_code == 0, result.output
    apps = {item["app"]: item for item in json.loads(result.output)}
    assert apps["context"]["resource_uri"] == "ui://context/explorer"


def test_apps_signal_writes_context_widget_metadata(monkeypatch):
    calls = {}

    class FakeClient:
        def get_context(self, key, *, space_id=None):
            calls["get_context"] = {"key": key, "space_id": space_id}
            return {
                "key": key,
                "value": json.dumps(
                    {
                        "type": "file_upload",
                        "summary": "Architecture diagram",
                        "content": "# Architecture\n",
                        "file_upload": {"filename": "architecture.md"},
                    }
                ),
            }

        def send_message(
            self,
            space_id,
            content,
            *,
            channel="main",
            parent_id=None,
            attachments=None,
            metadata=None,
            message_type="text",
        ):
            calls["message"] = {
                "space_id": space_id,
                "content": content,
                "channel": channel,
                "parent_id": parent_id,
                "attachments": attachments,
                "metadata": metadata,
                "message_type": message_type,
            }
            return {"id": "msg-1"}

    monkeypatch.setattr("ax_cli.commands.apps.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.apps.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(
        app,
        [
            "apps",
            "signal",
            "context",
            "--context-key",
            "design:architecture",
            "--title",
            "Architecture",
            "--summary",
            "Review this diagram",
            "--message",
            "context artifact ready",
            "--to",
            "demo-agent",
            "--channel",
            "automation-alerts",
            "--alert-kind",
            "design_review",
            "--message-type",
            "system",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["get_context"] == {"key": "design:architecture", "space_id": "space-1"}
    assert calls["message"]["content"] == "@demo-agent context artifact ready"
    assert calls["message"]["channel"] == "automation-alerts"
    assert calls["message"]["message_type"] == "system"

    metadata = calls["message"]["metadata"]
    widget = metadata["ui"]["widget"]
    assert widget["tool_name"] == "context"
    assert widget["tool_action"] == "get"
    assert widget["resource_uri"] == "ui://context/explorer"
    assert widget["arguments"]["key"] == "design:architecture"
    assert widget["initial_data"]["selected_key"] == "design:architecture"
    assert widget["initial_data"]["items"][0]["file_content"] == "# Architecture\n"
    assert metadata["alert"]["kind"] == "design_review"
    assert metadata["alert"]["target_agent"] == "demo-agent"
    assert metadata["alert"]["response_required"] is True
    assert metadata["alert"]["summary"] == "Review this diagram"
    assert "top_level_ingress" not in metadata
    assert "signal_only" not in metadata

    output = json.loads(result.output)
    assert output["message"]["id"] == "msg-1"
    assert output["resource_uri"] == "ui://context/explorer"


def test_apps_signal_flattens_wrapped_context_payload(monkeypatch):
    calls = {}

    class FakeClient:
        def get_context(self, key, *, space_id=None):
            return {
                "key": key,
                "value": {
                    "value": json.dumps(
                        {
                            "type": "file_upload",
                            "filename": "channel-flow.svg",
                            "content_type": "image/svg+xml",
                            "url": "https://dev.paxai.app/api/v1/uploads/files/channel-flow.svg",
                        }
                    ),
                    "agent_name": "user:alex",
                    "summary": "SVG upload",
                    "ttl": 86400,
                },
                "source": "redis",
            }

        def send_message(
            self,
            space_id,
            content,
            *,
            channel="main",
            parent_id=None,
            attachments=None,
            metadata=None,
            message_type="text",
        ):
            calls["metadata"] = metadata
            calls["message_type"] = message_type
            calls["content"] = content
            return {"id": "msg-2"}

    monkeypatch.setattr("ax_cli.commands.apps.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.apps.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(
        app,
        [
            "apps",
            "signal",
            "context",
            "--context-key",
            "upload:channel-flow.svg",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["message_type"] == "system"
    assert calls["content"] == "Context Explorer: Context key `upload:channel-flow.svg`"
    item = calls["metadata"]["ui"]["widget"]["initial_data"]["items"][0]
    assert item["value"]["type"] == "file_upload"
    assert item["value"]["filename"] == "channel-flow.svg"
    assert item["summary"] == "SVG upload"
    assert item["ttl"] == 86400


def test_apps_signal_whoami_builds_identity_widget_payload(monkeypatch):
    calls = {}

    class FakeClient:
        def whoami(self):
            calls["whoami"] = True
            return {
                "id": "user-1",
                "email": "alex@example.com",
                "full_name": "Alex Example",
                "username": "alex",
                "role": "admin",
                "bound_agent": {
                    "agent_id": "agent-1",
                    "agent_name": "demo_agent",
                    "default_space_id": "space-1",
                    "default_space_name": "Alex's Workspace",
                    "allowed_spaces": [
                        {"space_id": "space-1", "name": "alex's Workspace", "is_default": True},
                    ],
                },
                "resolved_space_id": "space-1",
                "resolved_agent": "demo_agent",
            }

        def send_message(
            self,
            space_id,
            content,
            *,
            channel="main",
            parent_id=None,
            attachments=None,
            metadata=None,
            message_type="text",
        ):
            calls["message"] = {
                "space_id": space_id,
                "content": content,
                "channel": channel,
                "parent_id": parent_id,
                "attachments": attachments,
                "metadata": metadata,
                "message_type": message_type,
            }
            return {"id": "msg-identity"}

    monkeypatch.setattr("ax_cli.commands.apps.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.apps.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(
        app,
        [
            "apps",
            "signal",
            "whoami",
            "--summary",
            "CLI identity smoke",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["whoami"] is True
    widget = calls["message"]["metadata"]["ui"]["widget"]
    assert widget["tool_name"] == "whoami"
    assert widget["resource_uri"] == "ui://whoami/identity"
    initial_data = widget["initial_data"]
    assert initial_data["kind"] == "whoami_profile"
    assert initial_data["state"] == "ready"
    assert initial_data["data"]["identity"] == {
        "principal_kind": "agent",
        "role_label": "Agent",
        "status": "active",
        "handle": "demo_agent",
        "display_name": "demo_agent",
        "id": "agent-1",
    }
    assert initial_data["data"]["context"]["workspace_name"] == "alex's Workspace"
    assert initial_data["data"]["context"]["owner"]["handle"] == "alex"
    assert calls["message"]["metadata"]["top_level_ingress"] is False
    assert calls["message"]["metadata"]["signal_only"] is True
    assert calls["message"]["metadata"]["app_signal"]["signal_only"] is True


def test_apps_signal_agents_hydrates_dashboard_payload(monkeypatch):
    calls = {}

    class FakeClient:
        def list_agents(self, *, space_id=None, limit=None):
            calls["list_agents"] = {"space_id": space_id, "limit": limit}
            return {
                "agents": [
                    {
                        "id": "agent-1",
                        "name": "demo-agent",
                        "status": "active",
                        "description": "QA reviewer",
                    },
                ],
                "count": 1,
            }

        def send_message(
            self,
            space_id,
            content,
            *,
            channel="main",
            parent_id=None,
            attachments=None,
            metadata=None,
            message_type="text",
        ):
            calls["message"] = {
                "space_id": space_id,
                "content": content,
                "channel": channel,
                "parent_id": parent_id,
                "attachments": attachments,
                "metadata": metadata,
                "message_type": message_type,
            }
            return {"id": "msg-agents"}

    monkeypatch.setattr("ax_cli.commands.apps.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.apps.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(app, ["apps", "signal", "agents", "--summary", "Available agents", "--json"])

    assert result.exit_code == 0, result.output
    assert calls["list_agents"] == {"space_id": "space-1", "limit": 500}
    widget = calls["message"]["metadata"]["ui"]["widget"]
    assert widget["tool_name"] == "agents"
    assert widget["resource_uri"] == "ui://agents/dashboard"
    initial_data = widget["initial_data"]
    assert initial_data["kind"] == "agents"
    assert initial_data["items"] == [
        {
            "id": "agent-1",
            "name": "demo-agent",
            "status": "active",
            "description": "QA reviewer",
        },
    ]
    assert initial_data["keys"] == ["agent-1"]
    assert initial_data["count"] == 1


def test_apps_signal_spaces_hydrates_navigator_payload(monkeypatch):
    calls = {}

    class FakeClient:
        def list_spaces(self):
            calls["list_spaces"] = True
            return [
                {
                    "id": "space-1",
                    "name": "Development",
                    "visibility": "private",
                    "member_count": 4,
                }
            ]

        def send_message(
            self,
            space_id,
            content,
            *,
            channel="main",
            parent_id=None,
            attachments=None,
            metadata=None,
            message_type="text",
        ):
            calls["message"] = {
                "space_id": space_id,
                "content": content,
                "channel": channel,
                "parent_id": parent_id,
                "attachments": attachments,
                "metadata": metadata,
                "message_type": message_type,
            }
            return {"id": "msg-spaces"}

    monkeypatch.setattr("ax_cli.commands.apps.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.apps.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(app, ["apps", "signal", "spaces", "--summary", "Spaces ready", "--json"])

    assert result.exit_code == 0, result.output
    assert calls["list_spaces"] is True
    initial_data = calls["message"]["metadata"]["ui"]["widget"]["initial_data"]
    assert initial_data["kind"] == "spaces"
    assert initial_data["items"][0]["name"] == "Development"
    assert initial_data["keys"] == ["space-1"]
    assert initial_data["count"] == 1


def test_apps_signal_tasks_hydrates_board_payload(monkeypatch):
    calls = {}

    class FakeClient:
        def list_tasks(self, limit=20, *, agent_id=None, space_id=None):
            calls["list_tasks"] = {"limit": limit, "agent_id": agent_id, "space_id": space_id}
            return {
                "tasks": [
                    {
                        "id": "task-1",
                        "title": "Review alert card",
                        "status": "open",
                        "priority": "high",
                    }
                ],
                "total": 1,
            }

        def send_message(
            self,
            space_id,
            content,
            *,
            channel="main",
            parent_id=None,
            attachments=None,
            metadata=None,
            message_type="text",
        ):
            calls["message"] = {
                "space_id": space_id,
                "content": content,
                "channel": channel,
                "parent_id": parent_id,
                "attachments": attachments,
                "metadata": metadata,
                "message_type": message_type,
            }
            return {"id": "msg-tasks"}

    monkeypatch.setattr("ax_cli.commands.apps.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.apps.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(app, ["apps", "signal", "tasks", "--summary", "Task board ready", "--json"])

    assert result.exit_code == 0, result.output
    assert calls["list_tasks"] == {"limit": 50, "agent_id": None, "space_id": "space-1"}
    initial_data = calls["message"]["metadata"]["ui"]["widget"]["initial_data"]
    assert initial_data["kind"] == "tasks"
    assert initial_data["items"][0]["title"] == "Review alert card"
    assert initial_data["keys"] == ["task-1"]
    assert initial_data["count"] == 1
