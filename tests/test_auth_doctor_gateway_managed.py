"""Doctor must understand the Gateway-managed local config shape.

When a project's `.ax/config.toml` looks like:

    [gateway]
    mode = "local"
    url = "http://127.0.0.1:8765"

    [agent]
    agent_name = "cli_god"
    workdir = "/path/to/workspace"

…the credential is brokered by Gateway and there is no top-level token. The
old static doctor reported `missing_token` / PROBLEM in that situation, even
though the runtime path works fine. Doctor must surface the Gateway-brokered
shape honestly: ok=True, principal_intent=agent, auth_source local_config:gateway,
base_url hydrated from `[gateway].url`, with an informational warning
explaining the credential is held by Gateway out-of-band.
"""

import pytest

from ax_cli.config import diagnose_auth_config


@pytest.fixture
def isolated_global(tmp_path, monkeypatch):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
    return global_dir


def _write_local_gateway_config(tmp_path, *, url="http://127.0.0.1:8765", agent_name="cli_god"):
    local_ax = tmp_path / ".ax"
    local_ax.mkdir()
    (local_ax / "config.toml").write_text(
        "[gateway]\n"
        'mode = "local"\n'
        f'url = "{url}"\n'
        "\n"
        "[agent]\n"
        f'agent_name = "{agent_name}"\n'
        f'workdir = "{tmp_path}"\n'
    )
    return local_ax / "config.toml"


def test_doctor_sees_gateway_managed_local_config_as_ok(tmp_path, monkeypatch, isolated_global):
    _write_local_gateway_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    diagnostic = diagnose_auth_config()

    assert diagnostic["ok"] is True
    assert diagnostic["effective"]["principal_intent"] == "agent"
    assert diagnostic["effective"]["auth_source"] == "local_config:gateway"
    assert diagnostic["effective"]["base_url"] == "http://127.0.0.1:8765"
    assert diagnostic["effective"]["base_url_source"] == "local_config:gateway"
    assert diagnostic["effective"]["agent_name"] == "cli_god"
    assert diagnostic["effective"]["agent_name_source"] == "local_config:gateway"


def test_doctor_warns_credential_is_brokered_by_gateway(tmp_path, monkeypatch, isolated_global):
    _write_local_gateway_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    diagnostic = diagnose_auth_config()

    warning_codes = {w["code"] for w in diagnostic.get("warnings", [])}
    assert "credential_brokered_by_gateway" in warning_codes


def test_doctor_does_not_emit_missing_token_problem_for_gateway_managed_config(
    tmp_path, monkeypatch, isolated_global
):
    _write_local_gateway_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    diagnostic = diagnose_auth_config()

    problem_codes = {p["code"] for p in diagnostic.get("problems", [])}
    assert "missing_token" not in problem_codes


def test_doctor_uses_gateway_url_not_localhost_8001_default(tmp_path, monkeypatch, isolated_global):
    """Regression: the old default base_url 'http://localhost:8001' leaked
    through whenever no source set base_url. Gateway-managed config should
    hydrate base_url from `[gateway].url` instead."""
    _write_local_gateway_config(tmp_path, url="http://127.0.0.1:8765")
    monkeypatch.chdir(tmp_path)

    diagnostic = diagnose_auth_config()

    assert diagnostic["effective"]["base_url"] != "http://localhost:8001"
    assert diagnostic["effective"]["base_url"] == "http://127.0.0.1:8765"


def test_unsafe_user_pat_with_agent_identity_still_flagged(tmp_path, monkeypatch, isolated_global):
    """Slice B must NOT regress the unsafe-local guard. A local config that
    combines a user PAT with an agent identity must still be treated as
    unsafe — the Gateway shape is recognized as a *separate* trusted shape."""
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

    warning_codes = {w["code"] for w in diagnostic.get("warnings", [])}
    assert "unsafe_local_config_ignored" in warning_codes
