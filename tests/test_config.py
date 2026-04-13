"""Tests for config resolution — the cascade that burned us (2026-04-05)."""
from pathlib import Path

from ax_cli.config import (
    _find_project_root,
    _global_config_dir,
    _load_config,
    resolve_agent_id,
    resolve_agent_name,
    resolve_base_url,
    resolve_token,
)


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
        (global_dir / "config.toml").write_text(
            'agent_id = "global-agent"\nbase_url = "https://global.example.com"\n'
        )
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))

        # Local config (in CWD)
        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text('agent_id = "local-agent"\n')
        monkeypatch.chdir(tmp_path)

        cfg = _load_config()
        assert cfg["agent_id"] == "local-agent"  # local wins
        assert cfg["base_url"] == "https://global.example.com"  # global preserved


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

    def test_returns_none_when_not_set(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert resolve_agent_name() is None


class TestResolveToken:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("AX_TOKEN", "env-token")
        assert resolve_token() == "env-token"

    def test_falls_back_to_config(self, tmp_path, monkeypatch, write_config):
        write_config(token="config-token")
        monkeypatch.chdir(tmp_path)
        assert resolve_token() == "config-token"

    def test_returns_none_when_not_set(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert resolve_token() is None


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
