import tomllib

from typer.testing import CliRunner

from ax_cli.commands import auth
from ax_cli.main import app

runner = CliRunner()


def test_login_calls_user_login(monkeypatch):
    """`ax login` is the human login path, separate from local agent init."""
    called = {}

    def fake_login_user(token, *, base_url, agent, space_id):
        called.update(
            {
                "token": token,
                "base_url": base_url,
                "agent": agent,
                "space_id": space_id,
            }
        )

    monkeypatch.setattr(auth, "login_user", fake_login_user)

    result = runner.invoke(
        app,
        [
            "login",
            "--token",
            "axp_u_test.token",
            "--url",
            "https://next.paxai.app",
            "--agent",
            "anvil",
            "--space-id",
            "space-123",
        ],
    )

    assert result.exit_code == 0
    assert called == {
        "token": "axp_u_test.token",
        "base_url": "https://next.paxai.app",
        "agent": "anvil",
        "space_id": "space-123",
    }


def test_login_defaults_to_next_without_space_requirement(monkeypatch):
    """`ax login` is the user path: next URL by default, no space required."""
    called = {}

    def fake_login_user(token, *, base_url, agent, space_id):
        called.update(
            {
                "token": token,
                "base_url": base_url,
                "agent": agent,
                "space_id": space_id,
            }
        )

    monkeypatch.setattr(auth, "login_user", fake_login_user)

    result = runner.invoke(app, ["login", "--token", "axp_u_test.token"])

    assert result.exit_code == 0
    assert called == {
        "token": "axp_u_test.token",
        "base_url": "https://next.paxai.app",
        "agent": None,
        "space_id": None,
    }


def test_login_token_prompt_is_masked(monkeypatch):
    """Omitting --token prompts via Typer's hidden input path."""
    prompt_calls = []

    def fake_prompt(label, *, hide_input):
        prompt_calls.append({"label": label, "hide_input": hide_input})
        return " axp_u_prompt.token "

    monkeypatch.setattr(auth.typer, "prompt", fake_prompt)

    assert auth._resolve_login_token(None) == "axp_u_prompt.token"
    assert prompt_calls == [{"label": "Token", "hide_input": True}]


def test_login_space_selection_uses_only_unambiguous_space():
    assert auth._select_login_space([{"id": "space-1", "name": "Only"}]) == {"id": "space-1", "name": "Only"}
    assert auth._select_login_space(
        [
            {"id": "space-1", "name": "Team"},
            {"id": "space-2", "name": "Personal", "is_personal": True},
        ]
    ) == {"id": "space-2", "name": "Personal", "is_personal": True}
    assert (
        auth._select_login_space(
            [
                {"id": "space-1", "name": "Team A"},
                {"id": "space-2", "name": "Team B"},
            ]
        )
        is None
    )


def test_user_login_does_not_modify_local_agent_config(monkeypatch, write_config, config_dir):
    """A user PAT login is stored separately and must not rewrite an agent config."""
    write_config(
        token="axp_a_old.secret",
        base_url="https://old.example.com",
        agent_name="orion",
        agent_id="agent-orion",
        space_id="old-space",
    )

    class FakeTokenExchanger:
        def __init__(self, base_url, token):
            self.base_url = base_url
            self.token = token

        def get_token(self, token_class, *, scope, force_refresh):
            assert self.base_url == "https://next.paxai.app"
            assert self.token == "axp_u_new.secret"
            assert token_class == "user_access"
            assert scope == "messages tasks context agents spaces search"
            assert force_refresh is True
            return "fake.jwt"

    class FakeAxClient:
        def __init__(self, *, base_url, token):
            self.base_url = base_url
            self.token = token

        def whoami(self):
            return {"username": "madtank", "email": "madtank@example.com"}

        def list_spaces(self):
            return {"spaces": [{"id": "space-current", "name": "Team Hub", "is_current": True}]}

        def list_agents(self):
            raise AssertionError("user login must not auto-select or store an agent")

    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", FakeTokenExchanger)
    monkeypatch.setattr("ax_cli.client.AxClient", FakeAxClient)

    result = runner.invoke(app, ["login", "--token", "axp_u_new.secret"])

    assert result.exit_code == 0
    local_cfg = tomllib.loads((config_dir / "config.toml").read_text())
    assert local_cfg == {
        "token": "axp_a_old.secret",
        "base_url": "https://old.example.com",
        "agent_name": "orion",
        "agent_id": "agent-orion",
        "space_id": "old-space",
    }
    user_cfg = tomllib.loads((config_dir.parent / "_global_config" / "user.toml").read_text())
    assert user_cfg == {
        "token": "axp_u_new.secret",
        "base_url": "https://next.paxai.app",
        "principal_type": "user",
        "space_id": "space-current",
    }
