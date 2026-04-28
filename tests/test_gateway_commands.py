import json
import socket
import sys
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from ax_cli import gateway as gateway_core
from ax_cli import gateway_runtime_types
from ax_cli.commands import gateway as gateway_cmd
from ax_cli.main import app

runner = CliRunner()


class _FakeTokenExchanger:
    def __init__(self, base_url, token):
        self.base_url = base_url
        self.token = token

    def get_token(self, *args, **kwargs):
        return "jwt-test"


class _FakeLoginClient:
    def __init__(self, *args, **kwargs):
        self.base_url = kwargs["base_url"]
        self.token = kwargs["token"]

    def whoami(self):
        return {"username": "madtank", "email": "madtank@example.com"}

    def list_spaces(self):
        return {"spaces": [{"id": "space-1", "name": "Workspace", "is_default": True}]}


class _FakeHttpResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://example.test")
            response = httpx.Response(self.status_code, request=request, json=self.payload)
            raise httpx.HTTPStatusError("error", request=request, response=response)

    def json(self):
        return self.payload


class _FakeUserClient:
    def update_agent(self, *args, **kwargs):
        return {"ok": True}

    def send_message(self, space_id, content, *, agent_id=None, parent_id=None, metadata=None):
        return {
            "message": {
                "id": "gateway-test-1",
                "space_id": space_id,
                "content": content,
                "agent_id": agent_id,
                "parent_id": parent_id,
                "metadata": metadata,
            }
        }


def _fake_create_agent_in_space(*args, **kwargs):
    name = kwargs.get("name", "agent")
    return {"id": f"agent-{name}", "name": name}


class _FakeSseResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def close(self):
        self.closed = True

    def iter_lines(self):
        yield "event: connected"
        yield "data: {}"
        yield ""
        yield "event: message"
        yield f"data: {json.dumps(self.payload)}"
        yield ""


class _SharedRuntimeClient:
    def __init__(self, payload):
        self.payload = payload
        self.sent = []
        self.processing = []
        self.tool_calls = []
        self.connect_calls = 0

    def connect_sse(self, *, space_id, timeout=None):
        self.connect_calls += 1
        if self.connect_calls > 1:
            raise ConnectionError("test done")
        return _FakeSseResponse(self.payload)

    def send_message(self, space_id, content, *, agent_id=None, parent_id=None, **kwargs):
        self.sent.append(
            {
                "space_id": space_id,
                "content": content,
                "agent_id": agent_id,
                "parent_id": parent_id,
                "metadata": kwargs.get("metadata"),
            }
        )
        return {"message": {"id": "reply-1"}}

    def set_agent_processing_status(self, message_id, status, *, agent_name=None, space_id=None, **kwargs):
        payload = {
            "message_id": message_id,
            "status": status,
            "agent_name": agent_name,
            "space_id": space_id,
        }
        payload.update(kwargs)
        self.processing.append(payload)
        return {"ok": True}

    def record_tool_call(self, **payload):
        self.tool_calls.append(payload)
        return {"ok": True, "tool_call_id": payload["tool_call_id"]}

    def close(self):
        return None


class _FakeManagedSendClient:
    def __init__(self, *args, **kwargs):
        self.base_url = kwargs["base_url"]
        self.token = kwargs["token"]
        self.agent_name = kwargs.get("agent_name")
        self.agent_id = kwargs.get("agent_id")

    def send_message(self, space_id, content, *, agent_id=None, parent_id=None, metadata=None):
        return {
            "message": {
                "id": "msg-sent-1",
                "space_id": space_id,
                "content": content,
                "agent_id": agent_id,
                "parent_id": parent_id,
                "metadata": metadata,
            }
        }


def test_gateway_login_saves_gateway_session(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", _FakeTokenExchanger)
    monkeypatch.setattr(gateway_cmd, "AxClient", _FakeLoginClient)

    result = runner.invoke(
        app,
        ["gateway", "login", "--token", "axp_u_test.token", "--url", "https://paxai.app", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["base_url"] == "https://paxai.app"
    assert payload["space_id"] == "space-1"
    session = gateway_core.load_gateway_session()
    assert session["token"] == "axp_u_test.token"
    assert session["base_url"] == "https://paxai.app"
    assert not (config_dir / "user.toml").exists()
    recent = gateway_core.load_recent_gateway_activity()
    assert recent[-1]["event"] == "gateway_login"
    assert recent[-1]["username"] == "madtank"


def test_gateway_state_dir_isolated_by_environment(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("AX_GATEWAY_ENV", "dev/staging")

    assert gateway_core.gateway_environment() == "dev-staging"
    assert gateway_core.gateway_dir() == config_dir / "gateway" / "envs" / "dev-staging"
    assert gateway_core.session_path() == config_dir / "gateway" / "envs" / "dev-staging" / "session.json"


def test_gateway_state_dir_allows_explicit_override(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    custom_dir = tmp_path / "custom-gateway"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("AX_GATEWAY_ENV", "prod")
    monkeypatch.setenv("AX_GATEWAY_DIR", str(custom_dir))

    assert gateway_core.gateway_environment() == "prod"
    assert gateway_core.gateway_dir() == custom_dir
    assert gateway_core.registry_path() == custom_dir / "registry.json"


def test_gateway_run_refuses_second_live_daemon(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "madtank",
        }
    )
    gateway_core.write_gateway_pid(4242)
    monkeypatch.setattr(gateway_core, "_pid_alive", lambda pid: pid == 4242)

    result = runner.invoke(app, ["gateway", "run", "--once"])

    assert result.exit_code == 1, result.output
    assert "Gateway already running (pid 4242)." in result.output
    recent = gateway_core.load_recent_gateway_activity()
    assert recent[-1]["event"] == "gateway_start_blocked"
    assert recent[-1]["existing_pid"] == 4242


def test_gateway_run_refuses_process_table_daemon_when_pid_file_missing(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "madtank",
        }
    )
    monkeypatch.setattr(gateway_core, "_scan_gateway_process_pids", lambda: [5514])

    result = runner.invoke(app, ["gateway", "run", "--once"])

    assert result.exit_code == 1, result.output
    assert "Gateway already running (pid 5514)." in result.output
    recent = gateway_core.load_recent_gateway_activity()
    assert recent[-1]["event"] == "gateway_start_blocked"
    assert recent[-1]["existing_pids"] == [5514]


def test_clear_gateway_pid_keeps_newer_owner(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.write_gateway_pid(22179)

    gateway_core.clear_gateway_pid(5514)

    assert gateway_core.pid_path().exists()
    assert gateway_core.pid_path().read_text().strip() == "22179"


def test_scan_gateway_process_pids_ignores_current_parent_wrapper(monkeypatch):
    monkeypatch.setattr(gateway_core.os, "getpid", lambda: 22179)
    monkeypatch.setattr(gateway_core.os, "getppid", lambda: 22178)
    monkeypatch.setattr(gateway_core, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        gateway_core.subprocess,
        "check_output",
        lambda *args, **kwargs: "\n".join(
            [
                "22178 uv run ax gateway run",
                "22179 /Users/jacob/claude_home/ax-cli/.venv/bin/python3 /Users/jacob/claude_home/ax-cli/.venv/bin/ax gateway run",
                "5514 /Users/jacob/claude_home/ax-cli/.venv/bin/python3 /Users/jacob/claude_home/ax-cli/.venv/bin/ax gateway run",
            ]
        ),
    )

    assert gateway_core._scan_gateway_process_pids() == [5514]


def test_gateway_start_launches_background_daemon_and_ui(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "madtank",
        }
    )

    state = {"daemon_pid": None, "ui_pid": None}
    spawned: list[tuple[list[str], str]] = []

    class _FakeProcess:
        def __init__(self, pid: int):
            self.pid = pid

        def poll(self):
            return None

    def fake_spawn(command, *, log_path):
        spawned.append((command, str(log_path)))
        if "run" in command:
            state["daemon_pid"] = 5514
            return _FakeProcess(5514)
        state["ui_pid"] = 5515
        return _FakeProcess(5515)

    monkeypatch.setattr(gateway_cmd, "_spawn_gateway_background_process", fake_spawn)
    monkeypatch.setattr(gateway_cmd, "_wait_for_daemon_ready", lambda process, timeout=3.0: True)
    monkeypatch.setattr(gateway_cmd, "_wait_for_ui_ready", lambda process, host, port, timeout=3.0: True)
    monkeypatch.setattr(gateway_cmd, "active_gateway_pid", lambda: state["daemon_pid"])
    monkeypatch.setattr(gateway_cmd, "active_gateway_ui_pid", lambda: state["ui_pid"])
    monkeypatch.setattr(
        gateway_cmd,
        "ui_status",
        lambda: {
            "running": True,
            "pid": state["ui_pid"],
            "host": "127.0.0.1",
            "port": 8765,
            "url": "http://127.0.0.1:8765",
            "log_path": str(gateway_core.ui_log_path()),
        },
    )
    opened: list[str] = []
    monkeypatch.setattr(gateway_cmd.webbrowser, "open_new_tab", lambda url: opened.append(url))

    result = runner.invoke(app, ["gateway", "start", "--no-open"])

    assert result.exit_code == 0, result.output
    assert "daemon    = started" in result.output
    assert "ui        = started" in result.output
    assert len(spawned) == 2
    assert "gateway" in spawned[0][0] and "run" in spawned[0][0]
    assert spawned[0][0][-2:] == ["--poll-interval", "1.0"]
    assert "gateway" in spawned[1][0] and "ui" in spawned[1][0]
    assert opened == []


def test_gateway_cli_argv_prefers_current_ax_script(monkeypatch, tmp_path):
    current_ax = tmp_path / "bin" / "ax"
    current_ax.parent.mkdir(parents=True)
    current_ax.write_text("#!/bin/sh\n")
    current_ax.chmod(0o755)

    monkeypatch.setattr(gateway_cmd.sys, "argv", [str(current_ax), "gateway", "start"])
    monkeypatch.setattr(gateway_cmd.sys, "executable", "/opt/homebrew/bin/python3")
    monkeypatch.setattr(gateway_cmd.shutil, "which", lambda name: f"/opt/homebrew/bin/{name}")

    argv = gateway_cmd._gateway_cli_argv("gateway", "run")

    assert argv == [str(current_ax.resolve()), "gateway", "run"]


def test_gateway_start_without_login_starts_ui_only(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))

    state = {"ui_pid": None}
    spawned: list[list[str]] = []

    class _FakeProcess:
        def __init__(self, pid: int):
            self.pid = pid

        def poll(self):
            return None

    def fake_spawn(command, *, log_path):
        spawned.append(command)
        state["ui_pid"] = 6615
        return _FakeProcess(6615)

    monkeypatch.setattr(gateway_cmd, "_spawn_gateway_background_process", fake_spawn)
    monkeypatch.setattr(gateway_cmd, "_wait_for_ui_ready", lambda process, host, port, timeout=3.0: True)
    monkeypatch.setattr(gateway_cmd, "active_gateway_pid", lambda: None)
    monkeypatch.setattr(gateway_cmd, "active_gateway_ui_pid", lambda: state["ui_pid"])
    monkeypatch.setattr(
        gateway_cmd,
        "ui_status",
        lambda: {
            "running": True,
            "pid": state["ui_pid"],
            "host": "127.0.0.1",
            "port": 8765,
            "url": "http://127.0.0.1:8765",
            "log_path": str(gateway_core.ui_log_path()),
        },
    )

    result = runner.invoke(app, ["gateway", "start", "--no-open"])

    assert result.exit_code == 0, result.output
    assert "Gateway is not logged in yet" in result.output
    assert len(spawned) == 1
    assert "gateway" in spawned[0] and "ui" in spawned[0]


def test_gateway_stop_terminates_daemon_and_ui(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(gateway_cmd, "active_gateway_pids", lambda: [7714])
    monkeypatch.setattr(gateway_cmd, "active_gateway_ui_pids", lambda: [7715])
    monkeypatch.setattr(
        gateway_cmd,
        "_terminate_pids",
        lambda pids, timeout=3.0: (list(pids), [pids[0]] if pids and pids[0] == 7714 else []),
    )

    result = runner.invoke(app, ["gateway", "stop"])

    assert result.exit_code == 0, result.output
    assert "daemon = [7714]" in result.output
    assert "ui     = [7715]" in result.output
    assert "Forced kill:" in result.output


def test_gateway_start_rolls_back_daemon_when_ui_start_fails(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "madtank",
        }
    )

    state = {"daemon_pid": None, "ui_pid": None}

    class _FakeProcess:
        def __init__(self, pid: int):
            self.pid = pid

        def poll(self):
            return None

    def fake_spawn(command, *, log_path):
        if "run" in command:
            state["daemon_pid"] = 8814
            return _FakeProcess(8814)
        state["ui_pid"] = 8815
        return _FakeProcess(8815)

    terminated: list[list[int]] = []
    cleared: list[int | None] = []

    monkeypatch.setattr(gateway_cmd, "_spawn_gateway_background_process", fake_spawn)
    monkeypatch.setattr(gateway_cmd, "_wait_for_daemon_ready", lambda process, timeout=3.0: True)
    monkeypatch.setattr(gateway_cmd, "_wait_for_ui_ready", lambda process, host, port, timeout=3.0: False)
    monkeypatch.setattr(gateway_cmd, "active_gateway_pid", lambda: state["daemon_pid"])
    monkeypatch.setattr(gateway_cmd, "active_gateway_ui_pid", lambda: state["ui_pid"])
    monkeypatch.setattr(gateway_cmd, "_tail_log_lines", lambda path, lines=12: "address already in use")
    monkeypatch.setattr(
        gateway_cmd, "_terminate_pids", lambda pids, timeout=3.0: terminated.append(list(pids)) or (list(pids), [])
    )
    monkeypatch.setattr(gateway_core, "clear_gateway_pid", lambda pid=None: cleared.append(pid))

    result = runner.invoke(app, ["gateway", "start", "--no-open"])

    assert result.exit_code == 1, result.output
    assert "Failed to start Gateway UI." in result.output
    assert terminated == [[8814]]
    assert cleared == [None]


def test_gateway_agents_add_mints_token_and_writes_registry(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "madtank",
        }
    )
    monkeypatch.setattr(gateway_cmd, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(gateway_cmd, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway_cmd,
        "_create_agent_in_space",
        lambda *args, **kwargs: {"id": "agent-1", "name": "echo-bot"},
    )
    monkeypatch.setattr(gateway_cmd, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_cmd, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))

    result = runner.invoke(app, ["gateway", "agents", "add", "echo-bot", "--type", "echo", "--timeout", "42", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["name"] == "echo-bot"
    assert payload["runtime_type"] == "echo"
    assert payload["timeout_seconds"] == 42
    assert payload["desired_state"] == "running"
    assert payload["credential_source"] == "gateway"
    assert payload["transport"] == "gateway"
    registry = gateway_core.load_gateway_registry()
    assert registry["agents"][0]["name"] == "echo-bot"
    assert registry["agents"][0]["timeout_seconds"] == 42
    assert registry["bindings"][0]["asset_id"] == "agent-1"
    assert registry["bindings"][0]["approved_state"] == "approved"
    assert registry["agents"][0]["install_id"] == registry["bindings"][0]["install_id"]
    token_file = Path(registry["agents"][0]["token_file"])
    assert token_file.exists()
    assert token_file.read_text().strip() == "axp_a_agent.secret"
    recent = gateway_core.load_recent_gateway_activity()
    assert recent[-1]["event"] == "managed_agent_added"
    assert recent[-1]["agent_name"] == "echo-bot"


def test_gateway_agents_add_pass_through_requires_fingerprint_approval(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "madtank",
        }
    )
    monkeypatch.setattr(gateway_cmd, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(gateway_cmd, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway_cmd,
        "_create_agent_in_space",
        lambda *args, **kwargs: {"id": "agent-pass-1", "name": "codex-pass"},
    )
    monkeypatch.setattr(gateway_cmd, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_cmd, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))

    result = runner.invoke(app, ["gateway", "agents", "add", "codex-pass", "--template", "pass_through", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["name"] == "codex-pass"
    assert payload["template_id"] == "pass_through"
    assert payload["runtime_type"] == "inbox"
    assert payload["approval_state"] == "pending"
    assert payload["approval_id"]
    assert payload["attestation_state"] == "unknown"
    registry = gateway_core.load_gateway_registry()
    assert registry["bindings"] == []
    assert registry["approvals"][0]["approval_kind"] == "new_binding"
    assert registry["approvals"][0]["candidate_binding"]["path"] == str(Path(__file__).resolve().parent.parent)


def test_gateway_local_connect_requests_approval_then_issues_session(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "madtank",
        }
    )
    monkeypatch.setattr(gateway_cmd, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(gateway_cmd, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway_cmd,
        "_create_agent_in_space",
        lambda *args, **kwargs: {"id": "agent-local-1", "name": "codex-local"},
    )
    monkeypatch.setattr(gateway_cmd, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_cmd, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))
    fingerprint = {
        "agent_name": "codex-local",
        "pid": 999999,
        "parent_pid": 1,
        "cwd": str(tmp_path),
        "exe_path": sys.executable,
        "user": "madtank",
    }

    first = gateway_cmd._connect_local_pass_through_agent(agent_name="codex-local", fingerprint=fingerprint)

    assert first["status"] == "pending"
    assert first["approval_id"]
    registry = gateway_core.load_gateway_registry()
    entry = gateway_core.find_agent_entry(registry, "codex-local")
    assert entry is not None
    assert entry["template_id"] == "pass_through"
    assert entry["local_connection_mode"] == "pass_through"
    assert registry["approvals"][0]["status"] == "pending"

    gateway_core.approve_gateway_approval(first["approval_id"])
    second = gateway_cmd._connect_local_pass_through_agent(agent_name="codex-local", fingerprint=fingerprint)

    assert second["status"] == "approved"
    assert second["session_token"].startswith("axgw_s_")
    stored = gateway_core.load_gateway_registry()
    session = gateway_core.verify_local_session_token(stored, second["session_token"])
    assert session["agent_name"] == "codex-local"
    queued_entry = gateway_core.find_agent_entry(stored, "codex-local")
    assert queued_entry is not None
    queued_entry["backlog_depth"] = 1
    queued_entry["queue_depth"] = 1
    queued_entry["current_status"] = "queued"
    queued_entry["current_activity"] = "Queued in Gateway"
    queued_entry["last_received_message_id"] = "queued-local-1"
    gateway_core.save_gateway_registry(stored)
    gateway_core.save_agent_pending_messages(
        "codex-local",
        [
            {
                "message_id": "queued-local-1",
                "content": "@codex-local please check this",
                "display_name": "madtank",
                "created_at": "2026-04-25T11:59:00Z",
                "queued_at": "2026-04-25T12:00:00Z",
            }
        ],
    )

    third = gateway_cmd._connect_local_pass_through_agent(registry_ref="#1", fingerprint=fingerprint)

    assert third["status"] == "approved"
    assert third["registry_ref"] == "#1"
    assert third["agent"]["name"] == "codex-local"
    assert third["session_token"].startswith("axgw_s_")

    calls = {}

    class FakeManagedClient:
        def __init__(self):
            self.sent = []

        def send_message(
            self,
            space_id,
            content,
            *,
            agent_id=None,
            channel="main",
            parent_id=None,
            metadata=None,
            message_type="text",
        ):
            self.sent.append(
                {
                    "space_id": space_id,
                    "content": content,
                    "agent_id": agent_id,
                    "channel": channel,
                    "parent_id": parent_id,
                    "metadata": metadata,
                    "message_type": message_type,
                }
            )
            return {
                "message": {
                    "id": "local-send-1",
                    "sender_type": "agent",
                    "display_name": "codex-local",
                    "agent_id": agent_id,
                    "metadata": metadata,
                }
            }

        def list_messages(
            self,
            limit=20,
            channel="main",
            *,
            space_id=None,
            agent_id=None,
            unread_only=False,
            mark_read=False,
        ):
            calls["list"] = {
                "limit": limit,
                "channel": channel,
                "space_id": space_id,
                "agent_id": agent_id,
                "unread_only": unread_only,
                "mark_read": mark_read,
            }
            return {
                "messages": [
                    {
                        "id": "msg-1",
                        "content": "approve this deployment",
                        "display_name": "orion",
                        "created_at": "2026-04-25T12:00:00Z",
                    }
                ],
                "unread_count": 1,
                "marked_read_count": 1,
            }

    managed_client = FakeManagedClient()
    monkeypatch.setattr(gateway_cmd, "_load_managed_agent_client", lambda entry: managed_client)

    sent = gateway_cmd._send_local_session_message(
        session_token=second["session_token"],
        body={
            "space_id": "space-1",
            "content": "@night_owl please review",
            "parent_id": "parent-1",
            "metadata": {"purpose": "review"},
        },
    )

    assert sent["agent"] == "codex-local"
    assert sent["message"]["message"]["sender_type"] == "agent"
    assert sent["message"]["message"]["display_name"] == "codex-local"
    assert managed_client.sent == [
        {
            "space_id": "space-1",
            "content": "@night_owl please review",
            "agent_id": "agent-local-1",
            "channel": "main",
            "parent_id": "parent-1",
            "metadata": {
                "purpose": "review",
                "gateway_local_session_id": session["session_id"],
                "gateway_pass_through_agent": "codex-local",
                "gateway_pass_through_agent_id": "agent-local-1",
                "gateway_pass_through_fingerprint_signature": session["fingerprint_signature"],
            },
            "message_type": "text",
        }
    ]

    inbox = gateway_cmd._local_session_inbox(session_token=second["session_token"], limit=5)

    assert inbox["agent"] == "codex-local"
    assert inbox["messages"][0]["content"] == "approve this deployment"
    assert gateway_core.load_agent_pending_messages("codex-local") == []
    updated_entry = gateway_core.find_agent_entry(gateway_core.load_gateway_registry(), "codex-local")
    assert updated_entry["backlog_depth"] == 0
    assert updated_entry["queue_depth"] == 0
    assert updated_entry["current_status"] is None
    assert updated_entry["current_activity"] is None
    assert calls["list"] == {
        "limit": 5,
        "channel": "main",
        "space_id": "space-1",
        "agent_id": "agent-local-1",
        "unread_only": True,
        "mark_read": True,
    }


def test_gateway_local_connect_infers_home_space_from_agent_rows(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": None,
            "username": "madtank",
        }
    )
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "demo-hermes",
            "agent_id": "agent-existing",
            "space_id": "space-from-row",
            "runtime_type": "hermes_sentinel",
        }
    ]
    gateway_core.save_gateway_registry(registry)
    monkeypatch.setattr(gateway_cmd, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(gateway_cmd, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway_cmd,
        "_create_agent_in_space",
        lambda *args, **kwargs: {"id": "agent-local-2", "name": "codex-local"},
    )
    monkeypatch.setattr(gateway_cmd, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_cmd, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))
    fingerprint = {
        "agent_name": "codex-local",
        "pid": 999999,
        "cwd": str(tmp_path),
        "exe_path": sys.executable,
        "user": "madtank",
    }

    payload = gateway_cmd._connect_local_pass_through_agent(agent_name="codex-local", fingerprint=fingerprint)

    assert payload["status"] == "pending"
    stored = gateway_core.load_gateway_registry()
    entry = gateway_core.find_agent_entry(stored, "codex-local")
    assert entry["space_id"] == "space-from-row"


def test_gateway_local_connect_rejects_registry_ref_for_managed_runtime(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "demo-hermes",
            "agent_id": "agent-hermes-1",
            "space_id": "space-1",
            "template_id": "hermes",
            "runtime_type": "hermes_sentinel",
            "install_id": "install-hermes-1",
        }
    ]
    gateway_core.save_gateway_registry(registry)
    fingerprint = {
        "agent_name": "codex-local",
        "pid": 999999,
        "cwd": str(tmp_path),
        "exe_path": sys.executable,
        "user": "madtank",
    }

    with pytest.raises(ValueError, match="registry_ref_not_attachable"):
        gateway_cmd._connect_local_pass_through_agent(registry_ref="#1", fingerprint=fingerprint)


def test_find_agent_entry_by_ref_matches_row_and_stable_prefix():
    registry = {
        "agents": [
            {
                "name": "demo-hermes",
                "install_id": "install-hermes-abcdef",
                "agent_id": "agent-hermes-1",
            },
            {
                "name": "codex-pass-through",
                "install_id": "install-pass-123456",
                "agent_id": "agent-pass-1",
            },
        ]
    }

    assert gateway_core.find_agent_entry_by_ref(registry, "#2")["name"] == "codex-pass-through"
    assert gateway_core.find_agent_entry_by_ref(registry, "1")["name"] == "demo-hermes"
    assert gateway_core.find_agent_entry_by_ref(registry, "codex-pass-through")["agent_id"] == "agent-pass-1"
    assert gateway_core.find_agent_entry_by_ref(registry, "install-pass")["name"] == "codex-pass-through"
    assert gateway_core.find_agent_entry_by_ref(registry, "missing") is None


def test_gateway_reconcile_does_not_auto_approve_pass_through(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "codex-pass",
            "agent_id": "agent-pass-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "template_id": "pass_through",
            "runtime_type": "inbox",
            "desired_state": "running",
            "requires_approval": True,
            "install_id": "install-pass-1",
        }
    ]

    daemon = gateway_core.GatewayDaemon(client_factory=lambda **kwargs: _SharedRuntimeClient({}))
    reconciled = daemon._reconcile_registry(registry, {"token": "axp_u_test.token", "base_url": "https://paxai.app"})
    agent = reconciled["agents"][0]

    assert reconciled["bindings"] == []
    assert agent["approval_state"] == "pending"
    assert agent["approval_id"]
    assert reconciled["approvals"][0]["approval_kind"] == "new_binding"


def test_gateway_daemon_reconcile_normalizes_legacy_inbox_metadata(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    entry = {
        "name": "dev_channel_alpha",
        "agent_id": "agent-dev-channel-1",
        "space_id": "space-1",
        "base_url": "https://paxai.app",
        "runtime_type": "inbox",
        "desired_state": "stopped",
        "placement": "hosted",
        "activation": "persistent",
        "reply_mode": "interactive",
        "telemetry_level": "basic",
        "asset_class": "interactive_agent",
        "intake_model": "live_listener",
        "trigger_sources": ["direct_message"],
        "return_paths": ["inline_reply"],
        "tags": ["local", "custom-bridge"],
        "capabilities": ["reply"],
        "created_via": "legacy_registry",
    }
    gateway_core.ensure_local_asset_binding(registry, entry, created_via="legacy_registry", auto_approve=True)
    registry["agents"] = [entry]

    daemon = gateway_core.GatewayDaemon(client_factory=lambda **kwargs: _SharedRuntimeClient({}))
    reconciled = daemon._reconcile_registry(registry, {"token": "axp_u_test.token"})
    agent = reconciled["agents"][0]

    assert agent["placement"] == "mailbox"
    assert agent["activation"] == "queue_worker"
    assert agent["reply_mode"] == "summary_only"
    assert agent["mode"] == "INBOX"
    assert agent["reply"] == "SUMMARY"
    assert agent["asset_class"] == "background_worker"
    assert agent["intake_model"] == "queue_accept"
    assert agent["worker_model"] == "queue_drain"
    assert agent["return_paths"] == ["summary_post"]
    assert agent["asset_type_label"] == "Inbox Worker"
    assert agent["output_label"] == "Summary"


def test_annotate_runtime_health_respects_explicit_user_overrides():
    snapshot = {
        "name": "custom-inbox-ish",
        "agent_id": "agent-custom-1",
        "runtime_type": "inbox",
        "placement": "hosted",
        "activation": "persistent",
        "reply_mode": "interactive",
        "asset_class": "interactive_agent",
        "intake_model": "live_listener",
        "trigger_sources": ["direct_message"],
        "return_paths": ["inline_reply"],
        "user_overrides": {
            "operator": {
                "placement": "hosted",
                "activation": "persistent",
                "reply_mode": "interactive",
            },
            "asset": {
                "asset_class": "interactive_agent",
                "intake_model": "live_listener",
                "trigger_sources": ["direct_message"],
                "return_paths": ["inline_reply"],
            },
        },
        "effective_state": "stopped",
    }

    annotated = gateway_core.annotate_runtime_health(snapshot)

    assert annotated["placement"] == "hosted"
    assert annotated["activation"] == "persistent"
    assert annotated["reply_mode"] == "interactive"
    assert annotated["mode"] == "LIVE"
    assert annotated["reply"] == "REPLY"
    assert annotated["asset_class"] == "interactive_agent"
    assert annotated["intake_model"] == "live_listener"
    assert annotated["return_paths"] == ["inline_reply"]
    assert annotated["asset_type_label"] == "Live Listener"


def test_evaluate_runtime_attestation_detects_binding_drift_and_creates_approval(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    entry = {
        "name": "docs-worker",
        "agent_id": "agent-docs-1",
        "runtime_type": "exec",
        "exec_command": "python3 worker.py",
        "workdir": str(tmp_path / "repo-a"),
        "created_via": "cli",
    }
    gateway_core.ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)

    drifted = dict(entry)
    drifted["workdir"] = str(tmp_path / "repo-b")

    attestation = gateway_core.evaluate_runtime_attestation(registry, drifted)
    snapshot = gateway_core.annotate_runtime_health({**drifted, **attestation, "effective_state": "stopped"})

    assert attestation["attestation_state"] == "drifted"
    assert attestation["approval_state"] == "pending"
    assert attestation["approval_id"]
    assert registry["approvals"][0]["approval_kind"] == "binding_drift"
    assert snapshot["presence"] == "BLOCKED"
    assert snapshot["confidence"] == "BLOCKED"
    assert snapshot["confidence_reason"] == "binding_drift"


def test_evaluate_runtime_attestation_blocks_asset_mismatch(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    entry = {
        "name": "codex",
        "agent_id": "agent-codex-1",
        "runtime_type": "exec",
        "exec_command": "python3 codex_bridge.py",
        "workdir": str(tmp_path / "repo"),
        "install_id": "install-1",
        "created_via": "cli",
    }
    gateway_core.ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)

    mismatched = dict(entry)
    mismatched["agent_id"] = "agent-other-2"

    attestation = gateway_core.evaluate_runtime_attestation(registry, mismatched)
    snapshot = gateway_core.annotate_runtime_health({**mismatched, **attestation, "effective_state": "stopped"})

    assert attestation["attestation_state"] == "blocked"
    assert attestation["confidence_reason"] == "asset_mismatch"
    assert snapshot["presence"] == "BLOCKED"
    assert snapshot["confidence"] == "BLOCKED"


def test_gateway_daemon_reconcile_blocks_drifted_runtime(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    entry = {
        "name": "drift-bot",
        "agent_id": "agent-drift-1",
        "space_id": "space-1",
        "base_url": "https://paxai.app",
        "runtime_type": "exec",
        "exec_command": "python3 drift.py",
        "workdir": str(tmp_path / "repo-a"),
        "token_file": str(tmp_path / "token"),
        "desired_state": "running",
        "created_via": "cli",
    }
    Path(entry["token_file"]).write_text("axp_a_agent.secret")
    gateway_core.ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)
    entry["workdir"] = str(tmp_path / "repo-b")
    registry["agents"] = [entry]

    daemon = gateway_core.GatewayDaemon(client_factory=lambda **kwargs: _SharedRuntimeClient({}))
    reconciled = daemon._reconcile_registry(registry, {"token": "axp_u_test.token"})
    agent = reconciled["agents"][0]

    assert agent["attestation_state"] == "drifted"
    assert agent["approval_state"] == "pending"
    assert agent["presence"] == "BLOCKED"
    assert "drift-bot" not in daemon._runtimes


def test_gateway_daemon_reconcile_blocks_hermes_without_repo(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    workdir = tmp_path / "workspace" / "ax-cli"
    workdir.mkdir(parents=True)
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("HERMES_REPO_PATH", raising=False)
    monkeypatch.setattr(gateway_core.Path, "home", classmethod(lambda cls: tmp_path / "home"))

    registry = gateway_core.load_gateway_registry()
    entry = {
        "name": "hermes-2",
        "agent_id": "agent-hermes-2",
        "space_id": "space-1",
        "base_url": "https://paxai.app",
        "template_id": "hermes",
        "runtime_type": "exec",
        "exec_command": "python3 examples/hermes_sentinel/hermes_bridge.py",
        "workdir": str(workdir),
        "token_file": str(tmp_path / "token"),
        "desired_state": "running",
        "created_via": "cli",
    }
    Path(entry["token_file"]).write_text("axp_a_agent.secret")
    gateway_core.ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)
    registry["agents"] = [entry]

    daemon = gateway_core.GatewayDaemon(client_factory=lambda **kwargs: _SharedRuntimeClient({}))
    reconciled = daemon._reconcile_registry(registry, {"token": "axp_u_test.token", "base_url": "https://paxai.app"})
    agent = reconciled["agents"][0]

    assert daemon._runtimes == {}
    assert agent["effective_state"] == "error"
    assert agent["presence"] == "ERROR"
    assert agent["confidence"] == "BLOCKED"
    assert agent["confidence_reason"] == "setup_blocked"
    assert "Hermes checkout not found" in str(agent["last_error"])
    assert "Hermes checkout not found" in str(agent["confidence_detail"])


def test_gateway_daemon_rebinds_running_runtime_when_space_changes(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    started: list[str] = []
    stopped: list[str] = []

    class FakeRuntime:
        def __init__(self, entry, **kwargs):
            self.entry = dict(entry)
            self.name = str(entry.get("name"))
            self.started = False

        def start(self):
            self.started = True
            started.append(str(self.entry.get("space_id")))

        def stop(self):
            stopped.append(str(self.entry.get("space_id")))
            self.started = False

        def snapshot(self):
            return {
                "effective_state": "running" if self.started else "stopped",
                "runtime_instance_id": f"runtime-{self.entry.get('space_id')}",
                "last_error": None,
                "current_status": None,
                "current_activity": None,
                "current_tool": None,
                "current_tool_call_id": None,
                "backlog_depth": 0,
            }

    monkeypatch.setattr(gateway_core, "ManagedAgentRuntime", FakeRuntime)
    entry = {
        "name": "space-bot",
        "agent_id": "agent-space-1",
        "space_id": "space-1",
        "base_url": "https://paxai.app",
        "runtime_type": "echo",
        "desired_state": "running",
        "attestation_state": "verified",
        "approval_state": "approved",
        "identity_status": "verified",
        "environment_status": "environment_allowed",
        "space_status": "active_allowed",
    }
    daemon = gateway_core.GatewayDaemon(client_factory=lambda **kwargs: _SharedRuntimeClient({}))

    daemon._reconcile_runtime(entry)
    assert started == ["space-1"]
    assert stopped == []

    moved = {**entry, "space_id": "space-2"}
    daemon._reconcile_runtime(moved)

    assert stopped == ["space-1"]
    assert started == ["space-1", "space-2"]
    assert daemon._runtimes["space-bot"].snapshot()["runtime_instance_id"] == "runtime-space-2"
    events = gateway_core.load_recent_gateway_activity()
    assert any(row["event"] == "runtime_rebinding" and row.get("new_space_id") == "space-2" for row in events)


def test_gateway_approvals_approve_updates_binding(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    entry = {
        "name": "approve-bot",
        "agent_id": "agent-approve-1",
        "runtime_type": "exec",
        "exec_command": "python3 worker.py",
        "workdir": str(tmp_path / "repo-a"),
        "created_via": "cli",
    }
    gateway_core.ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)
    drifted = dict(entry)
    drifted["workdir"] = str(tmp_path / "repo-b")
    attestation = gateway_core.evaluate_runtime_attestation(registry, drifted)
    gateway_core.save_gateway_registry(registry)

    result = runner.invoke(
        app, ["gateway", "approvals", "approve", attestation["approval_id"], "--scope", "gateway", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["approval"]["status"] == "approved"
    assert payload["approval"]["decision_scope"] == "gateway"
    stored = gateway_core.load_gateway_registry()
    binding = gateway_core.find_binding(stored, install_id=entry["install_id"])
    assert binding is not None
    assert binding["path"] == str(Path(drifted["workdir"]).expanduser())
    assert binding["approval_scope"] == "gateway"


def test_gateway_approvals_deny_marks_request_rejected(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    entry = {
        "name": "deny-bot",
        "agent_id": "agent-deny-1",
        "runtime_type": "exec",
        "exec_command": "python3 worker.py",
        "workdir": str(tmp_path / "repo-a"),
        "created_via": "cli",
    }
    gateway_core.ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)
    drifted = dict(entry)
    drifted["workdir"] = str(tmp_path / "repo-b")
    attestation = gateway_core.evaluate_runtime_attestation(registry, drifted)
    gateway_core.save_gateway_registry(registry)

    result = runner.invoke(app, ["gateway", "approvals", "deny", attestation["approval_id"], "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["approval"]["status"] == "rejected"
    stored = gateway_core.load_gateway_registry()
    approval = next(item for item in stored["approvals"] if item["approval_id"] == attestation["approval_id"])
    assert approval["status"] == "rejected"


def test_sanitize_exec_env_strips_ax_credentials(monkeypatch):
    monkeypatch.setenv("AX_TOKEN", "secret-token")
    monkeypatch.setenv("AX_USER_TOKEN", "secret-user")
    monkeypatch.setenv("AX_BASE_URL", "https://paxai.app")
    monkeypatch.setenv("AX_AGENT_NAME", "orion")
    monkeypatch.setenv("OPENAI_API_KEY", "keep-me")

    env = gateway_core.sanitize_exec_env("hello", {"name": "echo-bot", "agent_id": "agent-1", "runtime_type": "exec"})

    assert "AX_TOKEN" not in env
    assert "AX_USER_TOKEN" not in env
    assert "AX_BASE_URL" not in env
    assert env["AX_AGENT_NAME"] == "echo-bot"
    assert env["AX_MENTION_CONTENT"] == "hello"
    assert env["AX_GATEWAY_AGENT_NAME"] == "echo-bot"
    assert env["OPENAI_API_KEY"] == "keep-me"


def test_sanitize_exec_env_passes_managed_agent_context(tmp_path):
    token_file = tmp_path / "agent.token"
    token_file.write_text("axp_a_agent.secret")

    env = gateway_core.sanitize_exec_env(
        "remember this",
        {
            "name": "ollama-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "exec",
            "token_file": str(token_file),
        },
    )

    assert env["AX_TOKEN_FILE"] == str(token_file)
    assert "AX_TOKEN" not in env
    assert env["AX_BASE_URL"] == "https://paxai.app"
    assert env["AX_SPACE_ID"] == "space-1"
    assert env["AX_AGENT_ID"] == "agent-1"
    assert env["AX_AGENT_NAME"] == "ollama-bot"
    assert env["AX_GATEWAY_AGENT_ID"] == "agent-1"
    assert env["AX_GATEWAY_AGENT_NAME"] == "ollama-bot"


def test_gateway_managed_token_loader_rejects_user_bootstrap_pat(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("axp_u_user.secret")

    with pytest.raises(ValueError, match="agent-bound token"):
        gateway_core.load_gateway_managed_agent_token(
            {
                "name": "echo-bot",
                "agent_id": "agent-1",
                "token_file": str(token_file),
            }
        )


def test_gateway_managed_token_loader_requires_bound_agent_id(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")

    with pytest.raises(ValueError, match="bound agent_id"):
        gateway_core.load_gateway_managed_agent_token(
            {
                "name": "echo-bot",
                "token_file": str(token_file),
            }
        )


def test_hermes_sentinel_env_rejects_user_bootstrap_pat(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("axp_u_user.secret")

    with pytest.raises(ValueError, match="agent-bound token"):
        gateway_core._build_hermes_sentinel_env(
            {
                "name": "dev_sentinel",
                "agent_id": "agent-1",
                "space_id": "space-1",
                "base_url": "https://paxai.app",
                "runtime_type": "hermes_sentinel",
                "token_file": str(token_file),
                "workdir": str(tmp_path / "dev_sentinel"),
            }
        )


def test_managed_echo_runtime_processes_message(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")
    payload = {
        "id": "msg-1",
        "content": "@echo-bot ping",
        "author": {"id": "user-1", "name": "madtank", "type": "user"},
        "mentions": ["echo-bot"],
    }
    shared = _SharedRuntimeClient(payload)

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "echo-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "token_file": str(token_file),
        },
        client_factory=lambda **kwargs: shared,
    )

    runtime.start()
    deadline = time.time() + 2.0
    while time.time() < deadline and not shared.sent:
        time.sleep(0.05)
    runtime.stop()

    assert shared.sent, "echo runtime should have replied"
    assert shared.sent[0]["content"] == "Echo: ping"
    assert shared.sent[0]["parent_id"] == "msg-1"
    assert shared.sent[0]["agent_id"] == "agent-1"
    assert shared.sent[0]["metadata"]["control_plane"] == "gateway"
    assert shared.sent[0]["metadata"]["gateway"]["managed"] is True
    assert shared.sent[0]["metadata"]["gateway"]["agent_name"] == "echo-bot"
    assert [row["status"] for row in shared.processing] == ["started", "processing", "completed"]
    assert shared.processing[0]["activity"] == "Picked up by Gateway"
    assert shared.processing[0]["detail"] == {"backlog_depth": 1, "pickup_state": "claimed"}
    assert shared.processing[1]["activity"] == "Composing echo reply"
    recent = gateway_core.load_recent_gateway_activity()
    event_names = [row["event"] for row in recent]
    assert "message_received" in event_names
    assert "message_claimed" in event_names
    assert "reply_sent" in event_names


def test_managed_exec_runtime_parses_gateway_progress_events(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")
    script = tmp_path / "bridge.py"
    script.write_text(
        """
import json
import sys

prefix = "AX_GATEWAY_EVENT "
print(prefix + json.dumps({"kind": "status", "status": "working", "message": "warming up"}), flush=True)
print(prefix + json.dumps({"kind": "status", "status": "working", "message": "warming up", "progress": {"current": 1, "total": 3, "unit": "steps"}}), flush=True)
print(prefix + json.dumps({"kind": "tool_start", "tool_name": "sleep", "tool_call_id": "tool-1", "arguments": {"seconds": 1}}), flush=True)
print(prefix + json.dumps({"kind": "tool_result", "tool_name": "sleep", "tool_call_id": "tool-1", "arguments": {"seconds": 1}, "initial_data": {"slept_seconds": 1}, "status": "success"}), flush=True)
print("done", flush=True)
""".strip()
    )
    payload = {
        "id": "msg-1",
        "content": "@exec-bot pause 1s",
        "author": {"id": "user-1", "name": "madtank", "type": "user"},
        "mentions": ["exec-bot"],
    }
    shared = _SharedRuntimeClient(payload)

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "exec-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "exec",
            "exec_command": f"{sys.executable} {script}",
            "token_file": str(token_file),
        },
        client_factory=lambda **kwargs: shared,
    )

    runtime.start()
    deadline = time.time() + 3.0
    while time.time() < deadline and not shared.sent:
        time.sleep(0.05)
    snapshot = runtime.snapshot()
    runtime.stop()

    assert shared.sent, "exec runtime should have replied"
    assert shared.sent[0]["content"] == "done"
    assert [row["status"] for row in shared.processing] == [
        "started",
        "processing",
        "working",
        "working",
        "tool_call",
        "tool_complete",
        "completed",
    ]
    assert shared.processing[0]["activity"] == "Picked up by Gateway"
    assert shared.processing[0]["detail"] == {"backlog_depth": 1, "pickup_state": "claimed"}
    assert shared.processing[1]["activity"] == "Preparing runtime"
    assert shared.processing[2]["activity"] == "warming up"
    assert shared.processing[3]["activity"] == "warming up"
    assert shared.processing[3]["progress"] == {"current": 1, "total": 3, "unit": "steps"}
    assert shared.processing[4]["tool_name"] == "sleep"
    assert shared.processing[4]["activity"] == "Using sleep"
    assert shared.processing[5]["tool_name"] == "sleep"
    assert shared.processing[5]["detail"] == {"slept_seconds": 1}
    assert shared.tool_calls
    assert shared.tool_calls[0]["tool_name"] == "sleep"
    assert shared.tool_calls[0]["message_id"] == "msg-1"
    assert snapshot["current_activity"] in {None, "warming up"}
    recent = gateway_core.load_recent_gateway_activity()
    events = [row["event"] for row in recent]
    assert "message_claimed" in events
    assert "tool_started" in events
    assert "tool_finished" in events


def test_managed_exec_runtime_marks_message_timed_out(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")
    script = tmp_path / "slow_bridge.py"
    script.write_text(
        """
import time

time.sleep(5)
print("too late", flush=True)
""".strip()
    )
    payload = {
        "id": "msg-1",
        "content": "@exec-bot run slow job",
        "author": {"id": "user-1", "name": "madtank", "type": "user"},
        "mentions": ["exec-bot"],
    }
    shared = _SharedRuntimeClient(payload)

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "exec-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "exec",
            "exec_command": f"{sys.executable} {script}",
            "timeout_seconds": 1,
            "token_file": str(token_file),
        },
        client_factory=lambda **kwargs: shared,
    )

    runtime.start()
    deadline = time.time() + 4.0
    while time.time() < deadline and not any(row.get("reason") == "runtime_timeout" for row in shared.processing):
        time.sleep(0.05)
    snapshot = runtime.snapshot()
    runtime.stop()

    assert not shared.sent
    assert [row["status"] for row in shared.processing] == ["started", "processing", "error"]
    timeout_status = shared.processing[-1]
    assert timeout_status["activity"] == "Timed out after 1s"
    assert timeout_status["reason"] == "runtime_timeout"
    assert timeout_status["detail"] == {"timeout_seconds": 1, "runtime_type": "exec"}
    assert "timed out after 1s" in timeout_status["error_message"]
    assert snapshot["current_status"] == "error"
    assert snapshot["current_activity"] == "Timed out after 1s"
    recent = gateway_core.load_recent_gateway_activity()
    events = [row["event"] for row in recent]
    assert "runtime_timeout" in events
    assert "reply_sent" not in events


def test_managed_sentinel_cli_runtime_resumes_agent_session(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")
    popen_calls = []

    class _FakePipe:
        def __init__(self, lines=None):
            self.lines = list(lines or [])
            self.writes = []

        def __iter__(self):
            return iter(self.lines)

        def write(self, text):
            self.writes.append(text)

        def read(self):
            return ""

        def close(self):
            return None

    class _FakeProcess:
        def __init__(self, cmd, **kwargs):
            popen_calls.append(cmd)
            self.stdin = _FakePipe()
            self.stderr = _FakePipe()
            self.returncode = 0
            if len(popen_calls) == 1:
                self.stdout = _FakePipe(
                    [
                        json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                        json.dumps(
                            {
                                "type": "item.started",
                                "item": {"type": "command_execution", "id": "tool-1", "command": "pwd"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "command_execution",
                                    "id": "tool-1",
                                    "command": "pwd",
                                    "exit_code": 0,
                                    "aggregated_output": "/tmp",
                                },
                            }
                        ),
                        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "remembered"}}),
                    ]
                )
            else:
                self.stdout = _FakePipe(
                    [
                        json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "cobalt"}}),
                    ]
                )

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(gateway_core.subprocess, "Popen", lambda cmd, **kwargs: _FakeProcess(cmd, **kwargs))
    shared = _SharedRuntimeClient({})
    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "dev_sentinel",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "sentinel_cli",
            "sentinel_runtime": "codex",
            "workdir": str(tmp_path),
            "token_file": str(token_file),
        },
        client_factory=lambda **kwargs: shared,
    )
    runtime._send_client = shared

    first = runtime._handle_prompt("remember cobalt", message_id="msg-1", data={"id": "msg-1"})
    second = runtime._handle_prompt("what word?", message_id="msg-2", data={"id": "msg-2"})

    assert first == "remembered"
    assert second == "cobalt"
    assert "resume" not in popen_calls[0]
    assert "resume" in popen_calls[1]
    assert "thread-1" in popen_calls[1]
    assert [row["status"] for row in shared.processing] == [
        "thinking",
        "tool_call",
        "tool_complete",
        "thinking",
    ]
    assert shared.tool_calls[0]["tool_name"] == "shell"
    assert shared.tool_calls[0]["message_id"] == "msg-1"


def test_managed_hermes_sentinel_runtime_supervises_long_running_listener(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")
    workdir = tmp_path / "agents" / "dev_sentinel"
    workdir.mkdir(parents=True)
    script = tmp_path / "agents" / "claude_agent_v2.py"
    observed = tmp_path / "observed.json"
    monkeypatch.setenv("TEST_HERMES_SENTINEL_OBSERVED", str(observed))
    script.write_text(
        """
import json
import os
import time

path = os.environ["TEST_HERMES_SENTINEL_OBSERVED"]
with open(path, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "AX_TOKEN": os.environ.get("AX_TOKEN"),
            "AX_BASE_URL": os.environ.get("AX_BASE_URL"),
            "AX_AGENT_NAME": os.environ.get("AX_AGENT_NAME"),
            "AX_AGENT_ID": os.environ.get("AX_AGENT_ID"),
            "AX_SPACE_ID": os.environ.get("AX_SPACE_ID"),
            "AX_CONFIG_DIR": os.environ.get("AX_CONFIG_DIR"),
        },
        handle,
    )
while True:
    time.sleep(1)
""".strip()
    )
    hermes_repo = tmp_path / "hermes-agent"
    hermes_repo.mkdir()

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "dev_sentinel",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://dev.paxai.app",
            "runtime_type": "hermes_sentinel",
            "template_id": "hermes",
            "workdir": str(workdir),
            "token_file": str(token_file),
            "hermes_repo_path": str(hermes_repo),
            "hermes_python": sys.executable,
            "log_path": str(tmp_path / "hermes.log"),
        }
    )

    runtime.start()
    deadline = time.time() + 3.0
    while time.time() < deadline and not observed.exists():
        time.sleep(0.05)
    snapshot = runtime.snapshot()
    runtime.stop()

    assert observed.exists()
    env = json.loads(observed.read_text())
    assert env["AX_TOKEN"] == "axp_a_agent.secret"
    assert env["AX_BASE_URL"] == "https://dev.paxai.app"
    assert env["AX_AGENT_NAME"] == "dev_sentinel"
    assert env["AX_AGENT_ID"] == "agent-1"
    assert env["AX_SPACE_ID"] == "space-1"
    assert env["AX_CONFIG_DIR"] == str(workdir / ".ax")
    assert snapshot["effective_state"] == "running"
    assert snapshot["current_activity"] == "Hermes sentinel listener running"


def test_managed_inbox_runtime_queues_message_without_reply(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")
    payload = {
        "id": "msg-1",
        "content": "@inbox-bot hello there",
        "author": {"id": "user-1", "name": "madtank", "type": "user"},
        "mentions": ["inbox-bot"],
    }
    shared = _SharedRuntimeClient(payload)

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "inbox-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "inbox",
            "token_file": str(token_file),
        },
        client_factory=lambda **kwargs: shared,
    )

    runtime.start()
    deadline = time.time() + 2.0
    snapshot = runtime.snapshot()
    while time.time() < deadline and snapshot["backlog_depth"] < 1:
        time.sleep(0.05)
        snapshot = runtime.snapshot()
    runtime.stop()

    assert not shared.sent
    assert snapshot["backlog_depth"] >= 1
    assert [row["status"] for row in shared.processing] == ["queued"]
    assert shared.processing[0]["activity"] == "Queued in Gateway"
    assert shared.processing[0]["detail"] == {"backlog_depth": 1, "pickup_state": "queued"}
    pending = gateway_core.load_agent_pending_messages("inbox-bot")
    assert pending == [
        {
            "message_id": "msg-1",
            "parent_id": None,
            "conversation_id": None,
            "content": "@inbox-bot hello there",
            "display_name": None,
            "created_at": pending[0]["created_at"],
            "queued_at": pending[0]["queued_at"],
        }
    ]
    assert snapshot["last_work_received_at"] == pending[0]["queued_at"]
    recent = gateway_core.load_recent_gateway_activity()
    events = [row["event"] for row in recent]
    assert "message_received" in events
    assert "message_queued" in events


def test_passive_runtime_snapshot_rehydrates_manual_queue_updates(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "inbox-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "inbox",
            "token_file": str(token_file),
            "backlog_depth": 0,
            "current_status": None,
            "current_activity": None,
            "processed_count": 1,
            "last_reply_message_id": "reply-1",
            "last_reply_preview": "handled",
        }
    ]
    gateway_core.save_gateway_registry(registry)
    gateway_core.save_agent_pending_messages("inbox-bot", [])

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "inbox-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "inbox",
            "token_file": str(token_file),
        },
        client_factory=lambda **kwargs: _SharedRuntimeClient({}),
    )
    runtime._update_state(backlog_depth=1, current_status="queued", current_activity="Queued in Gateway")

    snapshot = runtime.snapshot()

    assert snapshot["backlog_depth"] == 0
    assert snapshot["current_status"] is None
    assert snapshot["current_activity"] is None
    assert snapshot["processed_count"] == 1
    assert snapshot["last_reply_message_id"] == "reply-1"
    assert snapshot["last_reply_preview"] == "handled"


def test_annotate_runtime_health_marks_stale_after_missed_heartbeat():
    old_seen = (
        datetime.now(timezone.utc) - timedelta(seconds=gateway_core.RUNTIME_STALE_AFTER_SECONDS + 5)
    ).isoformat()

    snapshot = gateway_core.annotate_runtime_health(
        {
            "effective_state": "running",
            "last_seen_at": old_seen,
        }
    )

    assert snapshot["effective_state"] == "stale"
    assert snapshot["connected"] is False
    assert snapshot["last_seen_age_seconds"] >= gateway_core.RUNTIME_STALE_AFTER_SECONDS


def test_annotate_runtime_health_derives_identity_space_snapshot(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "space_name": "ax-cli-dev",
            "username": "codex",
        }
    )
    token_file = tmp_path / "identity.token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "identity-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "credential_source": "gateway",
            "token_file": str(token_file),
            "desired_state": "running",
            "effective_state": "running",
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "install_id": "inst-identity-1",
        }
    ]
    gateway_core.ensure_gateway_identity_binding(
        registry, registry["agents"][0], session=gateway_core.load_gateway_session()
    )

    snapshot = gateway_core.annotate_runtime_health(registry["agents"][0], registry=registry)

    assert snapshot["acting_agent_name"] == "identity-bot"
    assert snapshot["environment_label"] == "prod"
    assert snapshot["environment_status"] == "environment_allowed"
    assert snapshot["active_space_id"] == "space-1"
    assert snapshot["active_space_source"] == "gateway_binding"
    assert snapshot["space_status"] == "active_allowed"
    assert snapshot["identity_status"] == "verified"
    assert snapshot["confidence"] == "HIGH"


def test_annotate_runtime_health_blocks_environment_mismatch(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "identity.token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "identity-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "credential_source": "gateway",
            "token_file": str(token_file),
            "desired_state": "running",
            "effective_state": "running",
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "install_id": "inst-identity-1",
        }
    ]
    gateway_core.ensure_gateway_identity_binding(registry, registry["agents"][0])

    snapshot = gateway_core.annotate_runtime_health(
        {**registry["agents"][0], "base_url": "https://dev.paxai.app"},
        registry=registry,
    )

    assert snapshot["environment_status"] == "environment_mismatch"
    assert snapshot["presence"] == "BLOCKED"
    assert snapshot["confidence"] == "BLOCKED"
    assert snapshot["confidence_reason"] == "environment_mismatch"


@pytest.mark.parametrize(
    ("input_snapshot", "expected"),
    [
        (
            {
                "template_id": "claude_code_channel",
                "placement": "attached",
                "activation": "attach_only",
                "reply_mode": "interactive",
                "effective_state": "stale",
            },
            {
                "mode": "LIVE",
                "presence": "STALE",
                "reply": "REPLY",
                "confidence": "LOW",
                "reachability": "attach_required",
            },
        ),
        (
            {
                "placement": "hosted",
                "activation": "persistent",
                "reply_mode": "interactive",
                "effective_state": "stopped",
            },
            {
                "mode": "LIVE",
                "presence": "OFFLINE",
                "reply": "REPLY",
                "confidence": "LOW",
                "reachability": "unavailable",
            },
        ),
        (
            {
                "runtime_type": "inbox",
                "placement": "mailbox",
                "activation": "queue_worker",
                "reply_mode": "summary_only",
                "effective_state": "running",
                "last_seen_at": "__recent__",
                "backlog_depth": 0,
            },
            {
                "mode": "INBOX",
                "presence": "IDLE",
                "reply": "SUMMARY",
                "confidence": "HIGH",
                "reachability": "queue_available",
            },
        ),
        (
            {
                "runtime_type": "inbox",
                "placement": "mailbox",
                "activation": "queue_worker",
                "reply_mode": "summary_only",
                "effective_state": "running",
                "last_seen_at": "__recent__",
                "backlog_depth": 3,
                "last_doctor_result": {
                    "status": "failed",
                    "summary": "Queue writable but worker smoke test failed.",
                    "checks": [{"name": "test_claim", "status": "failed"}],
                },
            },
            {
                "mode": "INBOX",
                "presence": "QUEUED",
                "reply": "SUMMARY",
                "confidence": "LOW",
                "reachability": "queue_available",
            },
        ),
        (
            {
                "template_id": "ollama",
                "placement": "hosted",
                "activation": "on_demand",
                "reply_mode": "interactive",
                "effective_state": "stopped",
            },
            {
                "mode": "ON-DEMAND",
                "presence": "IDLE",
                "reply": "REPLY",
                "confidence": "MEDIUM",
                "reachability": "launch_available",
            },
        ),
        (
            {
                "template_id": "hermes",
                "effective_state": "error",
                "reply_mode": "interactive",
                "last_error": "missing repo",
            },
            {
                "mode": "LIVE",
                "presence": "ERROR",
                "reply": "REPLY",
                "confidence": "BLOCKED",
                "reachability": "unavailable",
            },
        ),
    ],
)
def test_annotate_runtime_health_derives_gateway_operator_model(input_snapshot, expected):
    input_snapshot = dict(input_snapshot)
    if input_snapshot.get("last_seen_at") == "__recent__":
        input_snapshot["last_seen_at"] = datetime.now(timezone.utc).isoformat()
    snapshot = gateway_core.annotate_runtime_health(input_snapshot)

    assert snapshot["mode"] == expected["mode"]
    assert snapshot["presence"] == expected["presence"]
    assert snapshot["reply"] == expected["reply"]
    assert snapshot["confidence"] == expected["confidence"]
    assert snapshot["reachability"] == expected["reachability"]


def test_annotate_runtime_health_prefers_doctor_summary_for_setup_error_detail():
    snapshot = gateway_core.annotate_runtime_health(
        {
            "template_id": "hermes",
            "effective_state": "error",
            "last_reply_preview": "(stderr: ERROR: hermes-agent repo not found at /Users/jacob/hermes-agent. Set HERMES_REPO_PATH or clone hermes-agent.)",
            "last_doctor_result": {
                "status": "failed",
                "summary": "Hermes checkout not found at /Users/jacob/hermes-agent.",
                "checks": [{"name": "hermes_repo", "status": "failed"}],
            },
        }
    )

    assert snapshot["confidence"] == "BLOCKED"
    assert snapshot["confidence_reason"] == "setup_blocked"
    assert snapshot["confidence_detail"] == "Hermes checkout not found at /Users/jacob/hermes-agent."


def test_hermes_setup_status_prefers_sibling_checkout(monkeypatch, tmp_path):
    workdir = tmp_path / "workspace" / "ax-cli"
    sibling = tmp_path / "workspace" / "hermes-agent"
    workdir.mkdir(parents=True)
    sibling.mkdir(parents=True)
    monkeypatch.delenv("HERMES_REPO_PATH", raising=False)
    monkeypatch.setattr(gateway_core.Path, "home", classmethod(lambda cls: tmp_path / "home"))

    status = gateway_core.hermes_setup_status({"template_id": "hermes", "workdir": str(workdir)})

    assert status["ready"] is True
    assert status["resolved_path"] == str(sibling)


def test_sanitize_exec_env_sets_resolved_hermes_repo_path():
    env = gateway_core.sanitize_exec_env(
        "Gateway test OK.",
        {
            "agent_id": "agent-hermes-2",
            "name": "hermes-2",
            "runtime_type": "exec",
            "hermes_repo_path": "/tmp/hermes-agent",
        },
    )

    assert env["HERMES_REPO_PATH"] == "/tmp/hermes-agent"


def test_sanitize_exec_env_sets_ollama_model_override():
    env = gateway_core.sanitize_exec_env(
        "Gateway test OK.",
        {
            "agent_id": "agent-ember-1",
            "name": "ember",
            "runtime_type": "exec",
            "ollama_model": "gemma4:latest",
        },
    )

    assert env["OLLAMA_MODEL"] == "gemma4:latest"


def test_ollama_setup_status_recommends_recent_local_chat_model(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "models": [
                    {
                        "name": "nomic-embed-text:latest",
                        "modified_at": "2026-01-06T21:04:28.576252397-08:00",
                        "details": {"family": "nomic-bert", "families": ["nomic-bert"], "parameter_size": "137M"},
                    },
                    {
                        "name": "nemotron-3-nano:latest",
                        "modified_at": "2025-12-16T14:03:52.946489046-08:00",
                        "details": {
                            "family": "nemotron_h_moe",
                            "families": ["nemotron_h_moe"],
                            "parameter_size": "31.6B",
                        },
                    },
                    {
                        "name": "gemma4:latest",
                        "modified_at": "2026-04-02T19:28:17.519867961-07:00",
                        "details": {"family": "gemma4", "families": ["gemma4"], "parameter_size": "8.0B"},
                    },
                    {
                        "name": "gpt-oss:120b-cloud",
                        "modified_at": "2025-11-11T16:50:56.418111483-08:00",
                        "remote_host": "https://ollama.com:443",
                        "details": {"family": "gptoss", "families": ["gptoss"], "parameter_size": "116.8B"},
                    },
                ]
            }

    monkeypatch.setattr(gateway_core.httpx, "get", lambda *args, **kwargs: _FakeResponse())

    status = gateway_core.ollama_setup_status()

    assert status["server_reachable"] is True
    assert status["recommended_model"] == "gemma4:latest"
    assert status["available_models"] == [
        "nomic-embed-text:latest",
        "nemotron-3-nano:latest",
        "gemma4:latest",
        "gpt-oss:120b-cloud",
    ]
    assert status["local_models"] == [
        "nomic-embed-text:latest",
        "nemotron-3-nano:latest",
        "gemma4:latest",
    ]


@pytest.mark.parametrize(
    ("input_snapshot", "expected"),
    [
        (
            {
                "template_id": "hermes",
                "effective_state": "running",
                "last_seen_at": datetime.now(timezone.utc).isoformat(),
            },
            {
                "asset_class": "interactive_agent",
                "intake_model": "live_listener",
                "asset_type_label": "Live Listener",
                "output_label": "Reply",
                "telemetry_shape": "rich",
            },
        ),
        (
            {
                "template_id": "ollama",
                "effective_state": "stopped",
            },
            {
                "asset_class": "interactive_agent",
                "intake_model": "launch_on_send",
                "asset_type_label": "On-Demand Agent",
                "output_label": "Reply",
                "telemetry_shape": "basic",
            },
        ),
        (
            {
                "runtime_type": "inbox",
                "template_id": "inbox",
                "effective_state": "running",
                "last_seen_at": datetime.now(timezone.utc).isoformat(),
            },
            {
                "asset_class": "background_worker",
                "intake_model": "queue_accept",
                "asset_type_label": "Inbox Worker",
                "output_label": "Summary",
                "telemetry_shape": "basic",
                "worker_model": "queue_drain",
            },
        ),
    ],
)
def test_annotate_runtime_health_derives_asset_taxonomy_fields(input_snapshot, expected):
    snapshot = gateway_core.annotate_runtime_health(input_snapshot)

    for key, value in expected.items():
        assert snapshot[key] == value
    assert isinstance(snapshot["asset_descriptor"], dict)
    assert snapshot["asset_descriptor"]["asset_class"] == expected["asset_class"]


def test_listener_timeout_enters_reconnecting_state(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")

    class _TimeoutRuntimeClient:
        def __init__(self):
            self.timeout = None

        def connect_sse(self, *, space_id, timeout=None):
            self.timeout = timeout
            raise httpx.ReadTimeout("boom", request=httpx.Request("GET", "https://paxai.app/api/v1/sse/messages"))

        def close(self):
            return None

    shared = _TimeoutRuntimeClient()
    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "echo-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "token_file": str(token_file),
        },
        client_factory=lambda **kwargs: shared,
    )

    runtime.start()
    deadline = time.time() + 1.0
    snapshot = runtime.snapshot()
    while time.time() < deadline and snapshot["effective_state"] != "reconnecting":
        time.sleep(0.05)
        snapshot = runtime.snapshot()
    runtime.stop()

    assert shared.timeout is not None
    assert shared.timeout.read == gateway_core.SSE_IDLE_TIMEOUT_SECONDS
    assert snapshot["effective_state"] == "reconnecting"
    assert snapshot["last_error"] == "idle timeout after 45s without SSE heartbeat"
    recent = gateway_core.load_recent_gateway_activity()
    assert recent[-1]["event"] in {"runtime_stopped", "listener_timeout"}
    assert any(row["event"] == "listener_timeout" for row in recent)


def test_gateway_watch_once_renders_dashboard(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    registry = gateway_core.load_gateway_registry()
    registry["gateway"].update(
        {
            "gateway_id": "gw-12345678",
            "desired_state": "running",
            "effective_state": "running",
            "last_reconcile_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    registry["agents"] = [
        {
            "name": "echo-bot",
            "runtime_type": "echo",
            "desired_state": "running",
            "effective_state": "running",
            "backlog_depth": 2,
            "processed_count": 7,
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "last_reply_preview": "Echo: ping",
        }
    ]
    gateway_core.save_gateway_registry(registry)
    gateway_core.record_gateway_activity("message_received", entry=registry["agents"][0], message_id="msg-1")

    result = runner.invoke(app, ["gateway", "watch", "--once"])

    assert result.exit_code == 0, result.output
    assert "Gateway Overview" in result.output
    assert "Managed Agents" in result.output
    assert "@echo-bot" in result.output
    assert "Recent Activity" in result.output


def test_render_gateway_ui_page_contains_local_dashboard_shell():
    page = gateway_cmd._render_gateway_ui_page(refresh_ms=2000)

    assert "Gateway Control Plane" in page
    assert "Agent Operated" in page
    assert "/api/status" in page
    assert "/api/templates" in page
    assert "/api/agents/&lt;name&gt;" in page
    assert "refreshMs = 2000" in page
    assert "Gateway Agent Setup" in page
    assert "gateway-agent-setup" in page
    assert "Agent Type" in page
    assert "Output" in page
    assert "Advanced launch settings" in page
    assert "Alerts" in page


def test_gateway_templates_command_json():
    result = runner.invoke(app, ["gateway", "templates", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    ids = [item["id"] for item in payload["templates"]]
    assert ids[:6] == ["echo_test", "ollama", "hermes", "pass_through", "sentinel_cli", "claude_code_channel"]
    assert "inbox" not in ids
    assert payload["count"] == 6
    ollama = next(item for item in payload["templates"] if item["id"] == "ollama")
    assert ollama["runtime_type"] == "exec"
    assert ollama["launchable"] is True
    assert ollama["asset_type_label"] == "On-Demand Agent"
    assert ollama["output_label"] == "Reply"
    assert ollama["setup_skill"] == "gateway-agent-setup"
    assert ollama["setup_skill_path"].endswith("skills/gateway-agent-setup/SKILL.md")
    pass_through = next(item for item in payload["templates"] if item["id"] == "pass_through")
    assert pass_through["runtime_type"] == "inbox"
    assert pass_through["requires_approval"] is True
    assert pass_through["intake_model"] == "polling_mailbox"


def test_gateway_template_echo_alias_resolves():
    assert gateway_runtime_types.agent_template_definition("echo")["id"] == "echo_test"


def test_gateway_templates_command_json_includes_ollama_catalog(monkeypatch):
    monkeypatch.setattr(
        gateway_cmd,
        "ollama_setup_status",
        lambda preferred_model=None: {
            "server_reachable": True,
            "recommended_model": "gemma4:latest",
            "available_models": ["gemma4:latest", "nemotron-3-nano:latest"],
            "local_models": ["gemma4:latest", "nemotron-3-nano:latest"],
            "summary": "Ollama is reachable. Recommended model: gemma4:latest.",
        },
    )

    result = runner.invoke(app, ["gateway", "templates", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    ollama = next(item for item in payload["templates"] if item["id"] == "ollama")
    assert ollama["defaults"]["ollama_model"] == "gemma4:latest"
    assert ollama["ollama_recommended_model"] == "gemma4:latest"
    assert ollama["ollama_available_models"] == ["gemma4:latest", "nemotron-3-nano:latest"]


def test_gateway_runtime_types_command_json():
    result = runner.invoke(app, ["gateway", "runtime-types", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    ids = [item["id"] for item in payload["runtime_types"]]
    assert ids == ["echo", "exec", "hermes_sentinel", "sentinel_cli", "inbox"]
    exec_type = next(item for item in payload["runtime_types"] if item["id"] == "exec")
    assert exec_type["signals"]["activity"]
    assert exec_type["examples"]
    hermes_type = next(item for item in payload["runtime_types"] if item["id"] == "hermes_sentinel")
    assert hermes_type["kind"] == "supervised_process"
    sentinel_type = next(item for item in payload["runtime_types"] if item["id"] == "sentinel_cli")
    assert sentinel_type["signals"]["tools"]


def test_gateway_ui_handler_serves_status_and_agent_detail(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(gateway_core, "_scan_gateway_process_pids", lambda: [])
    monkeypatch.setattr(gateway_core, "_scan_gateway_ui_process_pids", lambda: [])
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://dev.paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    registry = gateway_core.load_gateway_registry()
    registry["gateway"].update(
        {
            "gateway_id": "gw-ui-12345678",
            "desired_state": "running",
            "effective_state": "running",
            "last_reconcile_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    registry["agents"] = [
        {
            "name": "echo-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "runtime_type": "echo",
            "desired_state": "running",
            "effective_state": "running",
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "last_reply_preview": "Echo: ping",
            "token_file": "/tmp/echo-token",
            "transport": "gateway",
            "credential_source": "gateway",
        }
    ]
    gateway_core.save_gateway_registry(registry)
    gateway_core.record_gateway_activity("reply_sent", entry=registry["agents"][0], reply_preview="Echo: ping")

    handler = gateway_cmd._build_gateway_ui_handler(activity_limit=5, refresh_ms=1500)
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        host, port = probe.getsockname()
    server = gateway_cmd._GatewayUiServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://{host}:{port}", timeout=2.0) as client:
            status = client.get("/api/status")
            assert status.status_code == 200
            status_payload = status.json()
            assert status_payload["gateway"]["gateway_id"] == "gw-ui-12345678"
            assert status_payload["agents"][0]["name"] == "echo-bot"
            assert status_payload["agents"][0]["mode"] == "LIVE"
            assert status_payload["agents"][0]["presence"] == "IDLE"
            assert status_payload["agents"][0]["reply"] == "REPLY"
            assert status_payload["agents"][0]["confidence"] == "HIGH"
            assert status_payload["summary"]["alert_count"] >= 1
            assert status_payload["alerts"][0]["title"] == "Gateway daemon is stopped"

            runtime_types = client.get("/api/runtime-types")
            assert runtime_types.status_code == 200
            runtime_payload = runtime_types.json()
            assert runtime_payload["count"] == 5
            assert runtime_payload["runtime_types"][1]["id"] == "exec"

            templates = client.get("/api/templates")
            assert templates.status_code == 200
            template_payload = templates.json()
            assert template_payload["templates"][0]["id"] == "echo_test"
            assert template_payload["templates"][5]["launchable"] is False
            assert template_payload["count"] == 6

            detail = client.get("/api/agents/echo-bot")
            assert detail.status_code == 200
            detail_payload = detail.json()
            assert detail_payload["agent"]["name"] == "echo-bot"
            assert detail_payload["recent_activity"][0]["event"] == "reply_sent"

            page = client.get("/")
            assert page.status_code == 200
            assert "Bring your agents" in page.text
            assert "window.__GATEWAY_DEMO_REFRESH_MS__ = 1500" in page.text
            assert 'href="/operator"' in page.text

            operator_page = client.get("/operator")
            assert operator_page.status_code == 200
            assert "Gateway Control Plane" in operator_page.text
            assert "refreshMs = 1500" in operator_page.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_gateway_ui_handler_supports_agent_mutations(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://dev.paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    monkeypatch.setattr(gateway_cmd, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(gateway_cmd, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_cmd, "_create_agent_in_space", _fake_create_agent_in_space)
    monkeypatch.setattr(gateway_cmd, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_cmd, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))
    monkeypatch.setattr(gateway_cmd, "AxClient", _FakeManagedSendClient)

    handler = gateway_cmd._build_gateway_ui_handler(activity_limit=5, refresh_ms=1500)
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        host, port = probe.getsockname()
    server = gateway_cmd._GatewayUiServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://{host}:{port}", timeout=2.0) as client:
            created = client.post(
                "/api/agents",
                json={
                    "name": "ui-bot",
                    "template_id": "echo_test",
                },
            )
            assert created.status_code == 201
            assert created.json()["name"] == "ui-bot"
            assert created.json()["template_label"] == "Echo (Test)"

            updated = client.put(
                "/api/agents/ui-bot",
                json={
                    "template_id": "ollama",
                    "workdir": str(tmp_path),
                    "exec_command": "python3 examples/gateway_ollama/ollama_bridge.py",
                },
            )
            assert updated.status_code == 200
            updated_payload = updated.json()
            assert updated_payload["template_id"] == "ollama"
            assert updated_payload["workdir"] == str(tmp_path)

            stopped = client.post("/api/agents/ui-bot/stop", json={})
            assert stopped.status_code == 200
            assert stopped.json()["desired_state"] == "stopped"

            started = client.post("/api/agents/ui-bot/start", json={})
            assert started.status_code == 200
            assert started.json()["desired_state"] == "running"

            sent = client.post(
                "/api/agents/ui-bot/send",
                json={"content": "hello there", "to": "codex"},
            )
            assert sent.status_code == 201
            sent_payload = sent.json()
            assert sent_payload["agent"] == "ui-bot"
            assert sent_payload["content"] == "@codex hello there"

            tested = client.post("/api/agents/ui-bot/test", json={})
            assert tested.status_code == 201
            tested_payload = tested.json()
            assert tested_payload["target_agent"] == "ui-bot"
            assert tested_payload["author"] == "agent"
            assert tested_payload["sender_agent"].startswith("switchboard-")
            assert (
                tested_payload["content"]
                == "@ui-bot Reply with exactly: Gateway test OK. Then mention which local model answered."
            )

            doctored = client.post("/api/agents/ui-bot/doctor", json={})
            assert doctored.status_code == 201
            doctor_payload = doctored.json()
            assert doctor_payload["name"] == "ui-bot"
            assert doctor_payload["status"] in {"passed", "warning", "failed"}

            removed = client.delete("/api/agents/ui-bot")
            assert removed.status_code == 200
            assert removed.json()["name"] == "ui-bot"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_gateway_move_updates_routing_for_test_messages(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )

    registry = gateway_core.load_gateway_registry()
    mover_token = tmp_path / "mover.token"
    mover_token.write_text("axp_a_mover.secret")
    switchboard_token = tmp_path / "switchboard.token"
    switchboard_token.write_text("axp_a_switchboard.secret")
    allowed_spaces = [
        {"space_id": "space-1", "name": "Old Space", "is_default": True},
        {"space_id": "space-2", "name": "New Space", "is_default": False},
    ]
    registry["agents"] = [
        {
            "name": "mover",
            "agent_id": "agent-mover",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "template_id": "echo_test",
            "desired_state": "running",
            "effective_state": "running",
            "transport": "gateway",
            "credential_source": "gateway",
            "allowed_spaces": allowed_spaces,
            "token_file": str(mover_token),
        },
        {
            "name": "switchboard-space2",
            "agent_id": "agent-switchboard-space2",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "inbox",
            "template_id": "inbox",
            "desired_state": "running",
            "effective_state": "running",
            "transport": "gateway",
            "credential_source": "gateway",
            "allowed_spaces": allowed_spaces,
            "token_file": str(switchboard_token),
        },
    ]
    for entry in registry["agents"]:
        gateway_core.ensure_gateway_identity_binding(registry, entry, session=gateway_core.load_gateway_session())
    gateway_core.save_gateway_registry(registry)

    class FakePlacementClient:
        def __init__(self):
            self.calls = []
            self.space_id = "space-1"

        def set_agent_placement(self, identifier, *, space_id, pinned=False):
            self.calls.append({"identifier": identifier, "space_id": space_id, "pinned": pinned})
            self.space_id = space_id
            return {"agent_id": identifier, "space_id": space_id, "allowed_spaces": ["space-1", "space-2"]}

        def get_agent_placement(self, identifier):
            return {
                "agent_id": identifier,
                "name": "mover",
                "space_id": self.space_id,
                "allowed_spaces": ["space-1", "space-2"],
                "_record": {
                    "id": identifier,
                    "name": "mover",
                    "space_id": self.space_id,
                    "allowed_spaces": ["space-1", "space-2"],
                },
            }

        def get_agent(self, identifier):
            return {"agent": {"id": identifier, "name": "mover", "space_id": self.space_id}}

    fake_user_client = FakePlacementClient()
    sent_messages = []

    class RecordingManagedClient:
        def send_message(self, space_id, content, *, agent_id=None, parent_id=None, metadata=None):
            sent_messages.append(
                {
                    "space_id": space_id,
                    "content": content,
                    "agent_id": agent_id,
                    "parent_id": parent_id,
                    "metadata": metadata,
                }
            )
            return {"message": {"id": "gateway-test-1", "space_id": space_id, "content": content}}

    monkeypatch.setattr(gateway_cmd, "_load_gateway_user_client", lambda: fake_user_client)
    monkeypatch.setattr(gateway_cmd, "_load_managed_agent_client", lambda entry: RecordingManagedClient())

    moved = gateway_cmd._move_managed_agent_space("mover", "space-2")

    assert fake_user_client.calls == [{"identifier": "agent-mover", "space_id": "space-2", "pinned": False}]
    assert moved["space_id"] == "space-2"
    stored = gateway_core.find_agent_entry(gateway_core.load_gateway_registry(), "mover")
    assert stored["space_id"] == "space-2"

    tested = gateway_cmd._send_gateway_test_to_managed_agent("mover")

    assert tested["target_agent"] == "mover"
    assert tested["message"]["space_id"] == "space-2"
    assert sent_messages[-1]["space_id"] == "space-2"
    assert sent_messages[-1]["content"].startswith("@mover ")


def test_gateway_test_falls_back_when_space_sender_cannot_be_created(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )

    token_file = tmp_path / "mover.token"
    token_file.write_text("axp_a_mover.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "mover",
            "agent_id": "agent-mover",
            "space_id": "space-2",
            "active_space_id": "space-2",
            "default_space_id": "space-2",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "template_id": "echo_test",
            "desired_state": "running",
            "effective_state": "running",
            "transport": "gateway",
            "credential_source": "gateway",
            "allowed_spaces": [{"space_id": "space-2", "name": "New Space", "is_default": True}],
            "token_file": str(token_file),
        }
    ]
    gateway_core.ensure_gateway_identity_binding(
        registry,
        registry["agents"][0],
        session=gateway_core.load_gateway_session(),
    )
    gateway_core.save_gateway_registry(registry)
    sent_messages = []

    class RecordingManagedClient:
        def send_message(self, space_id, content, *, agent_id=None, parent_id=None, metadata=None):
            sent_messages.append({"space_id": space_id, "content": content, "agent_id": agent_id, "metadata": metadata})
            return {
                "message": {
                    "id": "gateway-test-1",
                    "space_id": space_id,
                    "content": content,
                    "agent_id": agent_id,
                    "metadata": metadata,
                }
            }

    monkeypatch.setattr(gateway_cmd, "_load_managed_agent_client", lambda entry: RecordingManagedClient())
    monkeypatch.setattr(
        gateway_cmd,
        "_register_managed_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("Backend rejected test sender PAT mint: 400")),
    )

    result = gateway_cmd._send_gateway_test_to_managed_agent("mover")

    assert result["sender_agent"] == "mover"
    assert result["message"]["space_id"] == "space-2"
    assert sent_messages[-1]["agent_id"] == "agent-mover"
    gateway_meta = sent_messages[-1]["metadata"]["gateway"]
    assert gateway_meta["test_sender_fallback"] == "self"
    assert "400" in gateway_meta["test_sender_error"]


def test_gateway_agents_update_changes_template_and_workdir(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    token_file = tmp_path / "echo.token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    entry = {
        "name": "northstar",
        "agent_id": "agent-1",
        "space_id": "space-1",
        "base_url": "https://paxai.app",
        "runtime_type": "echo",
        "template_id": "echo_test",
        "template_label": "Echo (Test)",
        "desired_state": "running",
        "effective_state": "running",
        "token_file": str(token_file),
        "transport": "gateway",
        "credential_source": "gateway",
        "created_via": "cli",
    }
    registry["agents"] = [entry]
    gateway_core.ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)
    gateway_core.ensure_gateway_identity_binding(registry, entry, session=gateway_core.load_gateway_session())
    gateway_core.save_gateway_registry(registry)
    monkeypatch.setattr(gateway_cmd, "_load_gateway_user_client", lambda: _FakeUserClient())

    result = runner.invoke(
        app,
        [
            "gateway",
            "agents",
            "update",
            "northstar",
            "--template",
            "ollama",
            "--workdir",
            str(tmp_path),
            "--exec",
            "python3 examples/gateway_ollama/ollama_bridge.py",
            "--timeout",
            "120",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["template_id"] == "ollama"
    assert payload["runtime_type"] == "exec"
    assert payload["workdir"] == str(tmp_path)
    assert payload["timeout_seconds"] == 120
    stored = gateway_core.load_gateway_registry()["agents"][0]
    assert stored["template_id"] == "ollama"
    assert stored["workdir"] == str(tmp_path)
    assert stored["timeout_seconds"] == 120
    registry_after = gateway_core.load_gateway_registry()
    binding = registry_after["bindings"][0]
    assert binding["launch_spec"]["runtime_type"] == "exec"
    assert binding["launch_spec"]["workdir"] == str(tmp_path)
    assert binding["path"] == str(tmp_path)
    runtime_fingerprint = binding["runtime_fingerprint"]
    assert runtime_fingerprint["schema"] == "gateway.runtime_fingerprint.v1"
    assert runtime_fingerprint["runtime_type"] == "exec"
    assert runtime_fingerprint["template_id"] == "ollama"
    assert runtime_fingerprint["workdir"] == str(tmp_path)
    assert runtime_fingerprint["command"] == "python3 examples/gateway_ollama/ollama_bridge.py"
    assert runtime_fingerprint["runtime_fingerprint_hash"].startswith("sha256:")
    attestation = gateway_core.evaluate_runtime_attestation(registry_after, stored)
    assert attestation["attestation_state"] == "verified"


def test_gateway_agents_add_ollama_persists_model_override(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    monkeypatch.setattr(gateway_cmd, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(gateway_cmd, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_cmd, "_create_agent_in_space", _fake_create_agent_in_space)
    monkeypatch.setattr(gateway_cmd, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_cmd, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))

    result = runner.invoke(
        app,
        [
            "gateway",
            "agents",
            "add",
            "ember",
            "--template",
            "ollama",
            "--ollama-model",
            "gemma4:latest",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["template_id"] == "ollama"
    assert payload["ollama_model"] == "gemma4:latest"
    stored = gateway_core.load_gateway_registry()["agents"][0]
    assert stored["ollama_model"] == "gemma4:latest"


def test_gateway_agents_add_ollama_uses_recommended_model_when_unspecified(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    monkeypatch.setattr(gateway_cmd, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(gateway_cmd, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_cmd, "_create_agent_in_space", _fake_create_agent_in_space)
    monkeypatch.setattr(gateway_cmd, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_cmd, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))
    monkeypatch.setattr(
        gateway_cmd,
        "ollama_setup_status",
        lambda preferred_model=None: {
            "recommended_model": "gemma4:latest",
            "server_reachable": True,
            "available_models": ["gemma4:latest"],
            "local_models": ["gemma4:latest"],
            "summary": "Ollama is reachable. Recommended model: gemma4:latest.",
        },
    )

    result = runner.invoke(
        app,
        [
            "gateway",
            "agents",
            "add",
            "ember-default",
            "--template",
            "ollama",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["template_id"] == "ollama"
    assert payload["ollama_model"] == "gemma4:latest"
    stored = gateway_core.load_gateway_registry()["agents"][0]
    assert stored["ollama_model"] == "gemma4:latest"


def test_gateway_agents_show_json_filters_activity(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "echo-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "runtime_type": "echo",
            "desired_state": "running",
            "effective_state": "running",
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "last_reply_preview": "Echo: ping",
            "token_file": "/tmp/echo-token",
        },
        {
            "name": "other-bot",
            "agent_id": "agent-2",
            "space_id": "space-1",
            "runtime_type": "exec",
            "desired_state": "running",
            "effective_state": "running",
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "token_file": "/tmp/other-token",
        },
    ]
    gateway_core.save_gateway_registry(registry)
    gateway_core.record_gateway_activity("reply_sent", entry=registry["agents"][0], reply_preview="Echo: ping")
    gateway_core.record_gateway_activity("reply_sent", entry=registry["agents"][1], reply_preview="Other reply")

    result = runner.invoke(app, ["gateway", "agents", "show", "echo-bot", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["agent"]["name"] == "echo-bot"
    assert payload["recent_activity"]
    assert all(row["agent_name"] == "echo-bot" for row in payload["recent_activity"])


def test_gateway_agents_send_uses_managed_identity(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    token_file = tmp_path / "sender.token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "sender-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "inbox",
            "desired_state": "running",
            "effective_state": "running",
            "token_file": str(token_file),
            "transport": "gateway",
            "credential_source": "gateway",
        }
    ]
    gateway_core.save_gateway_registry(registry)
    monkeypatch.setattr(gateway_cmd, "AxClient", _FakeManagedSendClient)

    result = runner.invoke(app, ["gateway", "agents", "send", "sender-bot", "hello there", "--to", "codex", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["agent"] == "sender-bot"
    assert payload["content"] == "@codex hello there"
    assert payload["message"]["metadata"]["gateway"]["sent_via"] == "gateway_cli"
    recent = gateway_core.load_recent_gateway_activity()
    assert recent[-1]["event"] == "manual_message_sent"


def test_gateway_agents_send_rejects_user_bootstrap_pat(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    token_file = tmp_path / "sender.token"
    token_file.write_text("axp_u_user.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "sender-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "inbox",
            "desired_state": "running",
            "effective_state": "running",
            "token_file": str(token_file),
            "transport": "gateway",
            "credential_source": "gateway",
        }
    ]
    gateway_core.save_gateway_registry(registry)
    monkeypatch.setattr(gateway_cmd, "AxClient", _FakeManagedSendClient)

    result = runner.invoke(app, ["gateway", "agents", "send", "sender-bot", "hello there", "--to", "codex"])

    assert result.exit_code == 1, result.output
    assert "agent-bound token" in result.output
    assert "user" in result.output
    assert "bootstrap PAT" in result.output


def test_gateway_agents_send_acknowledges_pending_inbox_message(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    token_file = tmp_path / "sender.token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "sender-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "inbox",
            "desired_state": "running",
            "effective_state": "running",
            "token_file": str(token_file),
            "transport": "gateway",
            "credential_source": "gateway",
            "backlog_depth": 1,
            "current_status": "queued",
            "current_activity": "Queued in Gateway",
            "last_received_message_id": "msg-queued-1",
            "last_work_received_at": "2026-04-23T18:00:00+00:00",
        }
    ]
    gateway_core.save_gateway_registry(registry)
    gateway_core.save_agent_pending_messages(
        "sender-bot",
        [
            {
                "message_id": "msg-queued-1",
                "parent_id": None,
                "conversation_id": "msg-queued-1",
                "content": "@sender-bot hello there",
                "display_name": "madtank",
                "created_at": "2026-04-23T18:00:00+00:00",
                "queued_at": "2026-04-23T18:00:01+00:00",
            }
        ],
    )
    monkeypatch.setattr(gateway_cmd, "AxClient", _FakeManagedSendClient)

    result = runner.invoke(
        app,
        [
            "gateway",
            "agents",
            "send",
            "sender-bot",
            "handled",
            "--parent-id",
            "msg-queued-1",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["message"]["parent_id"] == "msg-queued-1"
    assert gateway_core.load_agent_pending_messages("sender-bot") == []
    updated = gateway_core.find_agent_entry(gateway_core.load_gateway_registry(), "sender-bot")
    assert updated["backlog_depth"] == 0
    assert updated["current_status"] is None
    assert updated["current_activity"] is None
    assert updated["processed_count"] == 1
    assert updated["last_reply_message_id"] == "msg-sent-1"
    recent = gateway_core.load_recent_gateway_activity()
    assert recent[-1]["event"] == "manual_queue_acknowledged"


def test_gateway_agents_send_blocks_identity_mismatch(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    token_file = tmp_path / "sender.token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "sender-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "inbox",
            "desired_state": "running",
            "effective_state": "running",
            "token_file": str(token_file),
            "transport": "gateway",
            "credential_source": "gateway",
            "install_id": "inst-sender-1",
        }
    ]
    gateway_core.ensure_gateway_identity_binding(
        registry, registry["agents"][0], session=gateway_core.load_gateway_session()
    )
    registry["identity_bindings"][0]["acting_identity"]["agent_name"] = "night_owl"
    gateway_core.save_gateway_registry(registry)
    monkeypatch.setattr(gateway_cmd, "AxClient", _FakeManagedSendClient)

    result = runner.invoke(app, ["gateway", "agents", "send", "sender-bot", "hello there", "--to", "codex", "--json"])

    assert result.exit_code == 1, result.output
    assert "identity_mismatch" in result.output.lower() or "mismatched acting identity" in result.output.lower()


def test_gateway_agents_test_sends_gateway_authored_probe(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "echo-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "template_id": "echo_test",
            "desired_state": "running",
            "effective_state": "running",
            "transport": "gateway",
            "credential_source": "gateway",
        }
    ]
    gateway_core.save_gateway_registry(registry)
    monkeypatch.setattr(gateway_cmd, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(gateway_cmd, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_cmd, "_create_agent_in_space", _fake_create_agent_in_space)
    monkeypatch.setattr(gateway_cmd, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_cmd, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))
    monkeypatch.setattr(gateway_cmd, "AxClient", _FakeManagedSendClient)

    result = runner.invoke(app, ["gateway", "agents", "test", "echo-bot", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["target_agent"] == "echo-bot"
    assert payload["author"] == "agent"
    assert payload["sender_agent"] == "switchboard-space1"
    assert payload["recommended_prompt"] == "gateway test ping"
    assert payload["content"] == "@echo-bot gateway test ping"
    assert payload["message"]["metadata"]["gateway"]["sent_via"] == "gateway_test"
    assert payload["message"]["metadata"]["gateway"]["test_author"] == "agent"
    recent = gateway_core.load_recent_gateway_activity()
    assert recent[-1]["event"] == "gateway_test_sent"


def test_gateway_agents_test_can_send_as_user(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "echo-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "template_id": "echo_test",
            "desired_state": "running",
            "effective_state": "running",
            "transport": "gateway",
            "credential_source": "gateway",
        }
    ]
    gateway_core.save_gateway_registry(registry)
    monkeypatch.setattr(gateway_cmd, "_load_gateway_user_client", lambda: _FakeUserClient())

    result = runner.invoke(app, ["gateway", "agents", "test", "echo-bot", "--author", "user", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["author"] == "user"
    assert payload["sender_agent"] is None
    assert payload["message"]["metadata"]["gateway"]["test_author"] == "user"


def test_gateway_local_send_auto_connects_with_agent(monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if url.endswith("/local/connect"):
            return _FakeHttpResponse(
                {
                    "status": "approved",
                    "agent": {"name": "codex-pass-through"},
                    "registry_ref": "#4",
                    "session_token": "axgw_s_test.session",
                }
            )
        if url.endswith("/local/send"):
            return _FakeHttpResponse(
                {
                    "agent": "codex-pass-through",
                    "message": {"id": "msg-1", "content": json["content"], "space_id": json.get("space_id")},
                },
                status_code=201,
            )
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr(gateway_cmd.httpx, "post", fake_post)

    result = runner.invoke(
        app,
        [
            "gateway",
            "local",
            "send",
            "--agent",
            "codex-pass-through",
            "--url",
            "http://127.0.0.1:8765",
            "@night-owl please QA PR 114",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["url"].endswith("/local/connect")
    assert calls[0]["json"]["agent_name"] == "codex-pass-through"
    assert calls[1]["url"].endswith("/local/send")
    assert calls[1]["headers"] == {"X-Gateway-Session": "axgw_s_test.session"}
    assert calls[1]["json"]["content"] == "@night-owl please QA PR 114"
    payload = json.loads(result.output)
    assert payload["agent"] == "codex-pass-through"
    assert payload["connect"]["agent"] == "codex-pass-through"


def test_gateway_local_inbox_auto_connects_and_marks_read(monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"method": "POST", "url": url, "json": json, "headers": headers, "timeout": timeout})
        return _FakeHttpResponse(
            {
                "status": "approved",
                "agent": {"name": "codex-pass-through"},
                "registry_ref": "#4",
                "session_token": "axgw_s_test.session",
            }
        )

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"method": "GET", "url": url, "params": params, "headers": headers, "timeout": timeout})
        return _FakeHttpResponse({"agent": "codex-pass-through", "messages": [], "count": 0})

    monkeypatch.setattr(gateway_cmd.httpx, "post", fake_post)
    monkeypatch.setattr(gateway_cmd.httpx, "get", fake_get)

    result = runner.invoke(
        app,
        [
            "gateway",
            "local",
            "inbox",
            "--agent",
            "codex-pass-through",
            "--url",
            "http://127.0.0.1:8765",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["method"] == "POST"
    assert calls[1]["method"] == "GET"
    assert calls[1]["headers"] == {"X-Gateway-Session": "axgw_s_test.session"}
    assert calls[1]["params"]["mark_read"] == "true"
    payload = json.loads(result.output)
    assert payload["agent"] == "codex-pass-through"
    assert payload["connect"]["registry_ref"] == "#4"


def test_gateway_agents_doctor_persists_structured_result(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    token_file = tmp_path / "inbox.token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "docs-worker",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "inbox",
            "template_id": "inbox",
            "desired_state": "running",
            "effective_state": "stopped",
            "token_file": str(token_file),
            "transport": "gateway",
            "credential_source": "gateway",
        }
    ]
    gateway_core.save_gateway_registry(registry)

    result = runner.invoke(app, ["gateway", "agents", "doctor", "docs-worker", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "warning"
    check_names = [item["name"] for item in payload["checks"]]
    assert "gateway_auth" in check_names
    assert "queue_writable" in check_names
    assert "worker_attached" in check_names
    assert isinstance(payload["agent"]["last_doctor_result"], dict)
    assert payload["agent"]["last_doctor_result"]["status"] == "warning"
    assert payload["agent"]["last_doctor_result"]["checks"]
    assert payload["agent"]["last_successful_doctor_at"]

    stored = gateway_core.load_gateway_registry()["agents"][0]
    assert stored["last_doctor_result"]["status"] == "warning"
    assert stored["last_successful_doctor_at"]


def test_gateway_status_payload_surfaces_alerts(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "stale-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "runtime_type": "exec",
            "desired_state": "running",
            "effective_state": "running",
            "last_seen_at": (
                datetime.now(timezone.utc) - timedelta(seconds=gateway_core.RUNTIME_STALE_AFTER_SECONDS + 5)
            ).isoformat(),
            "backlog_depth": 2,
            "last_error": None,
            "token_file": "/tmp/stale-token",
        },
        {
            "name": "broken-bot",
            "agent_id": "agent-2",
            "space_id": "space-1",
            "runtime_type": "exec",
            "desired_state": "running",
            "effective_state": "error",
            "last_error": "bridge crashed",
            "token_file": "/tmp/broken-token",
        },
        {
            "name": "setup-bot",
            "agent_id": "agent-3",
            "space_id": "space-1",
            "runtime_type": "exec",
            "desired_state": "running",
            "effective_state": "running",
            "last_reply_preview": "(stderr: ERROR: hermes-agent repo not found at /Users/jacob/hermes-agent.)",
            "token_file": "/tmp/setup-token",
        },
    ]
    gateway_core.save_gateway_registry(registry)

    payload = gateway_cmd._status_payload(activity_limit=5)

    assert payload["summary"]["alert_count"] >= 2
    titles = [item["title"] for item in payload["alerts"]]
    assert any("@stale-bot looks stale" == title for title in titles)
    assert any("@broken-bot hit an error" == title for title in titles)
    assert any("@setup-bot has a runtime setup error" == title for title in titles)


def test_gateway_spaces_use_resolves_slug_and_updates_session(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "private-space",
            "space_name": "madtank-workspace",
            "username": "codex",
        }
    )

    class FakeClient:
        def list_spaces(self):
            return {
                "spaces": [
                    {"id": "private-space", "slug": "madtank-workspace", "name": "madtank's Workspace"},
                    {"id": "team-space", "slug": "ax-cli-dev", "name": "aX CLI Dev"},
                ]
            }

    monkeypatch.setattr(gateway_cmd, "_load_gateway_user_client", lambda: FakeClient())

    result = runner.invoke(app, ["gateway", "spaces", "use", "ax-cli-dev", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["space_id"] == "team-space"
    assert payload["space_name"] == "aX CLI Dev"
    session = gateway_core.load_gateway_session()
    assert session["space_id"] == "team-space"
    assert session["space_name"] == "aX CLI Dev"
    registry = gateway_core.load_gateway_registry()
    assert registry["gateway"]["space_id"] == "team-space"
    assert registry["gateway"]["space_name"] == "aX CLI Dev"


def test_gateway_spaces_current_shows_session_space(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "team-space",
            "space_name": "ax-cli-dev",
            "username": "codex",
        }
    )

    result = runner.invoke(app, ["gateway", "spaces", "current", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "space_id": "team-space",
        "space_name": "ax-cli-dev",
        "base_url": "https://paxai.app",
        "username": "codex",
    }
