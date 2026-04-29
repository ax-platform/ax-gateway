import json
import tomllib

from typer.testing import CliRunner

from ax_cli.commands import auth
from ax_cli.main import app

runner = CliRunner()


def test_login_calls_user_login(monkeypatch):
    """`ax login` is the human login path, separate from local agent init."""
    called = {}

    def fake_login_user(token, *, base_url, agent, space_id, env_name):
        called.update(
            {
                "token": token,
                "base_url": base_url,
                "agent": agent,
                "space_id": space_id,
                "env_name": env_name,
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
            "https://paxai.app",
            "--env",
            "next",
            "--agent",
            "anvil",
            "--space-id",
            "space-123",
        ],
    )

    assert result.exit_code == 0
    assert called == {
        "token": "axp_u_test.token",
        "base_url": "https://paxai.app",
        "agent": "anvil",
        "space_id": "space-123",
        "env_name": "next",
    }


def test_login_defaults_to_next_without_space_requirement(monkeypatch):
    """`ax login` is the user path: next URL by default, no space required."""
    called = {}

    def fake_login_user(token, *, base_url, agent, space_id, env_name):
        called.update(
            {
                "token": token,
                "base_url": base_url,
                "agent": agent,
                "space_id": space_id,
                "env_name": env_name,
            }
        )

    monkeypatch.setattr(auth, "login_user", fake_login_user)

    result = runner.invoke(app, ["login", "--token", "axp_u_test.token"])

    assert result.exit_code == 0
    assert called == {
        "token": "axp_u_test.token",
        "base_url": "https://paxai.app",
        "agent": None,
        "space_id": None,
        "env_name": None,
    }


def test_login_token_prompt_is_masked(monkeypatch):
    """Omitting --token prompts via Typer's hidden input path."""
    prompt_calls = []
    printed = []

    def fake_prompt(label, *, hide_input):
        prompt_calls.append({"label": label, "hide_input": hide_input})
        return " axp_u_prompt.token "

    monkeypatch.setattr(auth.typer, "prompt", fake_prompt)
    monkeypatch.setattr(auth.console, "print", lambda *args, **kwargs: printed.append(str(args[0]) if args else ""))

    assert auth._resolve_login_token(None) == "axp_u_prompt.token"
    assert prompt_calls == [{"label": "Token", "hide_input": True}]
    assert any("Token captured" in line for line in printed)
    assert "axp_u_prompt.token" not in "\n".join(printed)
    assert "axp_u_********" in "\n".join(printed)


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
            assert self.base_url == "https://paxai.app"
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
        "base_url": "https://paxai.app",
        "principal_type": "user",
        "space_id": "space-current",
    }


def test_user_login_env_stores_named_login_and_marks_active(monkeypatch, write_config, config_dir):
    """Admins can keep separate user bootstrap tokens for dev/next/prod."""
    write_config(token="axp_a_old.secret", base_url="https://old.example.com", agent_name="orion")

    class FakeTokenExchanger:
        def __init__(self, base_url, token):
            self.base_url = base_url
            self.token = token

        def get_token(self, token_class, *, scope, force_refresh):
            assert self.base_url == "https://dev.paxai.app"
            assert self.token == "axp_u_dev.secret"
            return "fake.jwt"

    class FakeAxClient:
        def __init__(self, *, base_url, token):
            self.base_url = base_url
            self.token = token

        def whoami(self):
            return {"username": "madtank", "email": "madtank@example.com"}

        def list_spaces(self):
            return {"spaces": []}

    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", FakeTokenExchanger)
    monkeypatch.setattr("ax_cli.client.AxClient", FakeAxClient)

    result = runner.invoke(app, ["login", "--token", "axp_u_dev.secret", "--url", "https://dev.paxai.app", "--env", "dev"])

    assert result.exit_code == 0
    global_dir = config_dir.parent / "_global_config"
    default_user = global_dir / "user.toml"
    dev_user = global_dir / "users" / "dev" / "user.toml"
    assert not default_user.exists()
    assert tomllib.loads(dev_user.read_text()) == {
        "token": "axp_u_dev.secret",
        "base_url": "https://dev.paxai.app",
        "principal_type": "user",
        "environment": "dev",
    }
    assert (global_dir / "users" / ".active").read_text().strip() == "dev"


def test_auth_doctor_json_outputs_diagnostics(monkeypatch):
    monkeypatch.setattr(
        auth,
        "diagnose_auth_config",
        lambda *, env_name, explicit_space_id: {
            "ok": True,
            "selected_env": env_name,
            "selected_profile": None,
            "runtime_config": "/tmp/codex/.ax/config.toml",
            "effective": {
                "auth_source": "user_login:dev",
                "token_kind": "user_pat",
                "token": "axp_u_...cret",
                "base_url": "https://dev.paxai.app",
                "base_url_source": "user_login:dev",
                "host": "dev.paxai.app",
                "space_id": explicit_space_id,
                "space_source": "option:--space-id",
                "principal_intent": "user",
            },
            "sources": [],
            "warnings": [],
            "problems": [],
        },
    )

    result = runner.invoke(app, ["auth", "doctor", "--env", "dev", "--space-id", "space-1", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["version"] == 1
    assert payload["skipped"] is False
    assert payload["summary"] == {
        "command": "ax auth doctor",
        "principal_intent": "user",
        "auth_source": "user_login:dev",
        "host": "dev.paxai.app",
        "space_id": "space-1",
        "warnings": 0,
        "problems": 0,
    }
    assert payload["details"] == []
    assert payload["effective"]["auth_source"] == "user_login:dev"
    assert payload["effective"]["space_id"] == "space-1"


def test_auth_whoami_reports_runtime_config(monkeypatch, tmp_path):
    runtime_config = tmp_path / "runtime-config.toml"
    runtime_config.write_text("")
    monkeypatch.setenv("AX_CONFIG_FILE", str(runtime_config))

    class FakeClient:
        def whoami(self):
            return {
                "id": "user-1",
                "bound_agent": {
                    "default_space_id": "space-1",
                },
            }

    monkeypatch.setattr(auth, "get_client", lambda: FakeClient())
    monkeypatch.setattr(auth, "resolve_agent_name", lambda *, client: "codex")
    monkeypatch.setattr(auth, "_local_config_dir", lambda: None)

    result = runner.invoke(app, ["auth", "whoami", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["runtime_config"] == str(runtime_config)
    assert payload["resolved_agent"] == "codex"


def test_auth_exchange_without_token_points_agents_to_gateway(monkeypatch, config_dir):
    monkeypatch.delenv("AX_TOKEN", raising=False)
    monkeypatch.delenv("AX_TOKEN_FILE", raising=False)

    result = runner.invoke(app, ["auth", "exchange"])

    assert result.exit_code == 1
    assert "No token configured" in result.output
    assert "ax gateway local" in result.output
    assert "ax gateway login" in result.output
    assert "auth token set" not in result.output
    assert "AX_TOKEN" not in result.output


def test_auth_token_show_without_token_points_agents_to_gateway(monkeypatch, config_dir):
    monkeypatch.delenv("AX_TOKEN", raising=False)
    monkeypatch.delenv("AX_TOKEN_FILE", raising=False)

    result = runner.invoke(app, ["auth", "token", "show"])

    assert result.exit_code == 1
    assert "Gateway-managed agents" in result.output
    assert "ax gateway local" in result.output
    assert "ax gateway login" in result.output
    assert "AX_TOKEN" not in result.output
