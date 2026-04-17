"""Tests for config resolution — the cascade that burned us (2026-04-05)."""

from pathlib import Path

from ax_cli import config as config_module
from ax_cli.config import (
    _find_project_root,
    _global_config_dir,
    _load_config,
    _save_user_config,
    diagnose_auth_config,
    resolve_agent_id,
    resolve_agent_name,
    resolve_base_url,
    resolve_token,
    resolve_user_base_url,
    resolve_user_token,
)


def _write_active_profile(global_dir: Path, *, name: str = "next-orion") -> Path:
    token_file = global_dir / "profiles" / name / "token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("axp_a_agent.secret")
    (global_dir / "profiles" / ".active").write_text(f"{name}\n")
    (global_dir / "profiles" / name / "profile.toml").write_text(
        f'base_url = "https://next.paxai.app"\n'
        f'agent_name = "orion"\n'
        f'agent_id = "agent-orion"\n'
        f'space_id = "next-space"\n'
        f'token_file = "{token_file}"\n'
    )
    return token_file


class TestFindProjectRoot:
    def test_finds_ax_dir(self, tmp_path, monkeypatch):
        (tmp_path / ".ax").mkdir()
        monkeypatch.chdir(tmp_path)
        assert _find_project_root() == tmp_path

    def test_ignores_git_dir(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        result = _find_project_root()
        assert result != tmp_path
        if result is not None:
            assert (result / ".ax").is_dir()

    def test_finds_ax_even_when_git_exists(self, tmp_path, monkeypatch):
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        assert _find_project_root() == tmp_path

    def test_walks_up(self, tmp_path, monkeypatch):
        (tmp_path / ".ax").mkdir()
        child = tmp_path / "sub" / "deep"
        child.mkdir(parents=True)
        monkeypatch.chdir(child)
        assert _find_project_root() == tmp_path

    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        # tmp_path has no .ax or .git
        isolated = tmp_path / "isolated"
        isolated.mkdir()
        monkeypatch.chdir(isolated)
        # May find something up the tree depending on environment,
        # but in an isolated tmp_path it should be None
        result = _find_project_root()
        # If no .ax anywhere up the tree
        if result is not None:
            assert (result / ".ax").is_dir()


class TestGlobalConfigDir:
    def test_default_is_home_ax(self, monkeypatch):
        monkeypatch.delenv("AX_CONFIG_DIR", raising=False)
        assert _global_config_dir() == Path.home() / ".ax"

    def test_respects_env_override(self, monkeypatch, tmp_path):
        custom = tmp_path / "custom-config"
        custom.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(custom))
        assert _global_config_dir() == custom


class TestLoadConfig:
    def test_empty_when_no_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "nonexistent"))
        assert _load_config() == {}

    def test_loads_global_config(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        (global_dir / "config.toml").write_text('base_url = "https://example.com"\n')
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        cfg = _load_config()
        assert cfg["base_url"] == "https://example.com"

    def test_local_overrides_global(self, tmp_path, monkeypatch):
        # Global config
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        (global_dir / "config.toml").write_text('agent_id = "global-agent"\nbase_url = "https://global.example.com"\n')
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))

        # Local config (in CWD)
        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text('agent_id = "local-agent"\n')
        monkeypatch.chdir(tmp_path)

        cfg = _load_config()
        assert cfg["agent_id"] == "local-agent"  # local wins
        assert cfg["base_url"] == "https://global.example.com"  # global preserved

    def test_ax_config_file_overrides_local_runtime_config(self, tmp_path, monkeypatch):
        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text(
            'token = "axp_a_local.secret"\n'
            'base_url = "https://local.example.com"\n'
            'agent_name = "local-agent"\n'
            'agent_id = "agent-local"\n'
        )
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        token_file = runtime_dir / "agent.pat"
        token_file.write_text("axp_a_runtime.secret")
        runtime_config = runtime_dir / "config.toml"
        runtime_config.write_text(
            f'token_file = "{token_file.name}"\n'
            'base_url = "https://next.paxai.app"\n'
            'agent_name = "orion"\n'
            'agent_id = "agent-orion"\n'
            'space_id = "space-next"\n'
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AX_CONFIG_FILE", str(runtime_config))

        cfg = _load_config()

        assert cfg["token"] == "axp_a_runtime.secret"
        assert cfg["base_url"] == "https://next.paxai.app"
        assert cfg["agent_name"] == "orion"
        assert cfg["agent_id"] == "agent-orion"
        assert cfg["space_id"] == "space-next"
        assert cfg["principal_type"] == "agent"

    def test_user_login_config_is_fallback_without_local_config(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        _save_user_config(
            {
                "token": "axp_u_user.secret",
                "base_url": "https://next.paxai.app",
                "principal_type": "user",
            }
        )
        isolated = tmp_path / "no-local"
        isolated.mkdir()
        monkeypatch.chdir(isolated)

        cfg = _load_config()

        assert cfg["token"] == "axp_u_user.secret"
        assert cfg["principal_type"] == "user"

    def test_local_agent_config_overrides_user_login_principal(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        _save_user_config(
            {
                "token": "axp_u_user.secret",
                "base_url": "https://next.paxai.app",
                "principal_type": "user",
            }
        )
        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text(
            'token = "axp_a_agent.secret"\n'
            'base_url = "https://next.paxai.app"\n'
            'agent_name = "orion"\n'
            'agent_id = "agent-orion"\n'
        )
        monkeypatch.chdir(tmp_path)

        cfg = _load_config()

        assert cfg["token"] == "axp_a_agent.secret"
        assert cfg["principal_type"] == "agent"
        assert cfg["agent_name"] == "orion"

    def test_unsafe_local_user_pat_agent_config_does_not_override_active_profile(
        self, tmp_path, monkeypatch, capsys
    ):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        monkeypatch.setattr(config_module, "_unsafe_local_config_warned", False)

        token_file = global_dir / "profiles" / "next-orion" / "token"
        token_file.parent.mkdir(parents=True)
        token_file.write_text("axp_a_agent.secret")
        (global_dir / "profiles" / ".active").write_text("next-orion\n")
        (global_dir / "profiles" / "next-orion" / "profile.toml").write_text(
            f'base_url = "https://next.paxai.app"\n'
            f'agent_name = "orion"\n'
            f'agent_id = "agent-orion"\n'
            f'space_id = "next-space"\n'
            f'token_file = "{token_file}"\n'
        )

        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text(
            'token = "axp_u_user.secret"\n'
            'base_url = "http://localhost:8002"\n'
            'agent_name = "wire_tap"\n'
            'agent_id = "agent-wire-tap"\n'
            'space_id = "dev-space"\n'
        )
        monkeypatch.chdir(tmp_path)

        cfg = _load_config()

        assert cfg["token"] == "axp_a_agent.secret"
        assert cfg["base_url"] == "https://next.paxai.app"
        assert cfg["agent_name"] == "orion"
        assert cfg["agent_id"] == "agent-orion"
        assert cfg["space_id"] == "next-space"
        assert "Ignoring unsafe local aX config" in capsys.readouterr().err

    def test_unsafe_local_user_pat_agent_config_falls_back_to_user_login(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        _save_user_config(
            {
                "token": "axp_u_user.secret",
                "base_url": "https://dev.paxai.app",
                "principal_type": "user",
            }
        )

        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text(
            'token = "axp_u_stale.secret"\n'
            'base_url = "http://localhost:8002"\n'
            'agent_name = "wire_tap"\n'
            'agent_id = "agent-wire-tap"\n'
        )
        monkeypatch.chdir(tmp_path)

        cfg = _load_config()

        assert cfg["token"] == "axp_u_user.secret"
        assert cfg["base_url"] == "https://dev.paxai.app"
        assert cfg["principal_type"] == "user"
        assert "agent_name" not in cfg
        assert "agent_id" not in cfg


class TestAuthDoctorDiagnostics:
    def test_named_env_reports_user_login_as_effective_source(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        _save_user_config(
            {
                "token": "axp_u_dev.secret",
                "base_url": "https://dev.paxai.app",
                "principal_type": "user",
                "space_id": "dev-space",
            },
            env_name="dev",
            activate=False,
        )

        diagnostic = diagnose_auth_config(env_name="dev")

        assert diagnostic["ok"] is True
        assert diagnostic["selected_env"] == "dev"
        assert diagnostic["effective"]["auth_source"] == "user_login:dev"
        assert diagnostic["effective"]["base_url"] == "https://dev.paxai.app"
        assert diagnostic["effective"]["host"] == "dev.paxai.app"
        assert diagnostic["effective"]["space_id"] == "dev-space"
        assert diagnostic["effective"]["principal_intent"] == "user"

    def test_default_env_alias_reports_default_user_login(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        _save_user_config(
            {
                "token": "axp_u_next.secret",
                "base_url": "https://next.paxai.app",
                "principal_type": "user",
                "space_id": "next-space",
            },
            env_name="default",
            activate=False,
        )

        diagnostic = diagnose_auth_config(env_name="default")

        assert diagnostic["ok"] is True
        assert diagnostic["selected_env"] == "default"
        assert diagnostic["effective"]["auth_source"] == "user_login:default"
        assert diagnostic["effective"]["base_url"] == "https://next.paxai.app"
        assert diagnostic["effective"]["space_id"] == "next-space"
        assert diagnostic["effective"]["principal_intent"] == "user"

    def test_active_profile_reports_agent_runtime_source(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        _write_active_profile(global_dir)
        isolated = tmp_path / "isolated"
        isolated.mkdir()
        monkeypatch.chdir(isolated)

        diagnostic = diagnose_auth_config()

        assert diagnostic["ok"] is True
        assert diagnostic["selected_profile"] == "next-orion"
        assert diagnostic["effective"]["auth_source"] == "active_profile:next-orion"
        assert diagnostic["effective"]["token_kind"] == "agent_pat"
        assert diagnostic["effective"]["principal_intent"] == "agent"
        assert diagnostic["effective"]["space_id"] == "next-space"

    def test_explicit_env_vars_report_environment_sources(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        monkeypatch.setenv("AX_TOKEN", "axp_a_env.secret")
        monkeypatch.setenv("AX_BASE_URL", "https://env.paxai.app")
        monkeypatch.setenv("AX_AGENT_NAME", "env-agent")
        monkeypatch.setenv("AX_AGENT_ID", "env-agent-id")
        monkeypatch.setenv("AX_SPACE_ID", "env-space")

        diagnostic = diagnose_auth_config()

        assert diagnostic["ok"] is True
        assert diagnostic["effective"]["auth_source"] == "env:AX_TOKEN"
        assert diagnostic["effective"]["base_url_source"] == "env:AX_BASE_URL"
        assert diagnostic["effective"]["agent_name_source"] == "env:AX_AGENT_NAME"
        assert diagnostic["effective"]["agent_id_source"] == "env:AX_AGENT_ID"
        assert diagnostic["effective"]["space_source"] == "env:AX_SPACE_ID"
        assert diagnostic["effective"]["host"] == "env.paxai.app"
        assert diagnostic["effective"]["principal_intent"] == "agent"

    def test_unsafe_local_config_reports_ignored_reason_and_uses_profile(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        _write_active_profile(global_dir)

        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text(
            'token = "axp_u_user.secret"\n'
            'base_url = "http://localhost:8002"\n'
            'agent_name = "wire_tap"\n'
            'agent_id = "agent-wire-tap"\n'
            'space_id = "dev-space"\n'
        )
        monkeypatch.chdir(tmp_path)

        diagnostic = diagnose_auth_config()

        assert diagnostic["ok"] is True
        assert diagnostic["effective"]["auth_source"] == "active_profile:next-orion"
        assert diagnostic["effective"]["agent_name"] == "orion"
        assert diagnostic["effective"]["space_id"] == "next-space"
        assert any(warning["code"] == "unsafe_local_config_ignored" for warning in diagnostic["warnings"])
        local_source = next(source for source in diagnostic["sources"] if source["name"] == "local_config")
        assert local_source["ignored"] is True
        assert "user PAT" in local_source["reason"]


class TestResolveAgentId:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("AX_AGENT_ID", "env-agent-id")
        assert resolve_agent_id() == "env-agent-id"

    def test_env_none_clears(self, monkeypatch):
        monkeypatch.setenv("AX_AGENT_ID", "none")
        assert resolve_agent_id() is None

    def test_env_empty_clears(self, monkeypatch):
        monkeypatch.setenv("AX_AGENT_ID", "")
        assert resolve_agent_id() is None

    def test_env_null_clears(self, monkeypatch):
        monkeypatch.setenv("AX_AGENT_ID", "null")
        assert resolve_agent_id() is None

    def test_falls_back_to_config(self, tmp_path, monkeypatch, write_config):
        write_config(agent_id="config-agent-id")
        monkeypatch.chdir(tmp_path)
        assert resolve_agent_id() == "config-agent-id"

    def test_user_principal_ignores_stale_config_agent_id(self, tmp_path, monkeypatch, write_config):
        write_config(principal_type="user", agent_id="stale-agent-id")
        monkeypatch.chdir(tmp_path)
        assert resolve_agent_id() is None

    def test_returns_none_when_not_set(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert resolve_agent_id() is None


class TestResolveAgentName:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("AX_AGENT_NAME", "env-agent")
        assert resolve_agent_name() == "env-agent"

    def test_env_none_clears(self, monkeypatch):
        monkeypatch.setenv("AX_AGENT_NAME", "none")
        assert resolve_agent_name() is None

    def test_env_empty_clears(self, monkeypatch):
        monkeypatch.setenv("AX_AGENT_NAME", "")
        assert resolve_agent_name() is None

    def test_env_null_clears(self, monkeypatch):
        monkeypatch.setenv("AX_AGENT_NAME", "null")
        assert resolve_agent_name() is None

    def test_falls_back_to_config(self, tmp_path, monkeypatch, write_config):
        write_config(agent_name="config-agent")
        monkeypatch.chdir(tmp_path)
        assert resolve_agent_name() == "config-agent"

    def test_user_principal_ignores_stale_config_agent_name(self, tmp_path, monkeypatch, write_config):
        write_config(principal_type="user", agent_name="stale-agent")
        monkeypatch.chdir(tmp_path)
        assert resolve_agent_name() is None

    def test_returns_none_when_not_set(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert resolve_agent_name() is None


class TestResolveToken:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("AX_TOKEN", "env-token")
        assert resolve_token() == "env-token"

    def test_ax_token_file_wins_when_no_direct_env_token(self, tmp_path, monkeypatch, write_config):
        write_config(token="config-token")
        token_file = tmp_path / "agent.pat"
        token_file.write_text("file-token")
        monkeypatch.setenv("AX_TOKEN_FILE", str(token_file))
        monkeypatch.chdir(tmp_path)

        assert resolve_token() == "file-token"

    def test_falls_back_to_config(self, tmp_path, monkeypatch, write_config):
        write_config(token="config-token")
        monkeypatch.chdir(tmp_path)
        assert resolve_token() == "config-token"

    def test_returns_none_when_not_set(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert resolve_token() is None

    def test_resolve_user_token_uses_user_login_even_with_local_agent(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        _save_user_config(
            {
                "token": "axp_u_user.secret",
                "base_url": "https://next.paxai.app",
                "principal_type": "user",
            }
        )
        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text(
            'token = "axp_a_agent.secret"\nagent_name = "orion"\nagent_id = "agent-orion"\n'
        )
        monkeypatch.chdir(tmp_path)

        assert resolve_token() == "axp_a_agent.secret"
        assert resolve_user_token() == "axp_u_user.secret"

    def test_named_user_env_selects_matching_user_login(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        _save_user_config(
            {
                "token": "axp_u_next.secret",
                "base_url": "https://next.paxai.app",
                "principal_type": "user",
            },
            env_name="next",
            activate=False,
        )
        _save_user_config(
            {
                "token": "axp_u_dev.secret",
                "base_url": "https://dev.paxai.app",
                "principal_type": "user",
            },
            env_name="dev",
            activate=False,
        )

        monkeypatch.setenv("AX_ENV", "dev")

        assert resolve_user_token() == "axp_u_dev.secret"
        assert resolve_user_base_url() == "https://dev.paxai.app"


class TestResolveBaseUrl:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("AX_BASE_URL", "https://custom.example.com")
        assert resolve_base_url() == "https://custom.example.com"

    def test_falls_back_to_config(self, tmp_path, monkeypatch, write_config):
        write_config(base_url="https://config.example.com")
        monkeypatch.chdir(tmp_path)
        assert resolve_base_url() == "https://config.example.com"

    def test_default_is_localhost(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert resolve_base_url() == "http://localhost:8001"
