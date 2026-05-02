"""ax auth doctor --probe and invalid_credential recovery copy.

Static doctor cannot tell the difference between "config layout consistent"
and "credential is alive". A `--probe` flag opt-in runs the matching
/auth/exchange so doctor can report dead credentials honestly. On rejection,
both the doctor probe path and the generic httpx error handler emit the same
recovery one-liner pointing at `axctl login --url <host>` so operators have a
clear next step.

Strict no-token-printing: even if the backend echoes the PAT in an error body,
or the request URL carries a query-string secret, no `axp_*` substring may
appear in user-facing output.
"""

import json
from unittest.mock import MagicMock

import click
import httpx
import pytest
from typer.testing import CliRunner

from ax_cli.main import app
from ax_cli.output import handle_error

runner = CliRunner()


_EXIT_TYPES = (SystemExit, click.exceptions.Exit)


@pytest.fixture
def isolated_global(tmp_path, monkeypatch):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
    return global_dir


def _write_local_agent_pat_config(tmp_path, *, token="axp_a_TestKey.TestSecret"):
    local_ax = tmp_path / ".ax"
    local_ax.mkdir()
    cfg = local_ax / "config.toml"
    cfg.write_text(
        f'token = "{token}"\n'
        'base_url = "https://paxai.app"\n'
        'agent_name = "night_owl"\n'
        'agent_id = "agent-night-owl"\n'
        'space_id = "night-space"\n'
    )
    cfg.chmod(0o600)
    return cfg


def _mock_exchange_response(monkeypatch, *, status_code, payload):
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = payload
    response.text = json.dumps(payload)
    if status_code >= 400:
        request = httpx.Request("POST", "https://paxai.app/auth/exchange")
        real_response = httpx.Response(status_code, request=request, json=payload)
        response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("error", request=request, response=real_response)
        )
    else:
        response.raise_for_status = MagicMock()
    monkeypatch.setattr(httpx, "post", MagicMock(return_value=response))
    return response


# --- doctor --probe success path -------------------------------------------------


def test_doctor_probe_success_keeps_ok_true(tmp_path, monkeypatch, isolated_global):
    _write_local_agent_pat_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    _mock_exchange_response(
        monkeypatch,
        status_code=200,
        payload={"access_token": "header.payload.sig", "expires_in": 900, "token_type": "bearer"},
    )

    result = runner.invoke(app, ["auth", "doctor", "--probe", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data.get("probe", {}).get("ok") is True


# --- doctor --probe invalid_credential -------------------------------------------


def test_doctor_probe_invalid_credential_marks_not_ok(tmp_path, monkeypatch, isolated_global):
    _write_local_agent_pat_config(tmp_path, token="axp_a_StaleKey.StaleSecret")
    monkeypatch.chdir(tmp_path)
    _mock_exchange_response(
        monkeypatch,
        status_code=401,
        payload={"detail": {"error": "invalid_credential", "message": "PAT rejected"}},
    )

    result = runner.invoke(app, ["auth", "doctor", "--probe", "--json"])
    assert result.exit_code != 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is False
    probe = data.get("probe") or {}
    assert probe.get("ok") is False
    assert probe.get("code") == "invalid_credential"


def test_doctor_probe_invalid_credential_emits_recovery_copy(tmp_path, monkeypatch, isolated_global):
    _write_local_agent_pat_config(tmp_path, token="axp_a_StaleKey.StaleSecret")
    monkeypatch.chdir(tmp_path)
    _mock_exchange_response(
        monkeypatch,
        status_code=401,
        payload={"detail": {"error": "invalid_credential", "message": "PAT rejected"}},
    )

    result = runner.invoke(app, ["auth", "doctor", "--probe"])
    assert result.exit_code != 0
    output = result.output.lower()
    assert "axctl login" in output
    assert "different environment" in output or "wrong environment" in output


def test_doctor_probe_never_prints_token(tmp_path, monkeypatch, isolated_global):
    secret = "axp_a_VerySecretKey.VerySecretSecretValue"
    _write_local_agent_pat_config(tmp_path, token=secret)
    monkeypatch.chdir(tmp_path)
    _mock_exchange_response(
        monkeypatch,
        status_code=401,
        payload={
            "detail": {
                "error": "invalid_credential",
                "message": f"PAT rejected: {secret}",
            }
        },
    )

    result = runner.invoke(app, ["auth", "doctor", "--probe", "--json"])
    assert secret not in result.output, "doctor leaked PAT into output"
    assert "axp_a_VerySecretKey" not in result.output


def test_doctor_without_probe_makes_no_http_call(tmp_path, monkeypatch, isolated_global):
    _write_local_agent_pat_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    called = {"count": 0}

    def boom(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("doctor without --probe must not hit the network")

    monkeypatch.setattr(httpx, "post", boom)

    result = runner.invoke(app, ["auth", "doctor", "--json"])
    assert result.exit_code == 0, result.output
    assert called["count"] == 0


def test_doctor_probe_skipped_for_gateway_managed_config(tmp_path, monkeypatch, isolated_global):
    """Gateway-brokered shape has no PAT to probe; doctor must skip cleanly."""
    local_ax = tmp_path / ".ax"
    local_ax.mkdir()
    (local_ax / "config.toml").write_text(
        "[gateway]\n"
        'mode = "local"\n'
        'url = "http://127.0.0.1:8765"\n'
        "\n"
        "[agent]\n"
        'agent_name = "cli_god"\n'
        f'workdir = "{tmp_path}"\n'
    )
    monkeypatch.chdir(tmp_path)

    def boom(*args, **kwargs):
        raise AssertionError("Gateway-brokered shape has no PAT; --probe must skip without HTTP")

    monkeypatch.setattr(httpx, "post", boom)

    result = runner.invoke(app, ["auth", "doctor", "--probe", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    probe = data.get("probe") or {}
    assert probe.get("skipped") is True
    assert "gateway" in (probe.get("reason") or "").lower()


# --- handle_error tightening -----------------------------------------------------


def test_handle_error_invalid_credential_emits_doctor_hint(capsys):
    request = httpx.Request("POST", "https://paxai.app/auth/exchange")
    response = httpx.Response(
        401,
        request=request,
        json={"detail": {"error": "invalid_credential", "message": "PAT rejected"}},
    )
    error = httpx.HTTPStatusError("invalid", request=request, response=response)

    with pytest.raises(_EXIT_TYPES):
        handle_error(error)

    captured = capsys.readouterr()
    combined = (captured.err + captured.out).lower()
    assert "axctl auth doctor --probe" in combined or "axctl login" in combined


def test_handle_error_redacts_pat_substrings_in_body(capsys):
    secret = "axp_a_LeakKey.LeakSecretValue"
    request = httpx.Request("POST", "https://paxai.app/auth/exchange")
    response = httpx.Response(
        401,
        request=request,
        json={
            "detail": {
                "error": "invalid_credential",
                "message": f"PAT rejected: {secret}",
            }
        },
    )
    error = httpx.HTTPStatusError("invalid", request=request, response=response)

    with pytest.raises(_EXIT_TYPES):
        handle_error(error)

    captured = capsys.readouterr()
    assert secret not in captured.err
    assert secret not in captured.out
    assert "axp_a_LeakKey" not in captured.err
    assert "axp_a_LeakKey" not in captured.out


# --- tasks list no axp_ leakage --------------------------------------------------


def test_tasks_list_json_does_not_leak_axp_in_stdout(monkeypatch):
    """Black-box contract: a successful `ax tasks list --json` must never echo
    any axp_ substring through stdout. Pins against accidental token printing
    in any envelope/summary field."""

    class FakeClient:
        def list_tasks(self, *, space_id=None, status=None, assignee_id=None, limit=None):
            return {
                "tasks": [
                    {"id": "t1", "title": "Wire CLI through Gateway", "status": "open"},
                    {"id": "t2", "title": "Tighten doctor", "status": "in_progress"},
                ]
            }

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(app, ["tasks", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert "axp_" not in result.output, "tasks list leaked an axp_ substring"
