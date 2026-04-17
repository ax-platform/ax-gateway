"""Shared test fixtures for ax-cli."""
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """Ensure no leaked env vars affect tests. Use tmp_path for config."""
    for var in (
        "AX_TOKEN", "AX_BASE_URL", "AX_AGENT_NAME", "AX_AGENT_ID",
        "AX_SPACE_ID", "AX_ENV", "AX_USER_ENV", "AX_USER_TOKEN",
        "AX_USER_BASE_URL", "AX_TOKEN_FILE", "AX_CONFIG_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    # Point global config to an empty dir so real ~/.ax/ doesn't leak in
    empty_global = tmp_path / "_global_config"
    empty_global.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(empty_global))
    # Prevent tests from reading real config via project root walk
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def config_dir(tmp_path):
    """Create a temporary .ax/ config directory."""
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()
    return ax_dir


@pytest.fixture
def write_config(config_dir):
    """Helper to write a config.toml with given key-value pairs."""
    def _write(**kwargs):
        lines = []
        for k, v in kwargs.items():
            if isinstance(v, str):
                lines.append(f'{k} = "{v}"')
            else:
                lines.append(f'{k} = {v}')
        (config_dir / "config.toml").write_text("\n".join(lines) + "\n")
    return _write


@pytest.fixture
def mock_exchange(monkeypatch):
    """Mock httpx.post for token exchange, returning a fake JWT."""
    import httpx

    def _make_mock(access_token="fake.jwt.token", expires_in=900):
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = {
            "access_token": access_token,
            "expires_in": expires_in,
            "token_type": "bearer",
        }
        response.raise_for_status = MagicMock()
        mock_post = MagicMock(return_value=response)
        monkeypatch.setattr(httpx, "post", mock_post)
        return mock_post

    return _make_mock


@pytest.fixture
def sample_pat():
    """A valid-format PAT for testing."""
    return "axp_u_TestKeyId.TestSecretValue"


@pytest.fixture
def sample_agent_pat():
    """A valid-format agent-bound PAT."""
    return "axp_a_AgentKeyId.AgentSecretValue"
