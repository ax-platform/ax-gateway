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
            "orion",
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
    assert calls["message"]["content"] == "@orion context artifact ready"
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
                    "agent_name": "user:madtank",
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
    item = calls["metadata"]["ui"]["widget"]["initial_data"]["items"][0]
    assert item["value"]["type"] == "file_upload"
    assert item["value"]["filename"] == "channel-flow.svg"
    assert item["summary"] == "SVG upload"
    assert item["ttl"] == 86400
