import json
import os
from pathlib import Path

import httpx
from typer.testing import CliRunner

from ax_cli.main import app

runner = CliRunner()


class FakeMintClient:
    base_url = "https://paxai.app"

    def list_agents(self):
        return {
            "agents": [
                {
                    "id": "12345678-90ab-cdef-1234-567890abcdef",
                    "name": "orion",
                }
            ]
        }

    def mgmt_issue_agent_pat(self, agent_id, *, name=None, expires_in_days=90, audience="both"):
        return {
            "token": "axp_a_newly_minted.secret",
            "expires_at": "2026-05-13T00:00:00Z",
            "agent_id": agent_id,
            "name": name,
            "audience": audience,
        }


class FakeCreateFallbackClient(FakeMintClient):
    def list_agents(self):
        return {"agents": []}

    def get_agent(self, agent):
        raise httpx.HTTPStatusError(
            "not found",
            request=httpx.Request("GET", f"https://paxai.app/api/v1/agents/manage/{agent}"),
            response=httpx.Response(
                404,
                json={"detail": "not found"},
                request=httpx.Request("GET", f"https://paxai.app/api/v1/agents/manage/{agent}"),
            ),
        )

    def mgmt_create_agent(self, agent):
        raise httpx.HTTPStatusError(
            "Expected JSON but got HTML",
            request=httpx.Request("POST", "https://paxai.app/api/v1/agents/manage/create"),
            response=httpx.Response(
                200,
                text="<!DOCTYPE html><html></html>",
                headers={"content-type": "text/html"},
                request=httpx.Request("POST", "https://paxai.app/api/v1/agents/manage/create"),
            ),
        )

    def create_agent(self, agent):
        return {"id": "agent-created", "name": agent}


def test_token_mint_prints_token_when_not_saving(monkeypatch, write_config):
    write_config(token="axp_u_user.secret", base_url="https://paxai.app")
    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: FakeMintClient())

    result = runner.invoke(app, ["token", "mint", "orion"])

    assert result.exit_code == 0, result.output
    assert "axp_a_newly_minted.secret" in result.output


def test_token_mint_create_falls_back_to_agents_api_when_management_route_is_frontend(
    monkeypatch, write_config, tmp_path
):
    write_config(token="axp_u_user.secret", base_url="https://paxai.app")
    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: FakeCreateFallbackClient())

    result = runner.invoke(
        app,
        [
            "token",
            "mint",
            "new-agent",
            "--create",
            "--save-to",
            str(tmp_path),
            "--no-print-token",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Created:" in result.output
    assert "new-agent" in result.output
    assert "axp_a_newly_minted.secret" not in result.output


def test_token_mint_hides_token_when_saved(monkeypatch, write_config, tmp_path):
    write_config(token="axp_u_user.secret", base_url="https://paxai.app")
    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: FakeMintClient())

    result = runner.invoke(app, ["token", "mint", "orion", "--save-to", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "axp_a_newly_minted.secret" not in result.output
    assert "not printed" in result.output
    assert (tmp_path / ".ax" / "orion_token").read_text() == "axp_a_newly_minted.secret"


def test_token_mint_json_hides_token_when_saved(monkeypatch, write_config, tmp_path):
    write_config(token="axp_u_user.secret", base_url="https://paxai.app")
    (tmp_path / ".ax" / "config.toml").chmod(0o600)
    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: FakeMintClient())

    result = runner.invoke(app, ["token", "mint", "orion", "--save-to", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "token" not in payload
    assert payload["token_redacted"] is True
    assert payload["token_printed"] is False
    assert payload["token_file"].endswith(".ax/orion_token")


def test_token_mint_can_print_saved_token_when_explicit(monkeypatch, write_config, tmp_path):
    write_config(token="axp_u_user.secret", base_url="https://paxai.app")
    (tmp_path / ".ax" / "config.toml").chmod(0o600)
    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: FakeMintClient())

    result = runner.invoke(app, ["token", "mint", "orion", "--save-to", str(tmp_path), "--print-token", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["token"] == "axp_a_newly_minted.secret"
    assert payload["token_printed"] is True


def test_token_mint_uses_user_login_when_local_config_is_agent(monkeypatch, write_config):
    write_config(
        token="axp_a_agent.secret",
        base_url="https://paxai.app",
        agent_name="orion",
        agent_id="agent-orion",
    )
    user_config_dir = Path(os.environ["AX_CONFIG_DIR"])
    user_config_dir.mkdir(parents=True, exist_ok=True)
    (user_config_dir / "user.toml").write_text(
        'token = "axp_u_user.secret"\nbase_url = "https://paxai.app"\nprincipal_type = "user"\n'
    )
    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: FakeMintClient())

    result = runner.invoke(app, ["token", "mint", "orion"])

    assert result.exit_code == 0, result.output
    assert "axp_a_newly_minted.secret" in result.output


def test_token_mint_env_selects_named_user_login(monkeypatch, write_config):
    write_config(token="axp_a_agent.secret", base_url="https://paxai.app", agent_name="orion")
    monkeypatch.setenv("AX_USER_TOKEN", "axp_u_dev.secret")

    def fake_get_user_client():
        assert os.environ["AX_USER_ENV"] == "dev"
        return FakeMintClient()

    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", fake_get_user_client)

    result = runner.invoke(app, ["token", "mint", "orion", "--env", "dev"])

    assert result.exit_code == 0, result.output
    assert "axp_a_newly_minted.secret" in result.output
