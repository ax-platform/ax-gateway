from pathlib import Path

from ax_cli import config


def test_local_config_dir_prefers_existing_ax_without_git(tmp_path, monkeypatch):
    root = tmp_path / "project"
    nested = root / "services" / "agent"
    (root / ".ax").mkdir(parents=True)
    nested.mkdir(parents=True)

    monkeypatch.chdir(nested)

    assert config._local_config_dir() == root / ".ax"


def test_save_local_config_creates_ax_in_non_git_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    config._save_config({"token": "axp_u_test"}, local=True)

    assert (tmp_path / ".ax" / "config.toml").exists()


def test_get_client_uses_agent_id_when_both_in_config(tmp_path, monkeypatch):
    """After bind, config has both name and id. ID must be the header sent."""
    project = tmp_path / "project"
    ax_dir = project / ".ax"
    ax_dir.mkdir(parents=True)
    (ax_dir / "config.toml").write_text(
        'token = "axp_u_test"\n'
        'base_url = "https://dev.paxai.app"\n'
        'agent_name = "orion"\n'
        'agent_id = "70c1b445-c733-44d8-8e75-9620452374a8"\n'
    )
    monkeypatch.chdir(project)
    # Clear env vars to isolate config-file path
    monkeypatch.delenv("AX_TOKEN", raising=False)
    monkeypatch.delenv("AX_AGENT_NAME", raising=False)
    monkeypatch.delenv("AX_AGENT_ID", raising=False)

    client = config.get_client()

    assert client._headers["X-Agent-Id"] == "70c1b445-c733-44d8-8e75-9620452374a8"
    assert "X-Agent-Name" not in client._headers


def test_get_client_uses_name_when_no_id_in_config(tmp_path, monkeypatch):
    """Bootstrap: config has name but no id yet → use name header."""
    project = tmp_path / "project"
    ax_dir = project / ".ax"
    ax_dir.mkdir(parents=True)
    (ax_dir / "config.toml").write_text(
        'token = "axp_u_test"\n'
        'base_url = "https://dev.paxai.app"\n'
        'agent_name = "orion"\n'
    )
    # Isolate from real global config that may contain agent_id
    global_dir = tmp_path / "global_ax"
    global_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
    monkeypatch.chdir(project)
    monkeypatch.delenv("AX_TOKEN", raising=False)
    monkeypatch.delenv("AX_AGENT_NAME", raising=False)
    monkeypatch.delenv("AX_AGENT_ID", raising=False)

    client = config.get_client()

    assert client._headers["X-Agent-Name"] == "orion"
    assert "X-Agent-Id" not in client._headers


def test_resolve_agent_id_from_env(monkeypatch):
    """AX_AGENT_ID env var is returned directly."""
    monkeypatch.setenv("AX_AGENT_ID", "70c1b445-c733-44d8-8e75-9620452374a8")
    monkeypatch.delenv("AX_AGENT_NAME", raising=False)

    assert config.resolve_agent_id() == "70c1b445-c733-44d8-8e75-9620452374a8"


def test_resolve_agent_id_env_wins_over_config(tmp_path, monkeypatch):
    """Env var agent_id takes precedence over config file."""
    project = tmp_path / "project"
    ax_dir = project / ".ax"
    ax_dir.mkdir(parents=True)
    (ax_dir / "config.toml").write_text(
        'token = "axp_u_test"\n'
        'agent_id = "config-id"\n'
    )
    monkeypatch.chdir(project)
    monkeypatch.setenv("AX_AGENT_ID", "env-id")

    assert config.resolve_agent_id() == "env-id"
