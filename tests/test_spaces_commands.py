import json

from typer.testing import CliRunner

from ax_cli.main import app

runner = CliRunner()


def test_spaces_use_accepts_slug_and_warns_when_bound_agent_not_attached(monkeypatch):
    saved = {}

    class FakeClient:
        def list_spaces(self):
            return {
                "spaces": [
                    {"id": "private-space", "slug": "madtank-workspace", "name": "madtank's Workspace"},
                    {"id": "team-space", "slug": "ax-cli-dev", "name": "aX CLI Dev"},
                ]
            }

        def whoami(self):
            return {
                "bound_agent": {
                    "agent_name": "orion",
                    "allowed_spaces": [{"space_id": "private-space", "name": "madtank's Workspace"}],
                }
            }

    def fake_save_space_id(space_id, *, local=True):
        saved["space_id"] = space_id
        saved["local"] = local

    monkeypatch.setattr("ax_cli.commands.spaces.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.spaces.save_space_id", fake_save_space_id)

    result = runner.invoke(app, ["spaces", "use", "ax-cli-dev", "--json"])

    assert result.exit_code == 0, result.output
    assert saved == {"space_id": "team-space", "local": True}
    payload = json.loads(result.output)
    assert payload["space_id"] == "team-space"
    assert payload["space_label"] == "ax-cli-dev"
    assert payload["scope"] == "local"
    assert payload["bound_agent"] == "orion"
    assert payload["bound_agent_allowed"] is False


def test_spaces_use_global_saves_global_config(monkeypatch):
    saved = {}

    class FakeClient:
        def list_spaces(self):
            return {"spaces": [{"id": "team-space", "slug": "ax-cli-dev", "name": "aX CLI Dev"}]}

        def whoami(self):
            return {}

    def fake_save_space_id(space_id, *, local=True):
        saved["space_id"] = space_id
        saved["local"] = local

    monkeypatch.setattr("ax_cli.commands.spaces.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.spaces.save_space_id", fake_save_space_id)

    result = runner.invoke(app, ["spaces", "use", "ax-cli-dev", "--global", "--json"])

    assert result.exit_code == 0, result.output
    assert saved == {"space_id": "team-space", "local": False}
    assert json.loads(result.output)["scope"] == "global"
