"""`ax gateway agents test` defaults to the invoking principal, never switchboard.

Per Madtank/supervisor (2026-05-02): service accounts (switchboard-*) are for
service-originated events (reminders, logs, system notifications). Principal-
invoked surfaces — including `agents test` and the UI test button — must
author as whoever invoked the command.

Three rules pinned here:
1. Default sender = invoking principal resolved from the workspace's
   `[gateway]`/`[agent]` local config.
2. With no resolvable invoking principal AND no explicit `--sender-agent`:
   fail hard with a message that points at Gateway-managed workdir + the
   service-account opt-in.
3. The switchboard auto-creation path (`_ensure_gateway_test_sender`) is
   never reached from the default `agents test` flow. It remains available
   for explicit service-event flows.
"""

import pytest

from ax_cli import gateway as gateway_core
from ax_cli.commands import gateway as gateway_cmd


def _make_registry_agent(*, name, agent_id, token_file, space_id="space-1"):
    return {
        "name": name,
        "agent_id": agent_id,
        "space_id": space_id,
        "active_space_id": space_id,
        "default_space_id": space_id,
        "base_url": "https://paxai.app",
        "runtime_type": "echo",
        "template_id": "echo_test",
        "desired_state": "running",
        "effective_state": "running",
        "transport": "gateway",
        "credential_source": "gateway",
        "allowed_spaces": [{"space_id": space_id, "name": "Test Space", "is_default": True}],
        "token_file": str(token_file),
    }


def _seed_session_and_registry(tmp_path, monkeypatch, *, extra_agents=()):
    config_dir = tmp_path / "_gw_config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "operator",
        }
    )
    target_token = tmp_path / "target.token"
    target_token.write_text("axp_a_target.secret")
    agents = [
        _make_registry_agent(name="target_agent", agent_id="agent-target", token_file=target_token),
    ]
    for spec in extra_agents:
        agents.append(spec)
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = agents
    for entry in registry["agents"]:
        gateway_core.ensure_gateway_identity_binding(
            registry, entry, session=gateway_core.load_gateway_session()
        )
    gateway_core.save_gateway_registry(registry)


def _passthrough_send_guard(monkeypatch):
    """Stub `_identity_space_send_guard` so credential-confidence checks do not
    block sender-routing tests. The guard's behavior is verified elsewhere."""
    def fake(entry, *, explicit_space_id=None):
        return {
            "active_space_id": str(entry.get("space_id") or "space-1"),
            "confidence": "HIGH",
            "reachability": "ready",
        }

    monkeypatch.setattr(gateway_cmd, "_identity_space_send_guard", fake)


def _write_workspace_gateway_config(tmp_path, *, agent_name="cli_god"):
    """Make `resolve_gateway_config()` see a Gateway-managed workspace."""
    local_ax = tmp_path / ".ax"
    local_ax.mkdir(exist_ok=True)
    (local_ax / "config.toml").write_text(
        "[gateway]\n"
        'mode = "local"\n'
        'url = "http://127.0.0.1:8765"\n'
        "\n"
        "[agent]\n"
        f'agent_name = "{agent_name}"\n'
        f'workdir = "{tmp_path}"\n'
    )
    # Caller is responsible for monkeypatch.chdir(tmp_path).


def _install_recording_client(monkeypatch):
    sent: list[dict] = []
    sender_arg_seen: list[str] = []

    class RecordingManagedClient:
        def send_message(self, space_id, content, *, agent_id=None, parent_id=None, metadata=None):
            sent.append(
                {
                    "space_id": space_id,
                    "content": content,
                    "agent_id": agent_id,
                    "parent_id": parent_id,
                    "metadata": metadata,
                }
            )
            return {
                "message": {
                    "id": "msg-1",
                    "space_id": space_id,
                    "content": content,
                    "agent_id": agent_id,
                    "metadata": metadata,
                }
            }

    def loader(entry):
        sender_arg_seen.append(str(entry.get("name") or ""))
        return RecordingManagedClient()

    monkeypatch.setattr(gateway_cmd, "_load_managed_agent_client", loader)
    return sent, sender_arg_seen


def _block_switchboard_auto_creation(monkeypatch):
    """Pin that `_ensure_gateway_test_sender` is never called from the default path."""
    calls = {"count": 0}

    def boom(*args, **kwargs):
        calls["count"] += 1
        raise AssertionError(
            "_ensure_gateway_test_sender must NOT be called from the default agents-test "
            "flow — service-account auto-creation is opt-in only via explicit --sender-agent"
        )

    monkeypatch.setattr(gateway_cmd, "_ensure_gateway_test_sender", boom)
    return calls


# -- Rule 1: default sender is the invoking principal ----------------------------


def test_default_sender_is_invoking_principal_from_workspace_config(tmp_path, monkeypatch):
    invoking_token = tmp_path / "cli_god.token"
    invoking_token.write_text("axp_a_invoker.secret")
    _seed_session_and_registry(
        tmp_path,
        monkeypatch,
        extra_agents=[_make_registry_agent(name="cli_god", agent_id="agent-cli-god", token_file=invoking_token)],
    )
    _write_workspace_gateway_config(tmp_path, agent_name="cli_god")
    monkeypatch.chdir(tmp_path)
    sent, sender_seen = _install_recording_client(monkeypatch)
    _block_switchboard_auto_creation(monkeypatch)
    _passthrough_send_guard(monkeypatch)

    result = gateway_cmd._send_gateway_test_to_managed_agent("target_agent")

    # The Gateway-side managed-agent loader is called with the INVOKING principal entry
    assert "cli_god" in sender_seen, f"sender entry passed to client loader: {sender_seen}"
    # Result reports the invoking principal, not switchboard
    assert result.get("sender_agent") == "cli_god"
    # No switchboard string anywhere in the sent payload
    payload = sent[-1]
    assert "switchboard" not in payload["content"].lower()
    assert "switchboard" not in str(payload.get("metadata") or "").lower()


def test_default_path_does_not_invoke_switchboard_auto_creation(tmp_path, monkeypatch):
    """Regression pin: even if the invoking principal resolves cleanly, the
    auto-switchboard helper must not be reached."""
    invoking_token = tmp_path / "cli_god.token"
    invoking_token.write_text("axp_a_invoker.secret")
    _seed_session_and_registry(
        tmp_path,
        monkeypatch,
        extra_agents=[_make_registry_agent(name="cli_god", agent_id="agent-cli-god", token_file=invoking_token)],
    )
    _write_workspace_gateway_config(tmp_path, agent_name="cli_god")
    monkeypatch.chdir(tmp_path)
    _install_recording_client(monkeypatch)
    _passthrough_send_guard(monkeypatch)
    auto_calls = _block_switchboard_auto_creation(monkeypatch)

    gateway_cmd._send_gateway_test_to_managed_agent("target_agent")

    assert auto_calls["count"] == 0


# -- Rule 2: fail hard when no invoking principal and no explicit override -------


def test_fails_hard_when_no_invoking_principal_resolvable(tmp_path, monkeypatch):
    _seed_session_and_registry(tmp_path, monkeypatch)
    # Deliberately NO workspace config — empty cwd, no Gateway local session
    monkeypatch.chdir(tmp_path)
    _block_switchboard_auto_creation(monkeypatch)

    with pytest.raises(ValueError) as excinfo:
        gateway_cmd._send_gateway_test_to_managed_agent("target_agent")

    msg = str(excinfo.value).lower()
    # Operator-actionable error per supervisor's prescribed shape
    assert "invoking principal" in msg or "invoking" in msg
    assert "gateway-managed" in msg or "gateway managed" in msg or "workdir" in msg
    assert "--sender-agent" in msg or "service" in msg
    # Don't use literal square-bracket section names — Rich console.print()
    # strips them as markup, so the operator sees `` + `` blocks.
    assert "[gateway]" not in str(excinfo.value)
    assert "[agent]" not in str(excinfo.value)


def test_fail_hard_does_not_silently_fall_back_to_switchboard_or_user(tmp_path, monkeypatch):
    """Belt-and-suspenders: when fail-hard fires, no message is sent at all."""
    _seed_session_and_registry(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    _block_switchboard_auto_creation(monkeypatch)
    sent, _ = _install_recording_client(monkeypatch)

    with pytest.raises(ValueError):
        gateway_cmd._send_gateway_test_to_managed_agent("target_agent")

    assert sent == [], "fail-hard must not deliver a message"


# -- Rule 3: explicit --sender-agent override remains the service-account path ---


def test_explicit_sender_agent_overrides_invoking_principal(tmp_path, monkeypatch):
    """Operator can name any sender (including switchboard) explicitly. The
    explicit name wins over the auto-resolved invoking principal."""
    invoking_token = tmp_path / "cli_god.token"
    invoking_token.write_text("axp_a_invoker.secret")
    sb_token = tmp_path / "switchboard.token"
    sb_token.write_text("axp_a_switchboard.secret")
    _seed_session_and_registry(
        tmp_path,
        monkeypatch,
        extra_agents=[
            _make_registry_agent(name="cli_god", agent_id="agent-cli-god", token_file=invoking_token),
            _make_registry_agent(name="switchboard-space1", agent_id="agent-sb1", token_file=sb_token),
        ],
    )
    _write_workspace_gateway_config(tmp_path, agent_name="cli_god")
    monkeypatch.chdir(tmp_path)
    _passthrough_send_guard(monkeypatch)
    sent, sender_seen = _install_recording_client(monkeypatch)

    result = gateway_cmd._send_gateway_test_to_managed_agent(
        "target_agent", sender_agent="switchboard-space1"
    )

    assert result.get("sender_agent") == "switchboard-space1"
    assert "switchboard-space1" in sender_seen


def test_explicit_sender_agent_works_even_without_invoking_principal(tmp_path, monkeypatch):
    """The opt-in service-account path is the escape hatch: even when no
    invoking principal resolves, naming a sender explicitly works."""
    sb_token = tmp_path / "switchboard.token"
    sb_token.write_text("axp_a_switchboard.secret")
    _seed_session_and_registry(
        tmp_path,
        monkeypatch,
        extra_agents=[
            _make_registry_agent(name="switchboard-space1", agent_id="agent-sb1", token_file=sb_token),
        ],
    )
    monkeypatch.chdir(tmp_path)
    _passthrough_send_guard(monkeypatch)
    sent, sender_seen = _install_recording_client(monkeypatch)

    result = gateway_cmd._send_gateway_test_to_managed_agent(
        "target_agent", sender_agent="switchboard-space1"
    )

    assert result.get("sender_agent") == "switchboard-space1"


# -- Resolver helper unit tests --------------------------------------------------


def test_resolve_invoking_principal_returns_workspace_agent(tmp_path, monkeypatch):
    _write_workspace_gateway_config(tmp_path, agent_name="codex_supervisor")
    monkeypatch.chdir(tmp_path)
    assert gateway_cmd._resolve_invoking_principal() == "codex_supervisor"


def test_resolve_invoking_principal_returns_none_when_no_gateway_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert gateway_cmd._resolve_invoking_principal() is None


def test_resolve_invoking_principal_returns_none_when_gateway_config_lacks_agent_name(tmp_path, monkeypatch):
    local_ax = tmp_path / ".ax"
    local_ax.mkdir()
    (local_ax / "config.toml").write_text(
        "[gateway]\n"
        'mode = "local"\n'
        'url = "http://127.0.0.1:8765"\n'
    )
    monkeypatch.chdir(tmp_path)
    assert gateway_cmd._resolve_invoking_principal() is None
