"""Gateway runtime backends and operator-facing templates.

Runtime types are the low-level execution adapters used by the Gateway.
Templates are the higher-level, user-facing choices presented in CLI and UI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _gateway_setup_skill_path() -> Path:
    return _repo_root() / "skills" / "gateway-agent-setup" / "SKILL.md"


def _shared_signals() -> dict[str, str]:
    return {
        "delivery": "Gateway confirms when a message was queued or claimed.",
        "liveness": "Gateway heartbeat and reconnect logic determine connected or stale state.",
    }


def runtime_type_catalog() -> dict[str, dict[str, Any]]:
    repo_root = _repo_root()
    return {
        "echo": {
            "id": "echo",
            "label": "Echo",
            "description": "Built-in test runtime for proving delivery, queueing, and reply flow.",
            "kind": "builtin",
            "passive": False,
            "requires": [],
            "form_fields": [],
            "examples": [],
            "signals": {
                **_shared_signals(),
                "activity": "Gateway emits built-in working and completed phases for echo replies.",
                "tools": "No tool-call telemetry. Echo is intentionally simple.",
            },
        },
        "exec": {
            "id": "exec",
            "label": "Command Bridge",
            "description": (
                "Gateway-owned command execution for bridges and adapters that print AX_GATEWAY_EVENT lines."
            ),
            "kind": "exec",
            "passive": False,
            "requires": ["exec_command"],
            "form_fields": [
                {
                    "name": "exec_command",
                    "label": "Exec Command",
                    "required": True,
                    "placeholder": "python3 examples/hermes_sentinel/hermes_bridge.py",
                },
                {
                    "name": "workdir",
                    "label": "Workdir",
                    "required": True,
                    "placeholder": str(repo_root),
                },
            ],
            "examples": [
                {
                    "label": "Gateway Probe",
                    "exec_command": "python3 examples/gateway_probe/probe_bridge.py",
                    "workdir": str(repo_root),
                },
                {
                    "label": "Codex Bridge",
                    "exec_command": "python3 examples/codex_gateway/codex_bridge.py",
                    "workdir": str(repo_root),
                },
                {
                    "label": "Hermes Sentinel",
                    "exec_command": "python3 examples/hermes_sentinel/hermes_bridge.py",
                    "workdir": str(repo_root),
                    "note": "Requires a local hermes-agent checkout plus auth setup.",
                },
                {
                    "label": "Ollama",
                    "exec_command": "python3 examples/gateway_ollama/ollama_bridge.py",
                    "workdir": str(repo_root),
                    "note": "Requires a local Ollama server and model.",
                },
            ],
            "signals": {
                **_shared_signals(),
                "activity": (
                    "Gateway can surface live activity when the bridge prints AX_GATEWAY_EVENT lines. "
                    "Without that, the operator still gets pickup and final completion."
                ),
                "tools": "Gateway can record tool usage when the bridge emits tool events.",
            },
        },
        "hermes_sentinel": {
            "id": "hermes_sentinel",
            "label": "Hermes Sentinel",
            "description": (
                "Gateway-supervised long-running Hermes sentinel using the original "
                "claude_agent_v2.py listener semantics."
            ),
            "kind": "supervised_process",
            "passive": False,
            "requires": [],
            "form_fields": [
                {
                    "name": "workdir",
                    "label": "Workdir",
                    "required": True,
                    "placeholder": "/home/ax-agent/agents/dev_sentinel",
                },
                {
                    "name": "model",
                    "label": "Model",
                    "required": False,
                    "placeholder": "codex:gpt-5.5",
                },
            ],
            "examples": [
                {
                    "label": "Hermes dev sentinel",
                    "runtime_type": "hermes_sentinel",
                    "workdir": "/home/ax-agent/agents/dev_sentinel",
                    "note": "Gateway starts the old listener once and monitors it; Hermes owns session continuity.",
                },
            ],
            "signals": {
                **_shared_signals(),
                "activity": (
                    "Gateway reports process liveness; the sentinel listener emits the same processing "
                    "and tool activity signals as the pre-Gateway setup."
                ),
                "tools": "Tool telemetry comes from claude_agent_v2.py/Hermes callbacks, not a one-shot bridge.",
            },
        },
        "sentinel_cli": {
            "id": "sentinel_cli",
            "label": "Sentinel CLI",
            "description": (
                "Gateway-owned listener with the original sentinel CLI runner semantics: "
                "session resume, queueing, and parsed Claude/Codex tool activity."
            ),
            "kind": "builtin",
            "passive": False,
            "requires": [],
            "form_fields": [
                {
                    "name": "workdir",
                    "label": "Workdir",
                    "required": False,
                    "placeholder": str(repo_root),
                },
                {
                    "name": "model",
                    "label": "Model",
                    "required": False,
                    "placeholder": "opus or gpt-5.4",
                },
            ],
            "examples": [
                {
                    "label": "Claude sentinel",
                    "runtime_type": "sentinel_cli",
                    "workdir": str(repo_root),
                    "note": "Uses Claude CLI by default and resumes the same session for agent-level continuity.",
                },
            ],
            "signals": {
                **_shared_signals(),
                "activity": "Gateway parses Claude/Codex JSON streams and emits working, thinking, and tool phases.",
                "tools": "Codex command events are recorded as tool calls; Claude tool-use blocks are surfaced as live tool activity.",
            },
        },
        "claude_code_channel": {
            "id": "claude_code_channel",
            "label": "Claude Code Channel",
            "description": "Attached Claude Code live channel. Gateway registers identity; ax-channel owns delivery.",
            "kind": "attached_session",
            "passive": False,
            "requires": [],
            "form_fields": [
                {
                    "name": "workdir",
                    "label": "Workdir",
                    "required": True,
                    "placeholder": str(repo_root),
                },
            ],
            "examples": [
                {
                    "label": "Claude Code channel",
                    "runtime_type": "claude_code_channel",
                    "workdir": str(repo_root),
                    "note": "Run ax channel setup after Gateway registration, then launch Claude Code with ax-channel.",
                },
            ],
            "signals": {
                **_shared_signals(),
                "activity": "ax-channel emits working on delivery and completed after the Claude Code reply tool runs.",
                "tools": "Tool telemetry comes from the attached Claude Code session and channel integration.",
            },
        },
        "inbox": {
            "id": "inbox",
            "label": "Passive Inbox",
            "description": "Passive Gateway-managed identity that receives and queues work without auto-replying.",
            "kind": "builtin",
            "passive": True,
            "requires": [],
            "form_fields": [],
            "examples": [],
            "signals": {
                **_shared_signals(),
                "activity": "Gateway reports queued state only. This runtime is passive by design.",
                "tools": "No tool-call telemetry. Inbox runtimes do not execute work.",
            },
        },
    }


def runtime_type_definition(runtime_type: str) -> dict[str, Any]:
    normalized = runtime_type.lower().strip()
    if normalized == "command":
        normalized = "exec"
    catalog = runtime_type_catalog()
    if normalized not in catalog:
        raise KeyError(runtime_type)
    return catalog[normalized]


def runtime_type_list() -> list[dict[str, Any]]:
    catalog = runtime_type_catalog()
    ordered_ids = ["echo", "exec", "hermes_sentinel", "sentinel_cli", "claude_code_channel", "inbox"]
    return [catalog[runtime_id] for runtime_id in ordered_ids if runtime_id in catalog]


def agent_template_catalog() -> dict[str, dict[str, Any]]:
    repo_root = _repo_root()
    skill_path = _gateway_setup_skill_path()
    runtime_signals = {
        key: runtime_type_definition(key)["signals"]
        for key in ("echo", "exec", "hermes_sentinel", "sentinel_cli", "claude_code_channel", "inbox")
    }
    return {
        "echo_test": {
            "id": "echo_test",
            "label": "Echo (Test)",
            "description": "Fastest way to prove the Gateway is connected and replying correctly.",
            "availability": "ready",
            "launchable": True,
            "runtime_type": "echo",
            "asset_class": "interactive_agent",
            "intake_model": "live_listener",
            "trigger_sources": ["direct_message"],
            "return_paths": ["inline_reply"],
            "telemetry_shape": "basic",
            "suggested_name": "echo-bot",
            "operator_summary": "Best first test. No local setup required.",
            "recommended_test_message": "gateway test ping",
            "what_you_need": [],
            "setup_skill": "gateway-agent-setup",
            "setup_skill_path": str(skill_path),
            "defaults": {
                "runtime_type": "echo",
            },
            "signals": runtime_signals["echo"],
            "advanced": {
                "adapter_label": "Built-in echo runtime",
                "supports_command_override": False,
            },
        },
        "ollama": {
            "id": "ollama",
            "label": "Ollama",
            "description": "Local model runtime managed by Gateway.",
            "availability": "ready",
            "launchable": True,
            "runtime_type": "exec",
            "asset_class": "interactive_agent",
            "intake_model": "launch_on_send",
            "trigger_sources": ["direct_message"],
            "return_paths": ["inline_reply"],
            "telemetry_shape": "basic",
            "suggested_name": "ollama-bot",
            "operator_summary": "Good for a local model with pickup, liveness, and streaming activity.",
            "recommended_test_message": "Reply naturally that the Gateway round trip worked, then mention which local model answered.",
            "what_you_need": [
                "Run a local Ollama server on this machine.",
                "Have at least one Ollama model pulled locally. Gateway can suggest an installed model when the server is reachable.",
            ],
            "setup_skill": "gateway-agent-setup",
            "setup_skill_path": str(skill_path),
            "defaults": {
                "runtime_type": "exec",
                "exec_command": "python3 examples/gateway_ollama/ollama_bridge.py",
                "workdir": str(repo_root),
            },
            "signals": runtime_signals["exec"],
            "advanced": {
                "adapter_label": "Gateway command bridge",
                "supports_command_override": True,
            },
        },
        "hermes": {
            "id": "hermes",
            "label": "Hermes Sentinel",
            "description": "Long-running Hermes coding sentinel managed by Gateway.",
            "availability": "ready",
            "launchable": True,
            "runtime_type": "hermes_sentinel",
            "asset_class": "interactive_agent",
            "intake_model": "live_listener",
            "trigger_sources": ["direct_message"],
            "return_paths": ["inline_reply"],
            "telemetry_shape": "rich",
            "suggested_name": "hermes-bot",
            "operator_summary": "Best path for a capable coding agent with continuity and rich progress.",
            "recommended_test_message": "Remember the word cobalt, reply briefly, then I will ask you what word I gave you.",
            "what_you_need": [
                "A local hermes-agent checkout, usually at ~/hermes-agent or via HERMES_REPO_PATH.",
                "Hermes auth or model credentials such as ~/.hermes/auth.json or provider env vars.",
            ],
            "setup_skill": "gateway-agent-setup",
            "setup_skill_path": str(skill_path),
            "defaults": {
                "runtime_type": "hermes_sentinel",
                "workdir": str(repo_root),
            },
            "signals": runtime_signals["hermes_sentinel"],
            "advanced": {
                "adapter_label": "Gateway-supervised Hermes listener",
                "supports_command_override": False,
            },
        },
        "sentinel_cli": {
            "id": "sentinel_cli",
            "label": "Sentinel CLI",
            "description": "Original aX sentinel runner pattern managed by Gateway.",
            "availability": "ready",
            "launchable": True,
            "runtime_type": "sentinel_cli",
            "asset_class": "interactive_agent",
            "intake_model": "live_listener",
            "trigger_sources": ["direct_message"],
            "return_paths": ["inline_reply"],
            "telemetry_shape": "rich",
            "suggested_name": "dev-sentinel",
            "operator_summary": "Best fit for long-lived coding sentinels that need session continuity and tool activity.",
            "recommended_test_message": "Remember the word cobalt, reply briefly, then I will ask you what word I gave you.",
            "what_you_need": [
                "Claude CLI or Codex CLI installed and authenticated on this machine.",
                "A workdir containing the sentinel's local instructions.",
            ],
            "setup_skill": "gateway-agent-setup",
            "setup_skill_path": str(skill_path),
            "defaults": {
                "runtime_type": "sentinel_cli",
                "workdir": str(repo_root),
            },
            "signals": runtime_signals["sentinel_cli"],
            "advanced": {
                "adapter_label": "Gateway sentinel CLI runner",
                "supports_command_override": False,
            },
        },
        "service_account": {
            "id": "service_account",
            "label": "Service Account",
            "description": "Named sender identity for Gateway notifications, reminders, alerts, and operator-authored probes.",
            "availability": "ready",
            "launchable": True,
            "runtime_type": "inbox",
            "asset_class": "service_account",
            "intake_model": "notification_source",
            "worker_model": "no_runtime",
            "trigger_sources": ["manual_message", "automation", "scheduled_job"],
            "return_paths": ["outbound_message"],
            "telemetry_shape": "basic",
            "suggested_name": "notifications",
            "operator_summary": "Best fit for sending messages from a named automation or notification source.",
            "recommended_test_message": "Service account delivery check.",
            "what_you_need": [],
            "setup_skill": "gateway-agent-setup",
            "setup_skill_path": str(skill_path),
            "defaults": {
                "runtime_type": "inbox",
                "workdir": str(repo_root),
            },
            "signals": {
                **runtime_signals["inbox"],
                "delivery": "Gateway sends messages as this named service identity.",
                "liveness": "Service accounts are not live agents and are not expected to reply.",
                "activity": "Gateway reports sent, queued, and automation activity for this identity.",
                "tools": "No tool telemetry. Service accounts represent sources, not tool-running agents.",
            },
            "advanced": {
                "adapter_label": "Gateway service account",
                "supports_command_override": False,
            },
        },
        "claude_code_channel": {
            "id": "claude_code_channel",
            "label": "Claude Code Channel",
            "description": "Live Claude Code session bridged through aX channel delivery.",
            "availability": "ready",
            "launchable": True,
            "runtime_type": "claude_code_channel",
            "asset_class": "interactive_agent",
            "intake_model": "live_listener",
            "trigger_sources": ["direct_message"],
            "return_paths": ["inline_reply"],
            "telemetry_shape": "basic",
            "suggested_name": "cc-channel",
            "operator_summary": "Gateway-registered Claude Code live channel. Gateway owns identity; ax-channel owns delivery.",
            "recommended_test_message": "Reply with exactly: Gateway test OK.",
            "what_you_need": [
                "Claude Code with development channels enabled.",
                "Run `ax channel setup <agent>` after Gateway registration to write .mcp.json.",
            ],
            "setup_skill": "gateway-agent-setup",
            "setup_skill_path": str(skill_path),
            "defaults": {
                "runtime_type": "claude_code_channel",
                "start": False,
            },
            "signals": runtime_signals["claude_code_channel"],
            "advanced": {
                "adapter_label": "Gateway-registered ax-channel",
                "supports_command_override": False,
            },
        },
        "pass_through": {
            "id": "pass_through",
            "label": "Pass-through",
            "description": "Polling mailbox identity for agents that check in through Gateway instead of listening live.",
            "availability": "ready",
            "launchable": True,
            "runtime_type": "inbox",
            "asset_class": "interactive_agent",
            "intake_model": "polling_mailbox",
            "worker_model": "agent_check_in",
            "trigger_sources": ["mailbox_poll", "manual_check"],
            "return_paths": ["manual_reply", "summary_post"],
            "telemetry_shape": "basic",
            "suggested_name": "pass-through",
            "operator_summary": "Best fit for attached agents that need an inbox without pretending to be always online.",
            "recommended_test_message": "Mailbox check: acknowledge this when you next poll Gateway.",
            "what_you_need": [
                "A local agent workspace. Gateway fingerprints the folder, launch spec, agent identity, and Gateway id.",
                "Operator approval before this mailbox identity can pass work through.",
            ],
            "setup_skill": "gateway-agent-setup",
            "setup_skill_path": str(skill_path),
            "defaults": {
                "runtime_type": "inbox",
                "workdir": str(repo_root),
            },
            "requires_approval": True,
            "signals": {
                **runtime_signals["inbox"],
                "delivery": "Gateway stores inbound work in the mailbox until the agent checks it.",
                "liveness": "Gateway shows approval and mailbox state. The agent is not treated as a live listener.",
                "activity": "Mailbox depth and manual check-ins are the primary activity signal.",
                "tools": "No automatic tool telemetry. Tool activity belongs to the checking agent session.",
            },
            "advanced": {
                "adapter_label": "Gateway pass-through mailbox",
                "supports_command_override": False,
            },
        },
        "inbox": {
            "id": "inbox",
            "label": "Passive Inbox",
            "description": "Passive receiver identity for queue demos, operator flows, and non-replying endpoints.",
            "availability": "advanced",
            "launchable": True,
            "runtime_type": "inbox",
            "asset_class": "background_worker",
            "intake_model": "queue_accept",
            "worker_model": "queue_drain",
            "trigger_sources": ["queued_job", "manual_trigger"],
            "return_paths": ["summary_post"],
            "telemetry_shape": "basic",
            "suggested_name": "inbox-bot",
            "operator_summary": "Advanced testing and operator-only flow.",
            "recommended_test_message": "Queue this test job, mark it received, and do not reply inline.",
            "what_you_need": [],
            "setup_skill": "gateway-agent-setup",
            "setup_skill_path": str(skill_path),
            "defaults": {
                "runtime_type": "inbox",
            },
            "signals": runtime_signals["inbox"],
            "advanced": {
                "adapter_label": "Built-in passive inbox runtime",
                "supports_command_override": False,
            },
        },
    }


def agent_template_definition(template_id: str) -> dict[str, Any]:
    normalized = template_id.lower().strip()
    if normalized == "echo":
        normalized = "echo_test"
    catalog = agent_template_catalog()
    if normalized not in catalog:
        raise KeyError(template_id)
    return catalog[normalized]


def agent_template_list(*, include_advanced: bool = False) -> list[dict[str, Any]]:
    catalog = agent_template_catalog()
    ordered_ids = [
        "hermes",
        "ollama",
        "echo_test",
        "service_account",
        "pass_through",
        "sentinel_cli",
        "claude_code_channel",
        "inbox",
    ]
    templates = [catalog[template_id] for template_id in ordered_ids if template_id in catalog]
    if include_advanced:
        return templates
    return [item for item in templates if str(item.get("availability") or "") != "advanced"]
