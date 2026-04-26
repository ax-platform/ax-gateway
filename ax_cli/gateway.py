"""Local Gateway runtime and state management.

The Gateway is a local control-plane daemon that owns bootstrap and agent
credentials, supervises managed runtimes, and keeps lightweight desired vs
effective state in a registry file. The first slice intentionally uses
filesystem state plus a foreground daemon so it can ship quickly without
introducing a second backend.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import queue
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from .client import AxClient
from .commands.listen import (
    _is_self_authored,
    _iter_sse,
    _remember_reply_anchor,
    _should_respond,
    _strip_mention,
)
from .config import _global_config_dir

RuntimeLogger = Callable[[str], None]

REPLY_ANCHOR_MAX = 500
SEEN_IDS_MAX = 500
DEFAULT_QUEUE_SIZE = 50
DEFAULT_ACTIVITY_LIMIT = 10
DEFAULT_HANDLER_TIMEOUT_SECONDS = 900
MIN_HANDLER_TIMEOUT_SECONDS = 1
SSE_IDLE_TIMEOUT_SECONDS = 45.0
RUNTIME_STALE_AFTER_SECONDS = 75.0
GATEWAY_EVENT_PREFIX = "AX_GATEWAY_EVENT "
DEFAULT_OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
ENV_DENYLIST = {
    "AX_AGENT_ID",
    "AX_AGENT_NAME",
    "AX_BASE_URL",
    "AX_CONFIG_FILE",
    "AX_ENV",
    "AX_SPACE_ID",
    "AX_TOKEN",
    "AX_TOKEN_FILE",
    "AX_USER_BASE_URL",
    "AX_USER_ENV",
    "AX_USER_TOKEN",
}


class GatewayRuntimeTimeoutError(TimeoutError):
    """Raised when a managed runtime exceeds its per-message timeout."""

    def __init__(self, timeout_seconds: int, *, runtime_type: str | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.runtime_type = runtime_type
        label = f" {runtime_type}" if runtime_type else ""
        super().__init__(f"Gateway{label} runtime timed out after {timeout_seconds}s.")


_ACTIVITY_LOCK = threading.Lock()
_GATEWAY_PROCESS_RE = re.compile(
    r"(?:uv\s+run\s+ax\s+gateway\s+run|(?:^|\s).+?/ax(?:ctl)?\s+gateway\s+run(?:\s|$)|-m\s+ax_cli\.main\s+gateway\s+run(?:\s|$))"
)
_GATEWAY_UI_PROCESS_RE = re.compile(
    r"(?:uv\s+run\s+ax\s+gateway\s+ui|(?:^|\s).+?/ax(?:ctl)?\s+gateway\s+ui(?:\s|$)|-m\s+ax_cli\.main\s+gateway\s+ui(?:\s|$))"
)

_CONTROLLED_PLACEMENTS = {"hosted", "attached", "brokered", "mailbox"}
_CONTROLLED_ACTIVATIONS = {"persistent", "on_demand", "attach_only", "queue_worker"}
_CONTROLLED_LIVENESS = {"connected", "stale", "offline", "setup_error"}
_CONTROLLED_WORK_STATES = {"idle", "queued", "working", "blocked"}
_CONTROLLED_REPLY_MODES = {"interactive", "background", "summary_only", "silent"}
_CONTROLLED_TELEMETRY_LEVELS = {"rich", "basic", "silent"}
_CONTROLLED_ASSET_CLASSES = {
    "interactive_agent",
    "background_worker",
    "scheduled_job",
    "alert_listener",
    "service_proxy",
}
_CONTROLLED_INTAKE_MODELS = {
    "live_listener",
    "launch_on_send",
    "queue_accept",
    "queue_drain",
    "scheduled_run",
    "event_triggered",
    "manual_only",
}
_CONTROLLED_TRIGGER_SOURCES = {
    "direct_message",
    "queued_job",
    "scheduled_invocation",
    "external_alert",
    "manual_trigger",
    "tool_call",
}
_CONTROLLED_RETURN_PATHS = {"inline_reply", "sender_inbox", "summary_post", "task_update", "event_log", "silent"}
_CONTROLLED_TELEMETRY_SHAPES = {"rich", "basic", "heartbeat_only", "opaque"}
_CONTROLLED_WORKER_MODELS = {"queue_drain"}
_CONTROLLED_ATTESTATION_STATES = {"verified", "drifted", "unknown", "blocked"}
_CONTROLLED_APPROVAL_STATES = {"not_required", "pending", "approved", "rejected"}
_CONTROLLED_IDENTITY_STATUSES = {
    "verified",
    "unknown_identity",
    "credential_mismatch",
    "fallback_blocked",
    "bootstrap_only",
    "blocked",
}
_CONTROLLED_SPACE_STATUSES = {"active_allowed", "active_not_allowed", "no_active_space", "unknown"}
_CONTROLLED_ENVIRONMENT_STATUSES = {
    "environment_allowed",
    "environment_mismatch",
    "environment_unknown",
    "environment_blocked",
}
_CONTROLLED_ACTIVE_SPACE_SOURCES = {"explicit_request", "gateway_binding", "visible_default", "none"}
_CONTROLLED_MODES = {"LIVE", "ON-DEMAND", "INBOX"}
_CONTROLLED_PRESENCE = {"IDLE", "QUEUED", "WORKING", "BLOCKED", "STALE", "OFFLINE", "ERROR"}
_CONTROLLED_REPLY = {"REPLY", "SUMMARY", "SILENT"}
_CONTROLLED_CONFIDENCE = {"HIGH", "MEDIUM", "LOW", "BLOCKED"}
_CONTROLLED_REACHABILITY = {"live_now", "queue_available", "launch_available", "attach_required", "unavailable"}
_CONTROLLED_CONFIDENCE_REASONS = {
    "live_now",
    "queue_available",
    "launch_available",
    "attach_required",
    "unavailable",
    "setup_blocked",
    "recent_test_failed",
    "completion_degraded",
    "approval_required",
    "binding_drift",
    "new_gateway",
    "unknown_asset",
    "asset_mismatch",
    "approval_denied",
    "identity_unbound",
    "identity_mismatch",
    "fallback_blocked",
    "bootstrap_only",
    "active_space_not_allowed",
    "no_active_space",
    "space_unknown",
    "environment_mismatch",
    "unknown",
    "other",
}
_WORKING_STATUSES = {
    "accepted",
    "started",
    "processing",
    "thinking",
    "tool_call",
    "tool_started",
    "streaming",
    "working",
}
_BLOCKED_STATUSES = {"rate_limited"}


def _normalized_controlled(value: object, allowed: set[str], *, fallback: str) -> str:
    normalized = str(value or "").strip()
    if normalized in allowed:
        return normalized
    lowered_map = {item.lower(): item for item in allowed}
    lowered = normalized.lower()
    if lowered in lowered_map:
        return lowered_map[lowered]
    return fallback


def _normalized_controlled_list(value: object, allowed: set[str], *, fallback: list[str]) -> list[str]:
    raw_items: list[str] = []
    if isinstance(value, str):
        parts = value.split(",") if "," in value else [value]
        raw_items = [part.strip() for part in parts if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        raw_items = [str(item).strip() for item in value if str(item).strip()]

    lowered_map = {item.lower(): item for item in allowed}
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        candidate = item if item in allowed else lowered_map.get(item.lower())
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized or list(fallback)


def _normalized_optional_controlled(value: object, allowed: set[str]) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if normalized in allowed:
        return normalized
    lowered_map = {item.lower(): item for item in allowed}
    return lowered_map.get(normalized.lower())


def _normalized_string_list(value: object, *, fallback: list[str]) -> list[str]:
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
        return items or list(fallback)
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return items or list(fallback)
    return list(fallback)


def _bool_with_fallback(value: object, *, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off"}:
            return False
    return fallback


def _override_fields(snapshot: dict[str, Any], *, domain: str) -> set[str]:
    names: set[str] = set()
    nested = snapshot.get("user_overrides")
    if isinstance(nested, dict):
        scoped = nested.get(domain)
        if isinstance(scoped, dict):
            names.update(str(key).strip() for key in scoped.keys() if str(key).strip())
        elif isinstance(scoped, (list, tuple, set)):
            names.update(str(item).strip() for item in scoped if str(item).strip())

    direct_key = f"{domain}_overrides"
    direct = snapshot.get(direct_key)
    if isinstance(direct, dict):
        names.update(str(key).strip() for key in direct.keys() if str(key).strip())
    elif isinstance(direct, (list, tuple, set)):
        names.update(str(item).strip() for item in direct if str(item).strip())
    return names


def _template_operator_defaults(template_id: str | None, runtime_type: object) -> dict[str, str]:
    template_key = str(template_id or "").strip().lower()
    runtime_key = str(runtime_type or "").strip().lower()
    defaults_by_template = {
        "echo_test": {
            "placement": "hosted",
            "activation": "persistent",
            "reply_mode": "interactive",
            "telemetry_level": "basic",
        },
        "ollama": {
            "placement": "hosted",
            "activation": "on_demand",
            "reply_mode": "interactive",
            "telemetry_level": "basic",
        },
        "hermes": {
            "placement": "hosted",
            "activation": "persistent",
            "reply_mode": "interactive",
            "telemetry_level": "rich",
        },
        "sentinel_cli": {
            "placement": "hosted",
            "activation": "persistent",
            "reply_mode": "interactive",
            "telemetry_level": "rich",
        },
        "claude_code_channel": {
            "placement": "attached",
            "activation": "attach_only",
            "reply_mode": "interactive",
            "telemetry_level": "basic",
        },
        "inbox": {
            "placement": "mailbox",
            "activation": "queue_worker",
            "reply_mode": "summary_only",
            "telemetry_level": "basic",
        },
    }
    defaults_by_runtime = {
        "echo": {
            "placement": "hosted",
            "activation": "persistent",
            "reply_mode": "interactive",
            "telemetry_level": "basic",
        },
        "exec": {
            "placement": "hosted",
            "activation": "persistent",
            "reply_mode": "interactive",
            "telemetry_level": "basic",
        },
        "hermes_sentinel": {
            "placement": "hosted",
            "activation": "persistent",
            "reply_mode": "interactive",
            "telemetry_level": "rich",
        },
        "sentinel_cli": {
            "placement": "hosted",
            "activation": "persistent",
            "reply_mode": "interactive",
            "telemetry_level": "rich",
        },
        "inbox": {
            "placement": "mailbox",
            "activation": "queue_worker",
            "reply_mode": "summary_only",
            "telemetry_level": "basic",
        },
    }
    return dict(
        defaults_by_template.get(template_key) or defaults_by_runtime.get(runtime_key) or defaults_by_runtime["exec"]
    )


def _template_asset_defaults(template_id: str | None, runtime_type: object) -> dict[str, Any]:
    template_key = str(template_id or "").strip().lower()
    runtime_key = str(runtime_type or "").strip().lower()
    defaults_by_template: dict[str, dict[str, Any]] = {
        "echo_test": {
            "asset_class": "interactive_agent",
            "intake_model": "live_listener",
            "trigger_sources": ["direct_message"],
            "return_paths": ["inline_reply"],
            "telemetry_shape": "basic",
            "worker_model": None,
            "addressable": True,
            "messageable": True,
            "schedulable": False,
            "externally_triggered": False,
            "tags": ["local", "live-listener", "test-agent"],
            "capabilities": ["reply"],
            "constraints": [],
        },
        "ollama": {
            "asset_class": "interactive_agent",
            "intake_model": "launch_on_send",
            "trigger_sources": ["direct_message"],
            "return_paths": ["inline_reply"],
            "telemetry_shape": "basic",
            "worker_model": None,
            "addressable": True,
            "messageable": True,
            "schedulable": False,
            "externally_triggered": False,
            "tags": ["local", "on-demand", "cold-start"],
            "capabilities": ["reply"],
            "constraints": ["requires-model"],
        },
        "hermes": {
            "asset_class": "interactive_agent",
            "intake_model": "live_listener",
            "trigger_sources": ["direct_message"],
            "return_paths": ["inline_reply"],
            "telemetry_shape": "rich",
            "worker_model": None,
            "addressable": True,
            "messageable": True,
            "schedulable": False,
            "externally_triggered": False,
            "tags": ["local", "live-listener", "hosted-by-gateway", "rich-telemetry", "repo-bound"],
            "capabilities": ["reply", "progress", "tool_events"],
            "constraints": ["requires-repo", "requires-provider-auth"],
        },
        "sentinel_cli": {
            "asset_class": "interactive_agent",
            "intake_model": "live_listener",
            "trigger_sources": ["direct_message"],
            "return_paths": ["inline_reply"],
            "telemetry_shape": "rich",
            "worker_model": None,
            "addressable": True,
            "messageable": True,
            "schedulable": False,
            "externally_triggered": False,
            "tags": ["local", "live-listener", "hosted-by-gateway", "sentinel-cli", "rich-telemetry"],
            "capabilities": ["reply", "progress", "tool_events", "session_resume"],
            "constraints": ["requires-cli-auth"],
        },
        "claude_code_channel": {
            "asset_class": "interactive_agent",
            "intake_model": "live_listener",
            "trigger_sources": ["direct_message"],
            "return_paths": ["inline_reply"],
            "telemetry_shape": "basic",
            "worker_model": None,
            "addressable": True,
            "messageable": True,
            "schedulable": False,
            "externally_triggered": False,
            "tags": ["attached-session", "live-listener", "basic-telemetry"],
            "capabilities": ["reply"],
            "constraints": ["requires-attached-session"],
        },
        "inbox": {
            "asset_class": "background_worker",
            "intake_model": "queue_accept",
            "trigger_sources": ["queued_job", "manual_trigger"],
            "return_paths": ["summary_post"],
            "telemetry_shape": "basic",
            "worker_model": "queue_drain",
            "addressable": True,
            "messageable": True,
            "schedulable": False,
            "externally_triggered": False,
            "tags": ["queue-backed", "summary-later"],
            "capabilities": ["queue_work", "post_summary"],
            "constraints": [],
        },
    }
    defaults_by_runtime: dict[str, dict[str, Any]] = {
        "echo": defaults_by_template["echo_test"],
        "exec": {
            "asset_class": "interactive_agent",
            "intake_model": "live_listener",
            "trigger_sources": ["direct_message"],
            "return_paths": ["inline_reply"],
            "telemetry_shape": "basic",
            "worker_model": None,
            "addressable": True,
            "messageable": True,
            "schedulable": False,
            "externally_triggered": False,
            "tags": ["local", "custom-bridge"],
            "capabilities": ["reply"],
            "constraints": [],
        },
        "hermes_sentinel": defaults_by_template["hermes"],
        "sentinel_cli": defaults_by_template["sentinel_cli"],
        "inbox": defaults_by_template["inbox"],
    }
    resolved = (
        defaults_by_template.get(template_key) or defaults_by_runtime.get(runtime_key) or defaults_by_runtime["exec"]
    )
    return {
        "asset_class": resolved["asset_class"],
        "intake_model": resolved["intake_model"],
        "trigger_sources": list(resolved["trigger_sources"]),
        "return_paths": list(resolved["return_paths"]),
        "telemetry_shape": resolved["telemetry_shape"],
        "worker_model": resolved.get("worker_model"),
        "addressable": bool(resolved.get("addressable", True)),
        "messageable": bool(resolved.get("messageable", True)),
        "schedulable": bool(resolved.get("schedulable", False)),
        "externally_triggered": bool(resolved.get("externally_triggered", False)),
        "tags": list(resolved.get("tags", [])),
        "capabilities": list(resolved.get("capabilities", [])),
        "constraints": list(resolved.get("constraints", [])),
    }


def _asset_type_label(*, asset_class: str, intake_model: str, worker_model: str | None = None) -> str:
    if asset_class == "interactive_agent":
        if intake_model == "live_listener":
            return "Live Listener"
        if intake_model == "launch_on_send":
            return "On-Demand Agent"
    if asset_class == "background_worker":
        if intake_model == "queue_accept" or worker_model == "queue_drain":
            return "Inbox Worker"
        return "Background Worker"
    if asset_class == "scheduled_job":
        return "Scheduled Job"
    if asset_class == "alert_listener":
        return "Alert Listener"
    if asset_class == "service_proxy":
        return "Service / Tool Proxy"
    return "Connected Asset"


def _output_label(return_paths: list[str]) -> str:
    primary = return_paths[0] if return_paths else "inline_reply"
    return {
        "inline_reply": "Reply",
        "sender_inbox": "Inbox",
        "summary_post": "Summary",
        "task_update": "Task",
        "event_log": "Event Log",
        "silent": "Silent",
    }.get(primary, "Reply")


def infer_asset_descriptor(
    snapshot: dict[str, Any], *, operator_profile: dict[str, str] | None = None
) -> dict[str, Any]:
    defaults = _template_asset_defaults(
        str(snapshot.get("template_id") or "").strip() or None, snapshot.get("runtime_type")
    )
    overrides = _override_fields(snapshot, domain="asset")
    telemetry_fallback = defaults["telemetry_shape"]
    if operator_profile:
        telemetry_fallback = {
            "rich": "rich",
            "basic": "basic",
            "silent": "opaque",
        }.get(operator_profile.get("telemetry_level", ""), telemetry_fallback)

    asset_class = defaults["asset_class"]
    if "asset_class" in overrides:
        asset_class = _normalized_controlled(
            snapshot.get("asset_class"), _CONTROLLED_ASSET_CLASSES, fallback=defaults["asset_class"]
        )

    intake_model = defaults["intake_model"]
    if "intake_model" in overrides:
        intake_model = _normalized_controlled(
            snapshot.get("intake_model"), _CONTROLLED_INTAKE_MODELS, fallback=defaults["intake_model"]
        )

    worker_model = defaults.get("worker_model")
    if "worker_model" in overrides:
        worker_model = _normalized_optional_controlled(
            snapshot.get("worker_model"), _CONTROLLED_WORKER_MODELS
        ) or defaults.get("worker_model")

    trigger_sources = list(defaults["trigger_sources"])
    if "trigger_sources" in overrides or "trigger_source" in overrides:
        trigger_sources = _normalized_controlled_list(
            snapshot.get("trigger_sources")
            if snapshot.get("trigger_sources") is not None
            else snapshot.get("trigger_source"),
            _CONTROLLED_TRIGGER_SOURCES,
            fallback=defaults["trigger_sources"],
        )

    return_paths = list(defaults["return_paths"])
    if "return_paths" in overrides or "return_path" in overrides:
        return_paths = _normalized_controlled_list(
            snapshot.get("return_paths") if snapshot.get("return_paths") is not None else snapshot.get("return_path"),
            _CONTROLLED_RETURN_PATHS,
            fallback=defaults["return_paths"],
        )

    telemetry_shape = telemetry_fallback
    if "telemetry_shape" in overrides:
        telemetry_shape = _normalized_controlled(
            snapshot.get("telemetry_shape"),
            _CONTROLLED_TELEMETRY_SHAPES,
            fallback=telemetry_fallback,
        )

    tags = list(defaults["tags"])
    if "tags" in overrides:
        tags = _normalized_string_list(snapshot.get("tags"), fallback=defaults["tags"])

    capabilities = list(defaults["capabilities"])
    if "capabilities" in overrides:
        capabilities = _normalized_string_list(snapshot.get("capabilities"), fallback=defaults["capabilities"])

    constraints = list(defaults["constraints"])
    if "constraints" in overrides:
        constraints = _normalized_string_list(snapshot.get("constraints"), fallback=defaults["constraints"])

    descriptor = {
        "asset_id": str(snapshot.get("asset_id") or snapshot.get("agent_id") or snapshot.get("name") or "").strip()
        or None,
        "gateway_id": str(snapshot.get("gateway_id") or "").strip() or None,
        "display_name": str(
            snapshot.get("display_name")
            or snapshot.get("name")
            or snapshot.get("template_label")
            or snapshot.get("runtime_type")
            or "Managed Asset"
        ),
        "asset_class": asset_class,
        "intake_model": intake_model,
        "worker_model": worker_model,
        "trigger_sources": trigger_sources,
        "return_paths": return_paths,
        "telemetry_shape": telemetry_shape,
        "addressable": _bool_with_fallback(snapshot.get("addressable"), fallback=defaults["addressable"])
        if "addressable" in overrides
        else defaults["addressable"],
        "messageable": _bool_with_fallback(snapshot.get("messageable"), fallback=defaults["messageable"])
        if "messageable" in overrides
        else defaults["messageable"],
        "schedulable": _bool_with_fallback(snapshot.get("schedulable"), fallback=defaults["schedulable"])
        if "schedulable" in overrides
        else defaults["schedulable"],
        "externally_triggered": _bool_with_fallback(
            snapshot.get("externally_triggered"), fallback=defaults["externally_triggered"]
        )
        if "externally_triggered" in overrides
        else defaults["externally_triggered"],
        "tags": tags,
        "capabilities": capabilities,
        "constraints": constraints,
    }
    descriptor["type_label"] = _asset_type_label(
        asset_class=descriptor["asset_class"],
        intake_model=descriptor["intake_model"],
        worker_model=descriptor.get("worker_model"),
    )
    descriptor["output_label"] = _output_label(descriptor["return_paths"])
    descriptor["primary_trigger_source"] = descriptor["trigger_sources"][0] if descriptor["trigger_sources"] else None
    descriptor["primary_return_path"] = descriptor["return_paths"][0] if descriptor["return_paths"] else None
    return descriptor


def _hermes_repo_candidates(entry: dict[str, Any] | None = None) -> list[Path]:
    entry = entry or {}
    candidates: list[Path] = []
    seen: set[str] = set()

    def add(path_value: object) -> None:
        raw = str(path_value or "").strip()
        if not raw:
            return
        expanded = Path(raw).expanduser()
        key = str(expanded)
        if key in seen:
            return
        seen.add(key)
        candidates.append(expanded)

    add(entry.get("hermes_repo_path"))
    add(os.environ.get("HERMES_REPO_PATH"))

    workdir_raw = str(entry.get("workdir") or "").strip()
    if workdir_raw:
        workdir = Path(workdir_raw).expanduser()
        add(workdir.parent / "hermes-agent")
        if str(workdir).startswith("/home/ax-agent/agents"):
            add("/home/ax-agent/shared/repos/hermes-agent")

    add(Path.home() / "hermes-agent")
    return candidates


def hermes_setup_status(entry: dict[str, Any]) -> dict[str, Any]:
    template_id = str(entry.get("template_id") or "").strip().lower()
    runtime_type = str(entry.get("runtime_type") or "").strip().lower()
    if template_id != "hermes" and runtime_type != "hermes_sentinel":
        return {"ready": True, "template_id": template_id}

    candidates = _hermes_repo_candidates(entry)
    resolved = next((candidate for candidate in candidates if candidate.exists()), None)
    if resolved is not None:
        return {
            "ready": True,
            "template_id": template_id,
            "resolved_path": str(resolved),
            "summary": f"Hermes checkout found at {resolved}.",
        }

    expected = candidates[0] if candidates else (Path.home() / "hermes-agent")
    return {
        "ready": False,
        "template_id": template_id,
        "resolved_path": None,
        "expected_path": str(expected),
        "summary": f"Hermes checkout not found at {expected}.",
        "detail": (
            f"Hermes checkout not found at {expected}. Set HERMES_REPO_PATH or clone hermes-agent to ~/hermes-agent."
        ),
    }


def _ollama_model_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    models = payload.get("models")
    if not isinstance(models, list):
        return rows
    for item in models:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("model") or "").strip()
        if not name:
            continue
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        families = details.get("families") if isinstance(details.get("families"), list) else []
        family_values = [str(value).strip() for value in families if str(value).strip()]
        remote_host = str(item.get("remote_host") or "").strip() or None
        lowered_name = name.lower()
        is_embedding = "embed" in lowered_name or any("bert" in family.lower() for family in family_values)
        rows.append(
            {
                "name": name,
                "family": str(details.get("family") or "").strip() or None,
                "families": family_values,
                "parameter_size": str(details.get("parameter_size") or "").strip() or None,
                "modified_at": str(item.get("modified_at") or "").strip() or None,
                "remote_host": remote_host,
                "is_cloud": bool(remote_host or lowered_name.endswith(":cloud") or lowered_name.endswith("-cloud")),
                "is_embedding": is_embedding,
            }
        )
    return rows


def _recommended_ollama_model(rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return None

    def pick(candidates: list[dict[str, Any]]) -> str | None:
        if not candidates:
            return None
        ordered = sorted(
            candidates,
            key=lambda item: (
                str(item.get("modified_at") or ""),
                str(item.get("parameter_size") or ""),
                str(item.get("name") or ""),
            ),
            reverse=True,
        )
        return str(ordered[0].get("name") or "").strip() or None

    local_rows = [item for item in rows if not bool(item.get("is_cloud"))]
    local_chat_rows = [item for item in local_rows if not bool(item.get("is_embedding"))]
    chat_rows = [item for item in rows if not bool(item.get("is_embedding"))]
    return pick(local_chat_rows) or pick(local_rows) or pick(chat_rows) or pick(rows)


def ollama_setup_status(*, preferred_model: str | None = None) -> dict[str, Any]:
    base_url = DEFAULT_OLLAMA_BASE_URL
    endpoint = f"{base_url}/api/tags"
    preferred = str(preferred_model or "").strip() or None
    try:
        response = httpx.get(endpoint, timeout=3.0)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Ollama returned a non-object response.")
    except Exception as exc:
        return {
            "ready": False,
            "server_reachable": False,
            "base_url": base_url,
            "endpoint": endpoint,
            "preferred_model": preferred,
            "preferred_model_available": False,
            "recommended_model": None,
            "available_models": [],
            "local_models": [],
            "models": [],
            "summary": f"Ollama server not reachable at {base_url}.",
            "detail": str(exc),
        }

    rows = _ollama_model_rows(payload)
    available_models = [str(item.get("name") or "") for item in rows if str(item.get("name") or "").strip()]
    local_models = [str(item.get("name") or "") for item in rows if not bool(item.get("is_cloud"))]
    preferred_available = bool(preferred and preferred in available_models)
    recommended_model = preferred if preferred_available else _recommended_ollama_model(rows)
    ready = bool(available_models)
    if preferred and not preferred_available:
        summary = f"Ollama is reachable, but {preferred} is not installed locally."
    elif recommended_model:
        summary = f"Ollama is reachable. Recommended model: {recommended_model}."
    elif available_models:
        summary = f"Ollama is reachable with {len(available_models)} model(s) available."
    else:
        summary = "Ollama is reachable, but no models are installed yet."
    return {
        "ready": ready,
        "server_reachable": True,
        "base_url": base_url,
        "endpoint": endpoint,
        "preferred_model": preferred,
        "preferred_model_available": preferred_available,
        "recommended_model": recommended_model,
        "available_models": available_models,
        "local_models": local_models,
        "models": rows,
        "summary": summary,
        "detail": None,
    }


def infer_operator_profile(snapshot: dict[str, Any]) -> dict[str, str]:
    defaults = _template_operator_defaults(
        str(snapshot.get("template_id") or "").strip() or None, snapshot.get("runtime_type")
    )
    overrides = _override_fields(snapshot, domain="operator")
    return {
        "placement": _normalized_controlled(
            snapshot.get("placement"), _CONTROLLED_PLACEMENTS, fallback=defaults["placement"]
        )
        if "placement" in overrides
        else defaults["placement"],
        "activation": _normalized_controlled(
            snapshot.get("activation"), _CONTROLLED_ACTIVATIONS, fallback=defaults["activation"]
        )
        if "activation" in overrides
        else defaults["activation"],
        "reply_mode": _normalized_controlled(
            snapshot.get("reply_mode"), _CONTROLLED_REPLY_MODES, fallback=defaults["reply_mode"]
        )
        if "reply_mode" in overrides
        else defaults["reply_mode"],
        "telemetry_level": _normalized_controlled(
            snapshot.get("telemetry_level"),
            _CONTROLLED_TELEMETRY_LEVELS,
            fallback=defaults["telemetry_level"],
        )
        if "telemetry_level" in overrides
        else defaults["telemetry_level"],
    }


def _looks_like_setup_error(snapshot: dict[str, Any], raw_state: str) -> bool:
    if raw_state == "error":
        return True
    last_error = str(snapshot.get("last_error") or "").lower()
    preview = str(snapshot.get("last_reply_preview") or "").lower()
    if "repo not found" in last_error or "repo not found" in preview:
        return True
    if preview.startswith("(stderr:") or last_error.startswith("stderr:"):
        return True
    return False


def _derive_liveness(snapshot: dict[str, Any], *, raw_state: str, last_seen_age: int | None) -> tuple[str, bool]:
    if _looks_like_setup_error(snapshot, raw_state):
        return "setup_error", False
    if raw_state == "running":
        if last_seen_age is None or last_seen_age > RUNTIME_STALE_AFTER_SECONDS:
            return "stale", False
        return "connected", True
    if raw_state in {"starting", "reconnecting", "stale"}:
        return "stale", False
    return "offline", False


def _derive_work_state(snapshot: dict[str, Any], *, liveness: str) -> str:
    attestation_state = _normalized_optional_controlled(
        snapshot.get("attestation_state"), _CONTROLLED_ATTESTATION_STATES
    )
    approval_state = _normalized_optional_controlled(snapshot.get("approval_state"), _CONTROLLED_APPROVAL_STATES)
    identity_status = _normalized_optional_controlled(snapshot.get("identity_status"), _CONTROLLED_IDENTITY_STATUSES)
    environment_status = _normalized_optional_controlled(
        snapshot.get("environment_status"), _CONTROLLED_ENVIRONMENT_STATUSES
    )
    space_status = _normalized_optional_controlled(snapshot.get("space_status"), _CONTROLLED_SPACE_STATUSES)
    if liveness == "setup_error":
        return "blocked"
    if attestation_state in {"drifted", "unknown", "blocked"} or approval_state in {"pending", "rejected"}:
        return "blocked"
    if identity_status in {"unknown_identity", "credential_mismatch", "fallback_blocked", "bootstrap_only", "blocked"}:
        return "blocked"
    if environment_status in {"environment_mismatch", "environment_blocked"}:
        return "blocked"
    if space_status in {"active_not_allowed", "no_active_space"}:
        return "blocked"
    status = str(snapshot.get("current_status") or "").strip().lower()
    backlog_depth = int(snapshot.get("backlog_depth") or 0)
    if status in _WORKING_STATUSES:
        return "working"
    if status == "queued" or backlog_depth > 0:
        return "queued"
    if status in _BLOCKED_STATUSES:
        return "blocked"
    return "idle"


def _doctor_has_failed(snapshot: dict[str, Any]) -> bool:
    result = snapshot.get("last_doctor_result")
    if not isinstance(result, dict):
        return False
    status = str(result.get("status") or "").strip().lower()
    if status in {"failed", "error"}:
        return True
    checks = result.get("checks")
    if isinstance(checks, list):
        return any(
            isinstance(item, dict) and str(item.get("status") or "").strip().lower() == "failed" for item in checks
        )
    return False


def _derive_mode(profile: dict[str, str]) -> str:
    if profile["placement"] == "mailbox":
        return "INBOX"
    if profile["activation"] in {"persistent", "attach_only"}:
        return "LIVE"
    return "ON-DEMAND"


def _derive_presence(*, mode: str, liveness: str, work_state: str) -> str:
    if liveness == "setup_error":
        return "ERROR"
    if work_state == "blocked":
        return "BLOCKED"
    if liveness == "stale":
        return "STALE"
    if liveness == "offline" and mode == "LIVE":
        return "OFFLINE"
    if work_state == "working":
        return "WORKING"
    if work_state == "queued":
        return "QUEUED"
    return "IDLE"


def _derive_reply(reply_mode: str) -> str:
    if reply_mode == "interactive":
        return "REPLY"
    if reply_mode == "silent":
        return "SILENT"
    return "SUMMARY"


def _derive_reachability(*, snapshot: dict[str, Any], mode: str, liveness: str, activation: str) -> str:
    attestation_state = _normalized_optional_controlled(
        snapshot.get("attestation_state"), _CONTROLLED_ATTESTATION_STATES
    )
    approval_state = _normalized_optional_controlled(snapshot.get("approval_state"), _CONTROLLED_APPROVAL_STATES)
    identity_status = _normalized_optional_controlled(snapshot.get("identity_status"), _CONTROLLED_IDENTITY_STATUSES)
    environment_status = _normalized_optional_controlled(
        snapshot.get("environment_status"), _CONTROLLED_ENVIRONMENT_STATUSES
    )
    space_status = _normalized_optional_controlled(snapshot.get("space_status"), _CONTROLLED_SPACE_STATUSES)
    if liveness == "setup_error":
        return "unavailable"
    if attestation_state in {"drifted", "unknown", "blocked"} or approval_state in {"pending", "rejected"}:
        return "unavailable"
    if identity_status in {"unknown_identity", "credential_mismatch", "fallback_blocked", "bootstrap_only", "blocked"}:
        return "unavailable"
    if environment_status in {"environment_mismatch", "environment_blocked"}:
        return "unavailable"
    if space_status in {"active_not_allowed", "no_active_space"}:
        return "unavailable"
    if mode == "INBOX":
        return "queue_available"
    if activation == "attach_only" and liveness in {"stale", "offline"}:
        return "attach_required"
    if mode == "LIVE" and liveness == "connected":
        return "live_now"
    if mode == "ON-DEMAND" and liveness != "setup_error":
        return "launch_available"
    return "unavailable"


def _setup_error_detail(snapshot: dict[str, Any]) -> str:
    if _doctor_has_failed(snapshot):
        summary = _doctor_summary(snapshot)
        if summary:
            return summary
    return str(
        snapshot.get("last_error")
        or snapshot.get("last_reply_preview")
        or "Setup must be fixed before Gateway can send work."
    )


def _doctor_summary(snapshot: dict[str, Any]) -> str:
    result = snapshot.get("last_doctor_result")
    if not isinstance(result, dict):
        return ""
    summary = str(result.get("summary") or "").strip()
    if summary:
        return summary
    checks = result.get("checks")
    if isinstance(checks, list):
        failed = [
            str(item.get("name") or "").strip()
            for item in checks
            if isinstance(item, dict) and str(item.get("status") or "").strip().lower() == "failed"
        ]
        if failed:
            return f"Doctor failed: {', '.join(filter(None, failed))}."
    return ""


def _derive_confidence(
    snapshot: dict[str, Any],
    *,
    mode: str,
    liveness: str,
    reachability: str,
) -> tuple[str, str, str]:
    attestation_state = _normalized_optional_controlled(
        snapshot.get("attestation_state"), _CONTROLLED_ATTESTATION_STATES
    )
    approval_state = _normalized_optional_controlled(snapshot.get("approval_state"), _CONTROLLED_APPROVAL_STATES)
    governance_reason = _normalized_optional_controlled(
        snapshot.get("confidence_reason"), _CONTROLLED_CONFIDENCE_REASONS
    )
    governance_detail = (
        str(snapshot.get("confidence_detail") or "").strip()
        or "Gateway blocked this runtime until its binding is approved."
    )
    identity_status = _normalized_optional_controlled(snapshot.get("identity_status"), _CONTROLLED_IDENTITY_STATUSES)
    environment_status = _normalized_optional_controlled(
        snapshot.get("environment_status"), _CONTROLLED_ENVIRONMENT_STATUSES
    )
    space_status = _normalized_optional_controlled(snapshot.get("space_status"), _CONTROLLED_SPACE_STATUSES)
    if liveness == "setup_error":
        return ("BLOCKED", "setup_blocked", _setup_error_detail(snapshot))
    if identity_status == "unknown_identity":
        return (
            "BLOCKED",
            "identity_unbound",
            "Gateway does not have a bound acting identity for this asset in the requested environment.",
        )
    if identity_status in {"credential_mismatch", "fallback_blocked"}:
        return (
            "BLOCKED",
            "identity_mismatch",
            "Gateway blocked a mismatched acting identity instead of borrowing another identity.",
        )
    if identity_status == "bootstrap_only":
        return (
            "BLOCKED",
            "bootstrap_only",
            "Gateway bootstrap credentials can only be used for setup, verification, or repair flows.",
        )
    if environment_status == "environment_mismatch":
        return (
            "BLOCKED",
            "environment_mismatch",
            "Requested environment does not match the bound Gateway environment for this asset.",
        )
    if environment_status == "environment_blocked":
        return ("BLOCKED", "environment_mismatch", "Gateway blocked this asset in the requested environment.")
    if space_status == "active_not_allowed":
        return (
            "BLOCKED",
            "active_space_not_allowed",
            "The resolved target space is not allowed for this acting identity.",
        )
    if space_status == "no_active_space":
        return ("BLOCKED", "no_active_space", "Gateway does not have an active space selected for this asset.")
    if space_status == "unknown":
        return ("LOW", "space_unknown", "Gateway could not verify the allowed-space list for this acting identity.")
    if approval_state == "rejected":
        return ("BLOCKED", governance_reason or "approval_denied", governance_detail)
    if attestation_state in {"blocked", "unknown", "drifted"} or approval_state == "pending":
        return ("BLOCKED", governance_reason or "approval_required", governance_detail)
    if _doctor_has_failed(snapshot):
        detail = _doctor_summary(snapshot) or "Gateway Doctor reported a failed send path."
        return ("LOW", "recent_test_failed", detail)
    completion_rate = snapshot.get("completion_rate")
    try:
        if completion_rate is not None and float(completion_rate) < 0.5:
            return ("LOW", "completion_degraded", "Recent completion rate is below the healthy threshold.")
    except (TypeError, ValueError):
        pass
    if mode == "INBOX":
        return ("HIGH", "queue_available", "Gateway can safely accept and queue work now.")
    if mode == "ON-DEMAND" and reachability == "launch_available":
        return ("MEDIUM", "launch_available", "Gateway can launch this runtime on send. Cold start possible.")
    if liveness in {"offline", "stale"}:
        if reachability == "attach_required":
            return ("LOW", "attach_required", "Reconnect the attached session before sending.")
        return ("LOW", "unavailable", "Gateway does not currently have a healthy live path.")
    if liveness == "connected":
        return ("HIGH", "live_now", "A live runtime is attached and ready to claim work.")
    return ("MEDIUM", "unknown", "Gateway has partial health signals but no stronger confidence signal yet.")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gateway_id_from_registry(registry: dict[str, Any]) -> str:
    gateway = registry.setdefault("gateway", {})
    gateway_id = str(gateway.get("gateway_id") or "").strip()
    if gateway_id:
        return gateway_id
    gateway_id = str(uuid.uuid4())
    gateway["gateway_id"] = gateway_id
    return gateway_id


def _asset_id_for_entry(entry: dict[str, Any]) -> str:
    return str(entry.get("agent_id") or entry.get("asset_id") or entry.get("name") or "").strip()


def _binding_type_for_entry(entry: dict[str, Any]) -> str:
    activation = str(entry.get("activation") or "").strip()
    if activation == "attach_only":
        return "attached_session"
    if activation == "queue_worker" or str(entry.get("runtime_type") or "").strip().lower() == "inbox":
        return "queue_worker"
    return "local_runtime"


def _launch_spec_for_entry(entry: dict[str, Any]) -> dict[str, Any]:
    launch_spec = {
        "runtime_type": str(entry.get("runtime_type") or "").strip() or None,
        "template_id": str(entry.get("template_id") or "").strip() or None,
        "command": str(entry.get("exec_command") or "").strip() or None,
        "workdir": str(entry.get("workdir") or "").strip() or None,
        "ollama_model": str(entry.get("ollama_model") or "").strip() or None,
        "transport": str(entry.get("transport") or "").strip() or None,
    }
    model = str(
        entry.get("hermes_model")
        or entry.get("sentinel_model")
        or entry.get("runtime_model")
        or entry.get("model")
        or ""
    ).strip()
    if model:
        launch_spec["model"] = model
    return launch_spec


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _host_fingerprint() -> str:
    host = platform.node() or "unknown-host"
    return f"host:{hashlib.sha256(host.encode('utf-8')).hexdigest()[:16]}"


def _normalized_base_url(value: object) -> str:
    return str(value or "").strip().rstrip("/")


def _environment_label_for_base_url(base_url: object) -> str:
    normalized = _normalized_base_url(base_url)
    if not normalized:
        return "unknown"
    parsed = urlparse(normalized if "://" in normalized else f"https://{normalized}")
    host = str(parsed.netloc or parsed.path or "").lower()
    if host == "paxai.app":
        return "prod"
    if host == "dev.paxai.app":
        return "dev"
    if host in {"localhost", "127.0.0.1"} or host.startswith("localhost:") or host.startswith("127.0.0.1:"):
        return "local"
    return host or "custom"


def _redacted_path(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        path = Path(raw).expanduser().resolve()
        home = Path.home().resolve()
        try:
            rel = path.relative_to(home)
            return str(Path("~") / rel)
        except ValueError:
            return str(path)
    except Exception:
        return raw


def _space_cache_rows(value: object) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    items = value if isinstance(value, list) else []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        space_id = str(item.get("space_id") or item.get("id") or "").strip()
        if not space_id or space_id in seen:
            continue
        seen.add(space_id)
        rows.append(
            {
                "space_id": space_id,
                "name": str(item.get("name") or item.get("space_name") or space_id),
                "is_default": bool(item.get("is_default", False)),
            }
        )
    return rows


def _space_name_from_cache(allowed_spaces: list[dict[str, Any]], space_id: str | None) -> str | None:
    if not space_id:
        return None
    for item in allowed_spaces:
        if str(item.get("space_id") or "") == str(space_id):
            return str(item.get("name") or space_id)
    return None


def _fallback_allowed_spaces(entry: dict[str, Any], session: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    session = session or {}
    default_id = str(entry.get("default_space_id") or entry.get("space_id") or session.get("space_id") or "").strip()
    active_id = str(entry.get("active_space_id") or entry.get("space_id") or default_id).strip()
    rows: list[dict[str, Any]] = []
    if default_id:
        rows.append(
            {
                "space_id": default_id,
                "name": str(
                    entry.get("default_space_name")
                    or entry.get("space_name")
                    or session.get("space_name")
                    or default_id
                ),
                "is_default": True,
            }
        )
    if active_id and active_id != default_id:
        rows.append(
            {
                "space_id": active_id,
                "name": str(entry.get("active_space_name") or entry.get("space_name") or active_id),
                "is_default": False,
            }
        )
    return _space_cache_rows(rows)


def _space_id_allowed(allowed_spaces: list[dict[str, Any]], space_id: str | None) -> bool:
    if not space_id:
        return False
    return any(str(item.get("space_id") or "") == str(space_id) for item in allowed_spaces)


def _binding_candidate_for_entry(entry: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    asset_id = _asset_id_for_entry(entry)
    install_id = str(entry.get("install_id") or "").strip() or str(uuid.uuid4())
    launch_spec = _launch_spec_for_entry(entry)
    workdir = str(entry.get("workdir") or "").strip() or None
    path = str(Path(workdir).expanduser()) if workdir else None
    candidate = {
        "asset_id": asset_id,
        "gateway_id": _gateway_id_from_registry(registry),
        "install_id": install_id,
        "binding_type": _binding_type_for_entry(entry),
        "path": path,
        "launch_spec": launch_spec,
        "launch_spec_hash": _payload_hash(launch_spec),
        "created_from": str(
            entry.get("created_from") or ("ax_template" if entry.get("template_id") else "custom_bridge")
        ),
        "created_via": str(entry.get("created_via") or "cli"),
        "approved_state": str(entry.get("approved_state") or "approved"),
        "first_seen_at": str(entry.get("first_seen_at") or _now_iso()),
        "last_verified_at": str(entry.get("last_verified_at") or _now_iso()),
    }
    candidate["candidate_signature"] = _payload_hash(
        {
            "asset_id": candidate["asset_id"],
            "gateway_id": candidate["gateway_id"],
            "install_id": candidate["install_id"],
            "path": candidate["path"],
            "launch_spec_hash": candidate["launch_spec_hash"],
        }
    )
    return candidate


def _ensure_registry_lists(registry: dict[str, Any]) -> None:
    registry.setdefault("bindings", [])
    registry.setdefault("identity_bindings", [])
    registry.setdefault("approvals", [])


def find_binding(
    registry: dict[str, Any],
    *,
    asset_id: str | None = None,
    install_id: str | None = None,
    gateway_id: str | None = None,
) -> dict[str, Any] | None:
    _ensure_registry_lists(registry)
    for binding in registry.get("bindings", []):
        if asset_id and str(binding.get("asset_id") or "") != asset_id:
            continue
        if install_id and str(binding.get("install_id") or "") != install_id:
            continue
        if gateway_id and str(binding.get("gateway_id") or "") != gateway_id:
            continue
        return binding
    return None


def _bindings_for_asset(registry: dict[str, Any], asset_id: str) -> list[dict[str, Any]]:
    _ensure_registry_lists(registry)
    return [binding for binding in registry.get("bindings", []) if str(binding.get("asset_id") or "") == asset_id]


def upsert_binding(registry: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    _ensure_registry_lists(registry)
    bindings = registry["bindings"]
    target_install_id = str(binding.get("install_id") or "")
    for idx, existing in enumerate(bindings):
        if str(existing.get("install_id") or "") == target_install_id and target_install_id:
            merged = dict(existing)
            merged.update(binding)
            bindings[idx] = merged
            return merged
    bindings.append(binding)
    return binding


def find_identity_binding(
    registry: dict[str, Any],
    *,
    identity_binding_id: str | None = None,
    install_id: str | None = None,
    asset_id: str | None = None,
    gateway_id: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any] | None:
    _ensure_registry_lists(registry)
    normalized_base_url = _normalized_base_url(base_url)
    for binding in registry.get("identity_bindings", []):
        if identity_binding_id and str(binding.get("identity_binding_id") or "") != identity_binding_id:
            continue
        if install_id and str(binding.get("install_id") or "") != install_id:
            continue
        if asset_id and str(binding.get("asset_id") or "") != asset_id:
            continue
        if gateway_id and str(binding.get("gateway_id") or "") != gateway_id:
            continue
        if (
            normalized_base_url
            and _normalized_base_url(
                ((binding.get("environment") or {}) if isinstance(binding.get("environment"), dict) else {}).get(
                    "base_url"
                )
            )
            != normalized_base_url
        ):
            continue
        return binding
    return None


def _identity_bindings_for_asset(
    registry: dict[str, Any], asset_id: str, *, gateway_id: str | None = None
) -> list[dict[str, Any]]:
    _ensure_registry_lists(registry)
    rows = [
        binding for binding in registry.get("identity_bindings", []) if str(binding.get("asset_id") or "") == asset_id
    ]
    if gateway_id:
        rows = [binding for binding in rows if str(binding.get("gateway_id") or "") == gateway_id]
    return rows


def upsert_identity_binding(registry: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    _ensure_registry_lists(registry)
    bindings = registry["identity_bindings"]
    target_id = str(binding.get("identity_binding_id") or "")
    target_install_id = str(binding.get("install_id") or "")
    target_base_url = _normalized_base_url(
        ((binding.get("environment") or {}) if isinstance(binding.get("environment"), dict) else {}).get("base_url")
    )
    for idx, existing in enumerate(bindings):
        existing_base_url = _normalized_base_url(
            ((existing.get("environment") or {}) if isinstance(existing.get("environment"), dict) else {}).get(
                "base_url"
            )
        )
        if target_id and str(existing.get("identity_binding_id") or "") == target_id:
            merged = dict(existing)
            merged.update(binding)
            bindings[idx] = merged
            return merged
        if (
            target_install_id
            and str(existing.get("install_id") or "") == target_install_id
            and existing_base_url == target_base_url
        ):
            merged = dict(existing)
            merged.update(binding)
            bindings[idx] = merged
            return merged
    bindings.append(binding)
    return binding


def _normalize_allowed_spaces_payload(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        if isinstance(payload.get("spaces"), list):
            return _space_cache_rows(payload.get("spaces"))
        if isinstance(payload.get("items"), list):
            return _space_cache_rows(payload.get("items"))
        if isinstance(payload.get("results"), list):
            return _space_cache_rows(payload.get("results"))
    return _space_cache_rows(payload)


def _fetch_allowed_spaces_for_entry(entry: dict[str, Any]) -> list[dict[str, Any]] | None:
    base_url = _normalized_base_url(entry.get("base_url"))
    if not base_url:
        return None
    try:
        token = load_gateway_managed_agent_token(entry)
    except ValueError:
        return None
    client = AxClient(
        base_url=base_url,
        token=token,
        agent_name=str(entry.get("name") or "") or None,
        agent_id=str(entry.get("agent_id") or "") or None,
    )
    try:
        return _normalize_allowed_spaces_payload(client.list_spaces())
    except Exception:
        return None
    finally:
        try:
            client.close()
        except Exception:
            pass


def ensure_gateway_identity_binding(
    registry: dict[str, Any],
    entry: dict[str, Any],
    *,
    session: dict[str, Any] | None = None,
    created_via: str | None = None,
    verify_spaces: bool = False,
) -> dict[str, Any]:
    _ensure_registry_lists(registry)
    gateway_id = _gateway_id_from_registry(registry)
    asset_id = _asset_id_for_entry(entry)
    install_id = str(entry.get("install_id") or "").strip()
    if not install_id:
        install_id = str(uuid.uuid4())
        entry["install_id"] = install_id
    base_url = _normalized_base_url(entry.get("base_url") or (session or {}).get("base_url"))
    existing = find_identity_binding(
        registry,
        identity_binding_id=str(entry.get("identity_binding_id") or "").strip() or None,
        install_id=install_id,
        base_url=base_url or None,
    )
    allowed_spaces = _space_cache_rows(entry.get("allowed_spaces"))
    if not allowed_spaces and existing:
        allowed_spaces = _space_cache_rows(existing.get("allowed_spaces_cache"))
    if verify_spaces:
        fetched = _fetch_allowed_spaces_for_entry(entry)
        if fetched:
            allowed_spaces = fetched
    if not allowed_spaces:
        allowed_spaces = _fallback_allowed_spaces(entry, session=session)
    default_space_id = (
        str(
            entry.get("default_space_id")
            or ((existing or {}).get("default_space_id") if isinstance(existing, dict) else "")
            or next((item.get("space_id") for item in allowed_spaces if bool(item.get("is_default"))), None)
            or entry.get("space_id")
            or (session or {}).get("space_id")
            or ""
        ).strip()
        or None
    )
    active_space_id = (
        str(
            entry.get("active_space_id")
            or ((existing or {}).get("active_space_id") if isinstance(existing, dict) else "")
            or entry.get("space_id")
            or default_space_id
            or ""
        ).strip()
        or None
    )
    default_space_name = _space_name_from_cache(allowed_spaces, default_space_id) or str(
        entry.get("default_space_name") or entry.get("space_name") or default_space_id or ""
    )
    active_space_name = _space_name_from_cache(allowed_spaces, active_space_id) or str(
        entry.get("active_space_name") or entry.get("space_name") or active_space_id or ""
    )
    binding = {
        "identity_binding_id": str((existing or {}).get("identity_binding_id") or f"idbind_{str(uuid.uuid4())}"),
        "asset_id": asset_id,
        "gateway_id": gateway_id,
        "install_id": install_id,
        "environment": {
            "base_url": base_url or None,
            "label": _environment_label_for_base_url(base_url),
            "host": urlparse(base_url).netloc if base_url else None,
        },
        "acting_identity": (
            dict(existing.get("acting_identity") or {})
            if isinstance(existing, dict) and isinstance(existing.get("acting_identity"), dict)
            else {
                "agent_id": str(entry.get("agent_id") or asset_id or "") or None,
                "agent_name": str(entry.get("name") or "") or None,
                "principal_type": "agent",
            }
        ),
        "credential_ref": {
            "kind": "token_file" if str(entry.get("token_file") or "").strip() else "unknown",
            "id": str(
                (existing or {}).get("credential_ref", {}).get("id")
                if isinstance((existing or {}).get("credential_ref"), dict)
                else ""
            )
            or f"cred_{str(entry.get('name') or asset_id or 'asset')}_{_environment_label_for_base_url(base_url)}",
            "display": "Gateway-managed agent token"
            if str(entry.get("credential_source") or "gateway") == "gateway"
            else "Non-gateway credential",
            "path_redacted": _redacted_path(entry.get("token_file")),
        },
        "active_space_id": active_space_id,
        "active_space_name": active_space_name or None,
        "default_space_id": default_space_id,
        "default_space_name": default_space_name or None,
        "allowed_spaces_cache": allowed_spaces,
        "binding_state": "verified" if base_url and str(entry.get("agent_id") or "") else "unbound",
        "created_via": str(created_via or entry.get("created_via") or "gateway_setup"),
        "last_verified_at": _now_iso(),
    }
    stored = upsert_identity_binding(registry, binding)
    entry["identity_binding_id"] = stored["identity_binding_id"]
    entry["default_space_id"] = stored.get("default_space_id")
    entry["default_space_name"] = stored.get("default_space_name")
    if stored.get("active_space_id"):
        entry["active_space_id"] = stored.get("active_space_id")
    if stored.get("active_space_name"):
        entry["active_space_name"] = stored.get("active_space_name")
    return stored


def evaluate_identity_space_binding(
    registry: dict[str, Any],
    entry: dict[str, Any],
    *,
    explicit_space_id: str | None = None,
    requested_base_url: str | None = None,
) -> dict[str, Any]:
    _ensure_registry_lists(registry)
    gateway_id = _gateway_id_from_registry(registry)
    asset_id = _asset_id_for_entry(entry)
    install_id = str(entry.get("install_id") or "").strip() or None
    requested_url = _normalized_base_url(requested_base_url or entry.get("base_url"))
    binding = find_identity_binding(
        registry,
        identity_binding_id=str(entry.get("identity_binding_id") or "").strip() or None,
        install_id=install_id,
        base_url=requested_url or None,
    )
    asset_bindings = _identity_bindings_for_asset(registry, asset_id, gateway_id=gateway_id) if asset_id else []
    fallback_binding = asset_bindings[0] if asset_bindings else None
    acting_identity = (
        (
            binding.get("acting_identity")
            if isinstance(binding, dict) and isinstance(binding.get("acting_identity"), dict)
            else None
        )
        or (
            fallback_binding.get("acting_identity")
            if isinstance(fallback_binding, dict) and isinstance(fallback_binding.get("acting_identity"), dict)
            else None
        )
        or {}
    )
    bound_base_url = _normalized_base_url(
        (
            (binding.get("environment") or {})
            if isinstance(binding, dict) and isinstance(binding.get("environment"), dict)
            else {}
        ).get("base_url")
    )
    environment_status = "environment_unknown"
    if binding:
        environment_status = "environment_allowed"
        if requested_url and bound_base_url and requested_url != bound_base_url:
            environment_status = "environment_mismatch"
    elif requested_url and asset_bindings:
        environment_status = "environment_mismatch"

    identity_status = "verified"
    if not binding:
        identity_status = "verified" if asset_bindings else "unknown_identity"
    elif str(entry.get("credential_source") or "gateway").strip().lower() not in {"gateway", ""}:
        identity_status = "bootstrap_only"
    elif not str(entry.get("token_file") or "").strip():
        identity_status = "bootstrap_only"
    else:
        bound_agent_id = str(acting_identity.get("agent_id") or "").strip()
        bound_agent_name = str(acting_identity.get("agent_name") or "").strip().lower()
        entry_agent_id = str(entry.get("agent_id") or "").strip()
        entry_agent_name = str(entry.get("name") or "").strip().lower()
        if bound_agent_id and entry_agent_id and bound_agent_id != entry_agent_id:
            identity_status = "credential_mismatch"
        elif bound_agent_name and entry_agent_name and bound_agent_name != entry_agent_name:
            identity_status = "fallback_blocked"

    allowed_spaces = _space_cache_rows((binding or {}).get("allowed_spaces_cache"))
    if not allowed_spaces and binding:
        allowed_spaces = _fallback_allowed_spaces(entry)
    active_space_source = "none"
    active_space_id = str(explicit_space_id or "").strip() or None
    if active_space_id:
        active_space_source = "explicit_request"
    elif binding and str(binding.get("active_space_id") or "").strip():
        active_space_id = str(binding.get("active_space_id") or "").strip()
        active_space_source = "gateway_binding"
    elif binding and str(binding.get("default_space_id") or "").strip():
        active_space_id = str(binding.get("default_space_id") or "").strip()
        active_space_source = "visible_default"

    default_space_id = str((binding or {}).get("default_space_id") or "").strip() or None
    default_space_name = (
        str(
            (binding or {}).get("default_space_name")
            or _space_name_from_cache(allowed_spaces, default_space_id)
            or default_space_id
            or ""
        ).strip()
        or None
    )
    active_space_name = (
        _space_name_from_cache(allowed_spaces, active_space_id)
        or str((binding or {}).get("active_space_name") or active_space_id or "").strip()
        or None
    )

    if not active_space_id:
        space_status = "no_active_space"
    elif not allowed_spaces:
        space_status = "unknown"
    elif _space_id_allowed(allowed_spaces, active_space_id):
        space_status = "active_allowed"
    else:
        space_status = "active_not_allowed"

    return {
        "identity_binding_id": str((binding or {}).get("identity_binding_id") or entry.get("identity_binding_id") or "")
        or None,
        "asset_id": asset_id or None,
        "gateway_id": gateway_id,
        "install_id": install_id,
        "acting_agent_id": str(acting_identity.get("agent_id") or entry.get("agent_id") or "").strip() or None,
        "acting_agent_name": str(acting_identity.get("agent_name") or entry.get("name") or "").strip() or None,
        "principal_type": str(acting_identity.get("principal_type") or "agent"),
        "base_url": bound_base_url or requested_url or None,
        "environment_label": _environment_label_for_base_url(bound_base_url or requested_url),
        "environment_status": environment_status,
        "active_space_id": active_space_id,
        "active_space_name": active_space_name,
        "active_space_source": active_space_source,
        "default_space_id": default_space_id,
        "default_space_name": default_space_name,
        "allowed_spaces": allowed_spaces,
        "allowed_space_count": len(allowed_spaces),
        "identity_status": identity_status,
        "space_status": space_status,
        "last_space_verification_at": str((binding or {}).get("last_verified_at") or ""),
        "identity_binding_state": str((binding or {}).get("binding_state") or "unbound"),
        "credential_ref": dict((binding or {}).get("credential_ref") or {})
        if isinstance((binding or {}).get("credential_ref"), dict)
        else None,
    }


def _approval_status(approval: dict[str, Any]) -> str:
    status = str(approval.get("status") or "").strip().lower()
    if status == "denied":
        return "rejected"
    return status


def _find_approval_by_id(registry: dict[str, Any], approval_id: str) -> dict[str, Any] | None:
    _ensure_registry_lists(registry)
    for approval in registry.get("approvals", []):
        if str(approval.get("approval_id") or "") == approval_id:
            return approval
    return None


def _find_approval_for_signature(registry: dict[str, Any], candidate_signature: str) -> dict[str, Any] | None:
    _ensure_registry_lists(registry)
    matches = [
        approval
        for approval in registry.get("approvals", [])
        if str(approval.get("candidate_signature") or "") == candidate_signature
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda item: str(item.get("requested_at") or ""))[-1]


def list_gateway_approvals(*, status: str | None = None) -> list[dict[str, Any]]:
    registry = load_gateway_registry()
    _ensure_registry_lists(registry)
    normalized_status = _normalized_optional_controlled(status, {"pending", "approved", "rejected"})
    approvals: list[dict[str, Any]] = []
    for approval in registry.get("approvals", []):
        row = dict(approval)
        row["status"] = _approval_status(row)
        if normalized_status and row["status"] != normalized_status:
            continue
        approvals.append(row)
    approvals.sort(key=lambda item: str(item.get("requested_at") or ""), reverse=True)
    return approvals


def get_gateway_approval(approval_id: str) -> dict[str, Any]:
    registry = load_gateway_registry()
    approval = _find_approval_by_id(registry, approval_id)
    if approval is None:
        raise LookupError(f"Approval not found: {approval_id}")
    result = dict(approval)
    result["status"] = _approval_status(result)
    return result


def _refresh_attestation_for_matching_entries(
    registry: dict[str, Any],
    *,
    install_id: str | None = None,
    asset_id: str | None = None,
) -> None:
    for entry in registry.get("agents", []):
        if install_id and str(entry.get("install_id") or "") != install_id:
            continue
        if asset_id and _asset_id_for_entry(entry) != asset_id:
            continue
        ensure_gateway_identity_binding(registry, entry)
        entry.update(evaluate_identity_space_binding(registry, entry))
        entry.update(evaluate_runtime_attestation(registry, entry))


def approve_gateway_approval(
    approval_id: str, *, scope: str = "asset", decided_by: str | None = None
) -> dict[str, Any]:
    normalized_scope = str(scope or "asset").strip().lower()
    if normalized_scope not in {"once", "asset", "gateway"}:
        raise ValueError("Approval scope must be one of: once, asset, gateway.")
    registry = load_gateway_registry()
    approval = _find_approval_by_id(registry, approval_id)
    if approval is None:
        raise LookupError(f"Approval not found: {approval_id}")
    candidate_binding = (
        approval.get("candidate_binding") if isinstance(approval.get("candidate_binding"), dict) else None
    )
    if not candidate_binding:
        raise ValueError("Approval is missing its candidate binding.")
    now = _now_iso()
    approval["status"] = "approved"
    approval["decision"] = "approve"
    approval["decision_scope"] = normalized_scope
    approval["decided_at"] = now
    approval["decided_by"] = decided_by or "local_gateway_operator"
    binding = dict(candidate_binding)
    binding["approved_state"] = "approved"
    binding["approved_at"] = now
    binding["approval_scope"] = normalized_scope
    binding["last_verified_at"] = now
    stored_binding = upsert_binding(registry, binding)
    _refresh_attestation_for_matching_entries(
        registry,
        install_id=str(approval.get("install_id") or "") or None,
        asset_id=str(approval.get("asset_id") or "") or None,
    )
    save_gateway_registry(registry)
    _record_governance_activity(
        "approval_granted",
        asset_id=approval.get("asset_id"),
        install_id=approval.get("install_id"),
        approval_id=approval.get("approval_id"),
        decision_scope=normalized_scope,
        decided_by=approval["decided_by"],
        gateway_id=approval.get("gateway_id"),
        path=stored_binding.get("path"),
    )
    result = dict(approval)
    result["status"] = _approval_status(result)
    return {"approval": result, "binding": stored_binding}


def deny_gateway_approval(approval_id: str, *, decided_by: str | None = None) -> dict[str, Any]:
    registry = load_gateway_registry()
    approval = _find_approval_by_id(registry, approval_id)
    if approval is None:
        raise LookupError(f"Approval not found: {approval_id}")
    now = _now_iso()
    approval["status"] = "rejected"
    approval["decision"] = "deny"
    approval["decided_at"] = now
    approval["decided_by"] = decided_by or "local_gateway_operator"
    _refresh_attestation_for_matching_entries(
        registry,
        install_id=str(approval.get("install_id") or "") or None,
        asset_id=str(approval.get("asset_id") or "") or None,
    )
    save_gateway_registry(registry)
    _record_governance_activity(
        "approval_denied",
        asset_id=approval.get("asset_id"),
        install_id=approval.get("install_id"),
        approval_id=approval.get("approval_id"),
        decided_by=approval["decided_by"],
        gateway_id=approval.get("gateway_id"),
    )
    result = dict(approval)
    result["status"] = _approval_status(result)
    return result


def _record_governance_activity(event: str, *, entry: dict[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
    return record_gateway_activity(event, entry=entry, **fields)


def ensure_local_asset_binding(
    registry: dict[str, Any],
    entry: dict[str, Any],
    *,
    created_via: str | None = None,
    auto_approve: bool = True,
    replace_existing: bool = False,
) -> dict[str, Any]:
    _ensure_registry_lists(registry)
    gateway_id = _gateway_id_from_registry(registry)
    asset_id = _asset_id_for_entry(entry)
    install_id = str(entry.get("install_id") or "").strip()
    if not install_id:
        install_id = str(uuid.uuid4())
        entry["install_id"] = install_id
    existing = find_binding(registry, install_id=install_id) or find_binding(
        registry, asset_id=asset_id, gateway_id=gateway_id
    )
    if existing:
        entry["install_id"] = str(existing.get("install_id") or install_id)
        if not replace_existing:
            return existing
        candidate = _binding_candidate_for_entry(
            {**entry, "created_via": created_via or entry.get("created_via")}, registry
        )
        candidate["first_seen_at"] = str(existing.get("first_seen_at") or candidate.get("first_seen_at") or _now_iso())
        if auto_approve:
            candidate["approved_state"] = "approved"
            candidate["approved_at"] = _now_iso()
        binding = upsert_binding(registry, candidate)
        if str(existing.get("candidate_signature") or "") != str(binding.get("candidate_signature") or ""):
            _record_governance_activity(
                "asset_binding_updated",
                entry=entry,
                asset_id=asset_id,
                install_id=entry["install_id"],
                binding_type=binding.get("binding_type"),
                gateway_id=gateway_id,
                path=binding.get("path"),
            )
        return binding
    candidate = _binding_candidate_for_entry(
        {**entry, "created_via": created_via or entry.get("created_via")}, registry
    )
    if auto_approve:
        candidate["approved_state"] = "approved"
        candidate["approved_at"] = _now_iso()
    binding = upsert_binding(registry, candidate)
    entry["install_id"] = str(binding.get("install_id") or install_id)
    _record_governance_activity(
        "asset_bound",
        entry=entry,
        asset_id=asset_id,
        install_id=entry["install_id"],
        binding_type=binding.get("binding_type"),
        gateway_id=gateway_id,
        path=binding.get("path"),
    )
    return binding


def _create_binding_approval(
    registry: dict[str, Any],
    entry: dict[str, Any],
    *,
    candidate_binding: dict[str, Any],
    action: str,
    reason: str,
    risk: str,
    approval_kind: str,
) -> dict[str, Any]:
    existing = _find_approval_for_signature(registry, str(candidate_binding.get("candidate_signature") or ""))
    if existing:
        return existing
    approval = {
        "approval_id": str(uuid.uuid4()),
        "asset_id": candidate_binding.get("asset_id"),
        "gateway_id": candidate_binding.get("gateway_id"),
        "install_id": candidate_binding.get("install_id"),
        "action": action,
        "resource": candidate_binding.get("path") or candidate_binding.get("launch_spec_hash"),
        "reason": reason,
        "risk": risk,
        "status": "pending",
        "decision": None,
        "requested_at": _now_iso(),
        "expires_at": None,
        "candidate_signature": candidate_binding.get("candidate_signature"),
        "candidate_binding": candidate_binding,
        "approval_kind": approval_kind,
    }
    registry.setdefault("approvals", []).append(approval)
    _record_governance_activity(
        "approval_requested",
        entry=entry,
        approval_id=approval["approval_id"],
        asset_id=approval["asset_id"],
        install_id=approval["install_id"],
        approval_kind=approval_kind,
        reason=reason,
        risk=risk,
    )
    return approval


def evaluate_runtime_attestation(registry: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    _ensure_registry_lists(registry)
    gateway_id = _gateway_id_from_registry(registry)
    asset_id = _asset_id_for_entry(entry)
    install_id = str(entry.get("install_id") or "").strip()
    candidate = _binding_candidate_for_entry(entry, registry)
    latest_approval = _find_approval_for_signature(registry, candidate["candidate_signature"])

    def blocked(
        reason: str, detail: str, *, approval: dict[str, Any] | None = None, state: str = "blocked"
    ) -> dict[str, Any]:
        return {
            "asset_id": asset_id or None,
            "gateway_id": gateway_id,
            "install_id": install_id or candidate["install_id"],
            "binding": None,
            "candidate_binding": candidate,
            "runtime_instance_id": str(entry.get("runtime_instance_id") or "") or None,
            "attestation_state": state,
            "drift_reason": reason,
            "approval_state": "rejected"
            if approval and _approval_status(approval) == "rejected"
            else ("pending" if approval and _approval_status(approval) == "pending" else "not_required"),
            "approval_id": approval.get("approval_id") if approval else None,
            "confidence_reason": reason,
            "confidence_detail": detail,
        }

    if not asset_id:
        return blocked("unknown_asset", "Runtime is missing a registered asset identity.")

    install_binding = find_binding(registry, install_id=install_id) if install_id else None
    asset_bindings = _bindings_for_asset(registry, asset_id)

    if install_binding and str(install_binding.get("asset_id") or "") != asset_id:
        return blocked("asset_mismatch", "Runtime install is bound to a different asset id than the one it claimed.")

    if latest_approval and _approval_status(latest_approval) == "rejected":
        return blocked(
            "approval_denied", "A prior approval request for this runtime binding was denied.", approval=latest_approval
        )

    if not install_binding:
        if asset_bindings:
            same_gateway = next(
                (binding for binding in asset_bindings if str(binding.get("gateway_id") or "") == gateway_id), None
            )
            if same_gateway is None:
                approval = latest_approval or _create_binding_approval(
                    registry,
                    entry,
                    candidate_binding=candidate,
                    action="runtime.bind",
                    reason="Asset is requesting access from a different Gateway than the approved binding.",
                    risk="high",
                    approval_kind="new_gateway",
                )
                return blocked(
                    "new_gateway",
                    "This asset is requesting access from a new Gateway and needs approval.",
                    approval=approval,
                    state="unknown",
                )
        approval = latest_approval or _create_binding_approval(
            registry,
            entry,
            candidate_binding=candidate,
            action="runtime.bind",
            reason="Gateway discovered a runtime binding that has not been approved yet.",
            risk="medium",
            approval_kind="new_binding",
        )
        return blocked(
            "approval_required",
            "Gateway needs approval before trusting this new asset binding.",
            approval=approval,
            state="unknown",
        )

    binding = install_binding
    if str(binding.get("gateway_id") or "") != gateway_id:
        approval = latest_approval or _create_binding_approval(
            registry,
            entry,
            candidate_binding=candidate,
            action="runtime.bind",
            reason="Asset binding is attempting to run from a different Gateway than the approved one.",
            risk="high",
            approval_kind="new_gateway",
        )
        return blocked(
            "new_gateway",
            "This asset binding is tied to a different Gateway and needs approval.",
            approval=approval,
            state="unknown",
        )

    if str(binding.get("approved_state") or "approved").lower() == "rejected":
        return blocked("approval_denied", "This asset binding was previously rejected.")

    current_path = str(candidate.get("path") or "")
    bound_path = str(binding.get("path") or "")
    current_hash = str(candidate.get("launch_spec_hash") or "")
    bound_hash = str(binding.get("launch_spec_hash") or "")
    if current_path != bound_path or current_hash != bound_hash:
        approval = latest_approval or _create_binding_approval(
            registry,
            entry,
            candidate_binding=candidate,
            action="runtime.attest",
            reason="Runtime launch path or launch spec changed since approval.",
            risk="high",
            approval_kind="binding_drift",
        )
        detail = "Runtime launch path or spec changed since approval. Review and approve the new binding before Gateway will trust it."
        return {
            "asset_id": asset_id,
            "gateway_id": gateway_id,
            "install_id": str(binding.get("install_id") or candidate["install_id"]),
            "binding": binding,
            "candidate_binding": candidate,
            "runtime_instance_id": str(entry.get("runtime_instance_id") or "") or None,
            "attestation_state": "drifted",
            "drift_reason": "binding_drift",
            "approval_state": "pending" if approval and _approval_status(approval) == "pending" else "not_required",
            "approval_id": approval.get("approval_id") if approval else None,
            "confidence_reason": "binding_drift",
            "confidence_detail": detail,
        }

    return {
        "asset_id": asset_id,
        "gateway_id": gateway_id,
        "install_id": str(binding.get("install_id") or candidate["install_id"]),
        "binding": binding,
        "candidate_binding": candidate,
        "runtime_instance_id": str(entry.get("runtime_instance_id") or "") or None,
        "attestation_state": "verified",
        "drift_reason": None,
        "approval_state": "not_required",
        "approval_id": None,
        "confidence_reason": None,
        "confidence_detail": "Runtime matches the approved local binding.",
    }


def _parse_iso8601(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_seconds(value: object, *, now: datetime | None = None) -> int | None:
    parsed = _parse_iso8601(value)
    if parsed is None:
        return None
    current = now or datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = current - parsed.astimezone(timezone.utc)
    return max(0, int(delta.total_seconds()))


def annotate_runtime_health(
    snapshot: dict[str, Any],
    *,
    now: datetime | None = None,
    registry: dict[str, Any] | None = None,
    explicit_space_id: str | None = None,
) -> dict[str, Any]:
    enriched = dict(snapshot)
    resolved_registry = registry
    if resolved_registry is None:
        try:
            resolved_registry = load_gateway_registry()
        except Exception:
            resolved_registry = None
    if resolved_registry and (resolved_registry.get("identity_bindings") or enriched.get("identity_binding_id")):
        identity_space = evaluate_identity_space_binding(
            resolved_registry, enriched, explicit_space_id=explicit_space_id
        )
        enriched.update(identity_space)
    last_seen_age = _age_seconds(enriched.get("last_seen_at"), now=now)
    last_error_age = _age_seconds(enriched.get("last_listener_error_at"), now=now)
    if last_seen_age is not None:
        enriched["last_seen_age_seconds"] = last_seen_age
    if last_error_age is not None:
        enriched["last_listener_error_age_seconds"] = last_error_age

    profile = infer_operator_profile(enriched)
    asset_descriptor = infer_asset_descriptor(enriched, operator_profile=profile)
    state = str(enriched.get("effective_state") or "stopped").lower()
    raw_state = state
    liveness, connected = _derive_liveness(enriched, raw_state=state, last_seen_age=last_seen_age)
    if liveness == "stale" and raw_state == "running":
        state = "stale"
    elif liveness == "setup_error":
        state = "error"
    elif liveness == "offline" and state not in {"stopped", "error"}:
        state = "stopped"

    work_state = _derive_work_state(enriched, liveness=liveness)
    mode = _derive_mode(profile)
    presence = _derive_presence(mode=mode, liveness=liveness, work_state=work_state)
    reply = _derive_reply(profile["reply_mode"])
    reachability = _derive_reachability(
        snapshot=enriched, mode=mode, liveness=liveness, activation=profile["activation"]
    )
    confidence, confidence_reason, confidence_detail = _derive_confidence(
        enriched,
        mode=mode,
        liveness=liveness,
        reachability=reachability,
    )

    enriched.update(profile)
    enriched["asset_class"] = _normalized_controlled(
        asset_descriptor["asset_class"], _CONTROLLED_ASSET_CLASSES, fallback="interactive_agent"
    )
    enriched["intake_model"] = _normalized_controlled(
        asset_descriptor["intake_model"], _CONTROLLED_INTAKE_MODELS, fallback="launch_on_send"
    )
    if asset_descriptor.get("worker_model"):
        enriched["worker_model"] = asset_descriptor["worker_model"]
    enriched["trigger_sources"] = list(asset_descriptor.get("trigger_sources") or [])
    enriched["return_paths"] = list(asset_descriptor.get("return_paths") or [])
    enriched["telemetry_shape"] = _normalized_controlled(
        asset_descriptor.get("telemetry_shape"),
        _CONTROLLED_TELEMETRY_SHAPES,
        fallback="basic",
    )
    enriched["asset_type_label"] = str(asset_descriptor.get("type_label") or "Connected Asset")
    enriched["output_label"] = str(asset_descriptor.get("output_label") or "Reply")
    enriched["tags"] = list(asset_descriptor.get("tags") or [])
    enriched["capabilities"] = list(asset_descriptor.get("capabilities") or [])
    enriched["constraints"] = list(asset_descriptor.get("constraints") or [])
    enriched["asset_descriptor"] = asset_descriptor
    enriched["effective_state"] = state
    enriched["connected"] = connected
    enriched["liveness"] = _normalized_controlled(liveness, _CONTROLLED_LIVENESS, fallback="offline")
    enriched["work_state"] = _normalized_controlled(work_state, _CONTROLLED_WORK_STATES, fallback="idle")
    enriched["mode"] = _normalized_controlled(mode, _CONTROLLED_MODES, fallback="ON-DEMAND")
    enriched["presence"] = _normalized_controlled(presence, _CONTROLLED_PRESENCE, fallback="OFFLINE")
    enriched["reply"] = _normalized_controlled(reply, _CONTROLLED_REPLY, fallback="REPLY")
    enriched["reachability"] = _normalized_controlled(reachability, _CONTROLLED_REACHABILITY, fallback="unavailable")
    enriched["confidence"] = _normalized_controlled(confidence, _CONTROLLED_CONFIDENCE, fallback="MEDIUM")
    enriched["confidence_reason"] = _normalized_controlled(
        confidence_reason,
        _CONTROLLED_CONFIDENCE_REASONS,
        fallback="unknown",
    )
    enriched["confidence_detail"] = str(confidence_detail or "").strip() or None
    enriched["attestation_state"] = _normalized_optional_controlled(
        enriched.get("attestation_state"), _CONTROLLED_ATTESTATION_STATES
    )
    enriched["approval_state"] = _normalized_optional_controlled(
        enriched.get("approval_state"), _CONTROLLED_APPROVAL_STATES
    )
    enriched["identity_status"] = _normalized_optional_controlled(
        enriched.get("identity_status"), _CONTROLLED_IDENTITY_STATUSES
    )
    enriched["space_status"] = _normalized_optional_controlled(enriched.get("space_status"), _CONTROLLED_SPACE_STATUSES)
    enriched["environment_status"] = _normalized_optional_controlled(
        enriched.get("environment_status"), _CONTROLLED_ENVIRONMENT_STATUSES
    )
    enriched["active_space_source"] = _normalized_optional_controlled(
        enriched.get("active_space_source"), _CONTROLLED_ACTIVE_SPACE_SOURCES
    )
    enriched["queue_capable"] = profile["placement"] == "mailbox"
    enriched["queue_depth"] = int(enriched.get("backlog_depth") or 0)
    enriched.setdefault("last_successful_doctor_at", None)
    enriched.setdefault("last_doctor_result", None)
    return enriched


def gateway_dir() -> Path:
    explicit = str(os.environ.get("AX_GATEWAY_DIR") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
    else:
        root = _global_config_dir() / "gateway"
        env_name = gateway_environment()
        path = root if env_name is None else root / "envs" / env_name
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def gateway_environment() -> str | None:
    raw = (
        str(os.environ.get("AX_GATEWAY_ENV") or "").strip()
        or str(os.environ.get("AX_USER_ENV") or "").strip()
        or str(os.environ.get("AX_ENV") or "").strip()
    )
    if not raw:
        return None
    normalized = re.sub(r"[^a-z0-9_.-]+", "-", raw.lower()).strip(".-")
    if not normalized or normalized in {"default", "user"}:
        return None
    return normalized


def gateway_agents_dir() -> Path:
    path = gateway_dir() / "agents"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def session_path() -> Path:
    return gateway_dir() / "session.json"


def registry_path() -> Path:
    return gateway_dir() / "registry.json"


def pid_path() -> Path:
    return gateway_dir() / "gateway.pid"


def ui_state_path() -> Path:
    return gateway_dir() / "gateway-ui.json"


def daemon_log_path() -> Path:
    return gateway_dir() / "gateway.log"


def ui_log_path() -> Path:
    return gateway_dir() / "gateway-ui.log"


def activity_log_path() -> Path:
    return gateway_dir() / "activity.jsonl"


def agent_dir(name: str) -> Path:
    path = gateway_agents_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def agent_token_path(name: str) -> Path:
    return agent_dir(name) / "token"


def load_gateway_managed_agent_token(entry: dict[str, Any]) -> str:
    """Read a Gateway-managed runtime token and reject bootstrap credentials."""
    token_file = Path(str(entry.get("token_file") or "")).expanduser()
    if not token_file.exists():
        raise ValueError(f"Gateway-managed token file is missing: {token_file}")
    token = token_file.read_text().strip()
    if not token:
        raise ValueError(f"Gateway-managed token file is empty: {token_file}")
    if token.startswith("axp_u_"):
        raise ValueError(
            "Gateway-managed agents require an agent-bound token. "
            f"Refusing to use a user bootstrap PAT from {token_file}."
        )
    if not str(entry.get("agent_id") or "").strip():
        raise ValueError("Gateway-managed agents require a bound agent_id before runtime use.")
    return token


def agent_pending_queue_path(name: str) -> Path:
    return agent_dir(name) / "pending.json"


def _default_pending_queue() -> dict[str, Any]:
    return {"version": 1, "items": []}


def load_agent_pending_messages(name: str) -> list[dict[str, Any]]:
    payload = _read_json(agent_pending_queue_path(name), default=_default_pending_queue())
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, dict)]


def save_agent_pending_messages(name: str, items: list[dict[str, Any]]) -> Path:
    payload = {
        "version": 1,
        "items": [dict(item) for item in items if isinstance(item, dict)],
    }
    _write_json(agent_pending_queue_path(name), payload)
    return agent_pending_queue_path(name)


def append_agent_pending_message(name: str, message: dict[str, Any]) -> list[dict[str, Any]]:
    message_id = str(message.get("message_id") or message.get("id") or "").strip()
    items = load_agent_pending_messages(name)
    if any(str(item.get("message_id") or "").strip() == message_id for item in items):
        return items
    items.append(
        {
            "message_id": message_id,
            "parent_id": str(message.get("parent_id") or "").strip() or None,
            "conversation_id": str(message.get("conversation_id") or "").strip() or None,
            "content": str(message.get("content") or ""),
            "display_name": str(
                message.get("display_name") or message.get("agent_name") or message.get("sender_name") or ""
            )
            or None,
            "created_at": str(message.get("created_at") or _now_iso()),
            "queued_at": _now_iso(),
        }
    )
    save_agent_pending_messages(name, items)
    return items


def remove_agent_pending_message(name: str, message_id: str | None) -> list[dict[str, Any]]:
    target = str(message_id or "").strip()
    if not target:
        return load_agent_pending_messages(name)
    items = [item for item in load_agent_pending_messages(name) if str(item.get("message_id") or "").strip() != target]
    save_agent_pending_messages(name, items)
    return items


def _default_registry() -> dict[str, Any]:
    return {
        "version": 1,
        "gateway": {
            "gateway_id": str(uuid.uuid4()),
            "desired_state": "stopped",
            "effective_state": "stopped",
            "session_connected": False,
            "pid": None,
            "last_started_at": None,
            "last_reconcile_at": None,
        },
        "agents": [],
        "bindings": [],
        "identity_bindings": [],
        "approvals": [],
    }


def _write_json(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.chmod(mode)
    tmp.replace(path)
    path.chmod(mode)


def _read_json(path: Path, *, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return copy.deepcopy(default)
    return json.loads(path.read_text())


def load_gateway_session() -> dict[str, Any]:
    return _read_json(session_path(), default={})


def save_gateway_session(data: dict[str, Any]) -> Path:
    payload = dict(data)
    payload.setdefault("saved_at", _now_iso())
    _write_json(session_path(), payload)
    return session_path()


def load_gateway_registry() -> dict[str, Any]:
    registry = _read_json(registry_path(), default=_default_registry())
    registry.setdefault("version", 1)
    registry.setdefault("gateway", {})
    registry.setdefault("agents", [])
    registry.setdefault("bindings", [])
    registry.setdefault("identity_bindings", [])
    registry.setdefault("approvals", [])
    gateway = registry["gateway"]
    gateway.setdefault("gateway_id", str(uuid.uuid4()))
    gateway.setdefault("desired_state", "stopped")
    gateway.setdefault("effective_state", "stopped")
    gateway.setdefault("session_connected", False)
    gateway.setdefault("pid", None)
    gateway.setdefault("last_started_at", None)
    gateway.setdefault("last_reconcile_at", None)
    return registry


def save_gateway_registry(registry: dict[str, Any]) -> Path:
    _write_json(registry_path(), registry)
    return registry_path()


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def daemon_status() -> dict[str, Any]:
    pid = None
    if pid_path().exists():
        try:
            pid = int(pid_path().read_text().strip())
        except ValueError:
            pid = None
    running = _pid_alive(pid)
    if not running:
        scanned = _scan_gateway_process_pids()
        if scanned:
            pid = scanned[0]
            running = True
    registry = load_gateway_registry()
    return {
        "pid": pid,
        "running": running,
        "gateway_dir": str(gateway_dir()),
        "gateway_environment": gateway_environment(),
        "registry_path": str(registry_path()),
        "session_path": str(session_path()),
        "registry": registry,
    }


def _scan_process_pids(pattern: re.Pattern[str]) -> list[int]:
    current_pid = os.getpid()
    parent_pid = os.getppid()
    try:
        output = subprocess.check_output(
            ["ps", "-axo", "pid=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []

    pids: list[int] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        pid_text, _, command = line.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid in {current_pid, parent_pid} or not _pid_alive(pid):
            continue
        command = command.strip()
        if command and pattern.search(command):
            pids.append(pid)
    return sorted(set(pids))


def _scan_gateway_process_pids() -> list[int]:
    """Best-effort fallback for live daemons that predate the pid file."""
    return _scan_process_pids(_GATEWAY_PROCESS_RE)


def _default_ui_state() -> dict[str, Any]:
    return {
        "pid": None,
        "host": "127.0.0.1",
        "port": 8765,
        "last_started_at": None,
    }


def load_gateway_ui_state() -> dict[str, Any]:
    state = _read_json(ui_state_path(), default=_default_ui_state())
    state.setdefault("pid", None)
    state.setdefault("host", "127.0.0.1")
    state.setdefault("port", 8765)
    state.setdefault("last_started_at", None)
    return state


def save_gateway_ui_state(data: dict[str, Any]) -> Path:
    payload = _default_ui_state()
    payload.update(data)
    _write_json(ui_state_path(), payload)
    return ui_state_path()


def ui_status() -> dict[str, Any]:
    state = load_gateway_ui_state()
    pid = state.get("pid")
    try:
        pid_value = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        pid_value = None
    host = str(state.get("host") or "127.0.0.1")
    try:
        port = int(state.get("port") or 8765)
    except (TypeError, ValueError):
        port = 8765
    running = _pid_alive(pid_value)
    if not running:
        scanned = _scan_gateway_ui_process_pids()
        if scanned:
            pid_value = scanned[0]
            running = True
    return {
        "pid": pid_value,
        "running": running,
        "host": host,
        "port": port,
        "url": f"http://{host}:{port}",
        "state_path": str(ui_state_path()),
        "log_path": str(ui_log_path()),
        "last_started_at": state.get("last_started_at"),
    }


def _scan_gateway_ui_process_pids() -> list[int]:
    """Best-effort fallback for live UIs that predate the ui state file."""
    return _scan_process_pids(_GATEWAY_UI_PROCESS_RE)


def active_gateway_ui_pids() -> list[int]:
    """Return all known live Gateway UI PIDs except the current process."""
    status = ui_status()
    pids: list[int] = []
    pid = status.get("pid")
    if isinstance(pid, int) and status.get("running") and pid != os.getpid():
        pids.append(pid)
    pids.extend(_scan_gateway_ui_process_pids())
    return sorted(set(pids))


def active_gateway_ui_pid() -> int | None:
    """Return the PID of a live Gateway UI, if one is already running."""
    pids = active_gateway_ui_pids()
    return pids[0] if pids else None


def write_gateway_ui_state(*, pid: int, host: str, port: int) -> None:
    save_gateway_ui_state(
        {
            "pid": pid,
            "host": host,
            "port": port,
            "last_started_at": _now_iso(),
        }
    )


def clear_gateway_ui_state(pid: int | None = None) -> None:
    if not ui_state_path().exists():
        return
    if pid is not None:
        try:
            state = load_gateway_ui_state()
            existing_pid = int(state.get("pid")) if state.get("pid") is not None else None
        except (TypeError, ValueError):
            existing_pid = None
        if existing_pid not in {None, pid}:
            return
    ui_state_path().unlink()


def active_gateway_pids() -> list[int]:
    """Return all known live Gateway daemon PIDs except the current process."""
    status = daemon_status()
    pids: list[int] = []
    pid = status.get("pid")
    if isinstance(pid, int) and status.get("running") and pid != os.getpid():
        pids.append(pid)
    pids.extend(_scan_gateway_process_pids())
    return sorted(set(pids))


def active_gateway_pid() -> int | None:
    """Return the PID of a live Gateway daemon, if one is already running."""
    pids = active_gateway_pids()
    return pids[0] if pids else None


def write_gateway_pid(pid: int) -> None:
    pid_path().write_text(f"{pid}\n")
    pid_path().chmod(0o600)


def clear_gateway_pid(pid: int | None = None) -> None:
    if not pid_path().exists():
        return
    if pid is not None:
        try:
            existing_pid = int(pid_path().read_text().strip())
        except ValueError:
            existing_pid = None
        if existing_pid not in {None, pid}:
            return
    pid_path().unlink()


def record_gateway_activity(
    event: str,
    *,
    entry: dict[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "ts": _now_iso(),
        "event": event,
    }
    registry = load_gateway_registry()
    gateway = registry.get("gateway", {})
    if gateway.get("gateway_id"):
        record["gateway_id"] = gateway["gateway_id"]
    if entry:
        record.update(
            {
                "agent_name": entry.get("name"),
                "agent_id": entry.get("agent_id"),
                "asset_id": _asset_id_for_entry(entry) or None,
                "install_id": entry.get("install_id"),
                "runtime_instance_id": entry.get("runtime_instance_id"),
                "runtime_type": entry.get("runtime_type"),
                "transport": entry.get("transport", "gateway"),
                "credential_source": entry.get("credential_source", "gateway"),
            }
        )
    for key, value in fields.items():
        if value is not None:
            record[key] = value

    path = activity_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _ACTIVITY_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        path.chmod(0o600)
    return record


def load_recent_gateway_activity(
    limit: int = DEFAULT_ACTIVITY_LIMIT,
    *,
    agent_name: str | None = None,
) -> list[dict[str, Any]]:
    path = activity_log_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    if limit <= 0:
        return []
    agent_filter = agent_name.strip().lower() if agent_name else None
    items: list[dict[str, Any]] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if agent_filter and str(payload.get("agent_name") or "").lower() != agent_filter:
            continue
        items.append(payload)
        if len(items) >= limit:
            break
    items.reverse()
    return items


def find_agent_entry(registry: dict[str, Any], name: str) -> dict[str, Any] | None:
    for entry in registry.get("agents", []):
        if str(entry.get("name", "")).lower() == name.lower():
            return entry
    return None


def _apply_placement_event(
    entry: dict[str, Any],
    event_data: dict[str, Any],
    *,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Apply an ``agent.placement.changed`` event to the local Gateway registry.

    Returns a result dict describing what happened:

        {
          "applied": bool,
          "reason": str | None,           # if not applied
          "previous_space": str | None,
          "new_space": str | None,
          "placement_state": str | None,
          "policy_revision": int | None,
        }

    Spec: ``specs/GATEWAY-PLACEMENT-POLICY-001/spec.md``. The event payload
    follows the placement record fields (lines 32-46): ``agent_id``,
    ``current_space``, ``placement_state``, ``policy_revision``, etc.

    Idempotent: events for an agent we don't manage, or where ``current_space``
    already matches, are no-ops (``applied: False``, ``reason`` set).
    """
    event_agent_id = str(event_data.get("agent_id") or "").strip()
    entry_agent_id = str(entry.get("agent_id") or "").strip()
    if event_agent_id and entry_agent_id and event_agent_id != entry_agent_id:
        return {"applied": False, "reason": "agent_id_mismatch"}

    new_space = str(event_data.get("current_space") or event_data.get("space_id") or "").strip()
    if not new_space:
        return {"applied": False, "reason": "missing_current_space"}

    previous_space = str(entry.get("space_id") or "").strip() or None
    placement_state = str(event_data.get("placement_state") or "applied").strip() or "applied"
    policy_revision = event_data.get("policy_revision")
    try:
        policy_revision_int = int(policy_revision) if policy_revision is not None else None
    except (TypeError, ValueError):
        policy_revision_int = None

    # Already in sync — ack-without-apply
    if previous_space == new_space:
        existing_rev = entry.get("placement_revision")
        if policy_revision_int is None or (existing_rev is not None and int(existing_rev) >= policy_revision_int):
            return {
                "applied": False,
                "reason": "already_at_target",
                "previous_space": previous_space,
                "new_space": new_space,
                "placement_state": placement_state,
                "policy_revision": policy_revision_int,
            }

    # Persist to local registry
    registry = load_gateway_registry()
    name = agent_name or str(entry.get("name") or "")
    target_entry = find_agent_entry(registry, name)
    if target_entry is None:
        return {
            "applied": False,
            "reason": "agent_not_in_registry",
            "previous_space": previous_space,
            "new_space": new_space,
        }
    target_entry["space_id"] = new_space
    target_entry["placement_state"] = placement_state
    if policy_revision_int is not None:
        target_entry["placement_revision"] = policy_revision_int
    if "current_space_set_by" in event_data:
        target_entry["placement_source"] = str(event_data["current_space_set_by"])
    save_gateway_registry(registry)

    # Mirror to caller's `entry` so subsequent calls in same loop see the new value
    entry["space_id"] = new_space
    entry["placement_state"] = placement_state
    if policy_revision_int is not None:
        entry["placement_revision"] = policy_revision_int

    return {
        "applied": True,
        "previous_space": previous_space,
        "new_space": new_space,
        "placement_state": placement_state,
        "policy_revision": policy_revision_int,
    }


def _post_placement_ack(
    client: Any,
    entry: dict[str, Any],
    *,
    placement_state: str,
    policy_revision: int | None = None,
    runtime_pid: int | None = None,
) -> bool:
    """Best-effort PATCH /api/v1/agents/{id}/placement/ack — backend task 31adc3a4.

    Returns True on success, False otherwise. 404 is the expected failure mode
    until backend ships the endpoint; logged but not fatal.
    """
    agent_id = str(entry.get("agent_id") or "").strip()
    if not agent_id:
        return False
    body: dict[str, Any] = {"placement_state": placement_state}
    if policy_revision is not None:
        body["policy_revision"] = int(policy_revision)
    if runtime_pid is not None:
        body["runtime_pid"] = int(runtime_pid)
    body["ack_at"] = _now_iso()
    try:
        response = client._http.patch(f"/api/v1/agents/{agent_id}/placement/ack", json=body)
    except Exception:  # noqa: BLE001
        return False
    if response.status_code == 404:
        # Endpoint not yet shipped (31adc3a4 pending) — silent no-op
        return False
    return 200 <= response.status_code < 300


def upsert_agent_entry(registry: dict[str, Any], agent: dict[str, Any]) -> dict[str, Any]:
    agents = registry.setdefault("agents", [])
    for idx, existing in enumerate(agents):
        if str(existing.get("name", "")).lower() == str(agent.get("name", "")).lower():
            merged = dict(existing)
            merged.update(agent)
            agents[idx] = merged
            return merged
    agents.append(agent)
    return agent


def remove_agent_entry(registry: dict[str, Any], name: str) -> dict[str, Any] | None:
    agents = registry.setdefault("agents", [])
    for idx, entry in enumerate(agents):
        if str(entry.get("name", "")).lower() == name.lower():
            return agents.pop(idx)
    return None


def sanitize_exec_env(prompt: str, entry: dict[str, Any]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ENV_DENYLIST}
    env["AX_GATEWAY_AGENT_ID"] = str(entry.get("agent_id") or "")
    env["AX_GATEWAY_AGENT_NAME"] = str(entry.get("name") or "")
    env["AX_GATEWAY_RUNTIME_TYPE"] = str(entry.get("runtime_type") or "")
    env["AX_MENTION_CONTENT"] = prompt
    ollama_model = str(entry.get("ollama_model") or "").strip()
    if ollama_model:
        env["OLLAMA_MODEL"] = ollama_model
    hermes_repo_path = str(entry.get("hermes_repo_path") or "").strip()
    if hermes_repo_path:
        env["HERMES_REPO_PATH"] = hermes_repo_path
    return env


def _parse_gateway_exec_event(raw_line: str) -> dict[str, Any] | None:
    line = raw_line.strip()
    if not line.startswith(GATEWAY_EVENT_PREFIX):
        return None
    payload = line[len(GATEWAY_EVENT_PREFIX) :].strip()
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _hash_tool_arguments(arguments: dict[str, Any] | None) -> str | None:
    if not arguments:
        return None
    encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def runtime_timeout_seconds(entry: dict[str, Any]) -> int:
    """Resolve a safe per-message runtime timeout for Gateway-managed agents."""
    raw_value = entry.get("timeout_seconds")
    if raw_value is None:
        raw_value = entry.get("timeout")
    try:
        timeout = int(raw_value) if raw_value is not None else DEFAULT_HANDLER_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        timeout = DEFAULT_HANDLER_TIMEOUT_SECONDS
    return max(MIN_HANDLER_TIMEOUT_SECONDS, timeout)


def _run_exec_handler(
    command: str,
    prompt: str,
    entry: dict[str, Any],
    *,
    message_id: str | None = None,
    space_id: str | None = None,
    timeout_seconds: int | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    argv = [*shlex.split(command), prompt]
    env = sanitize_exec_env(prompt, entry)
    if message_id:
        env["AX_GATEWAY_MESSAGE_ID"] = message_id
    if space_id:
        env["AX_GATEWAY_SPACE_ID"] = space_id
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=entry.get("workdir") or None,
            env=env,
        )
    except FileNotFoundError:
        return f"(handler not found: {argv[0]})"

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def _consume_stdout() -> None:
        if process.stdout is None:
            return
        for raw in process.stdout:
            event = _parse_gateway_exec_event(raw)
            if event is not None:
                if on_event is not None:
                    try:
                        on_event(event)
                    except Exception:
                        pass
                continue
            stdout_lines.append(raw)

    def _consume_stderr() -> None:
        if process.stderr is None:
            return
        for raw in process.stderr:
            stderr_lines.append(raw)

    stdout_thread = threading.Thread(target=_consume_stdout, daemon=True, name=f"gw-exec-stdout-{entry.get('name')}")
    stderr_thread = threading.Thread(target=_consume_stderr, daemon=True, name=f"gw-exec-stderr-{entry.get('name')}")
    stdout_thread.start()
    stderr_thread.start()

    timeout_seconds = max(MIN_HANDLER_TIMEOUT_SECONDS, int(timeout_seconds or runtime_timeout_seconds(entry)))
    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
    finally:
        stdout_thread.join(timeout=1.0)
        stderr_thread.join(timeout=1.0)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    if timed_out:
        raise GatewayRuntimeTimeoutError(timeout_seconds, runtime_type="exec")

    output = "".join(stdout_lines).strip()
    stderr = "".join(stderr_lines).strip()
    if process.returncode != 0 and stderr:
        output = f"{output}\n(stderr: {stderr[:400]})".strip()
    return output or "(no output)"


def _echo_handler(prompt: str, _entry: dict[str, Any]) -> str:
    return f"Echo: {prompt}"


def _is_passive_runtime(runtime_type: object) -> bool:
    return str(runtime_type or "").lower() in {"inbox", "passive", "monitor"}


def _gateway_pickup_activity(runtime_type: object, backlog_depth: int) -> str:
    if _is_passive_runtime(runtime_type):
        if backlog_depth > 1:
            return f"Queued in Gateway ({backlog_depth} pending)"
        return "Queued in Gateway"
    if backlog_depth > 1:
        return f"Picked up by Gateway ({backlog_depth} pending)"
    return "Picked up by Gateway"


def _is_sentinel_cli_runtime(runtime_type: object) -> bool:
    return str(runtime_type or "").strip().lower() in {"sentinel_cli", "claude_cli", "codex_cli"}


def _is_hermes_sentinel_runtime(runtime_type: object) -> bool:
    return str(runtime_type or "").strip().lower() in {"hermes_sentinel", "hermes_sdk"}


def _gateway_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _agents_dir_for_entry(entry: dict[str, Any]) -> Path:
    workdir = Path(str(entry.get("workdir") or "")).expanduser() if str(entry.get("workdir") or "").strip() else None
    if workdir is not None:
        return workdir.parent
    return Path("/home/ax-agent/agents")


def _hermes_sentinel_script(entry: dict[str, Any]) -> Path:
    configured = str(entry.get("sentinel_script") or entry.get("hermes_sentinel_script") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return _agents_dir_for_entry(entry) / "claude_agent_v2.py"


def _hermes_sentinel_python(entry: dict[str, Any]) -> str:
    configured = str(entry.get("hermes_python") or entry.get("python") or "").strip()
    if configured:
        return configured
    hermes_repo = str(entry.get("hermes_repo_path") or "").strip()
    if hermes_repo:
        candidate = Path(hermes_repo).expanduser() / ".venv" / "bin" / "python3"
        if candidate.exists():
            return str(candidate)
    default = Path("/home/ax-agent/shared/repos/hermes-agent/.venv/bin/python3")
    if default.exists():
        return str(default)
    return "python3"


def _hermes_sentinel_model(entry: dict[str, Any]) -> str:
    for key in ("hermes_model", "sentinel_model", "runtime_model", "model"):
        value = str(entry.get(key) or "").strip()
        if value:
            return value
    return str(os.environ.get("AX_GATEWAY_HERMES_MODEL") or "codex:gpt-5.5")


def _hermes_sentinel_workdir(entry: dict[str, Any]) -> Path:
    raw = str(entry.get("workdir") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path("/home/ax-agent/agents") / str(entry.get("name") or "agent")


def _build_hermes_sentinel_cmd(entry: dict[str, Any]) -> list[str]:
    timeout = str(entry.get("timeout_seconds") or entry.get("timeout") or 600)
    update_interval = str(entry.get("update_interval") or 2.0)
    cmd = [
        _hermes_sentinel_python(entry),
        "-u",
        str(_hermes_sentinel_script(entry)),
        "--agent",
        str(entry.get("name") or ""),
        "--workdir",
        str(_hermes_sentinel_workdir(entry)),
        "--timeout",
        timeout,
        "--update-interval",
        update_interval,
        "--runtime",
        "hermes_sdk",
        "--model",
        _hermes_sentinel_model(entry),
    ]
    allowed_tools = str(entry.get("allowed_tools") or "").strip()
    if allowed_tools:
        cmd.extend(["--allowed-tools", allowed_tools])
    system_prompt = str(entry.get("system_prompt") or "").strip()
    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])
    if _bool_with_fallback(entry.get("disable_codex_mcp"), fallback=False):
        cmd.append("--disable-codex-mcp")
    return cmd


def _build_hermes_sentinel_env(entry: dict[str, Any]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ENV_DENYLIST}
    token = load_gateway_managed_agent_token(entry)
    workdir = _hermes_sentinel_workdir(entry)
    agents_dir = _agents_dir_for_entry(entry)
    hermes_repo = str(entry.get("hermes_repo_path") or "").strip() or "/home/ax-agent/shared/repos/hermes-agent"
    repo_root = str(_gateway_repo_root())

    env.update(
        {
            "AX_TOKEN": token,
            "AX_BASE_URL": str(entry.get("base_url") or ""),
            "AX_AGENT_NAME": str(entry.get("name") or ""),
            "AX_AGENT_ID": str(entry.get("agent_id") or ""),
            "AX_SPACE_ID": str(entry.get("space_id") or ""),
            "AX_CONFIG_DIR": str(workdir / ".ax"),
            "AX_PYTHON": _hermes_sentinel_python(entry),
            "HERMES_MAX_ITERATIONS": str(
                entry.get("hermes_max_iterations") or os.environ.get("HERMES_MAX_ITERATIONS") or 30
            ),
        }
    )
    env.setdefault("AGENT_RUNNER_API_KEY", "staging-dispatch-key")
    env.setdefault("INTERNAL_DISPATCH_API_KEY", env["AGENT_RUNNER_API_KEY"])

    python_paths = [str(agents_dir), hermes_repo, repo_root]
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        python_paths.append(existing_pythonpath)
    env["PYTHONPATH"] = ":".join(path for path in python_paths if path)

    path_entries = [str(_gateway_repo_root() / ".venv" / "bin"), "/home/ax-agent/shared/repos/ax-cli/.venv/bin"]
    if env.get("PATH"):
        path_entries.append(env["PATH"])
    env["PATH"] = ":".join(path_entries)
    return env


def _sentinel_runtime_name(entry: dict[str, Any]) -> str:
    runtime_type = str(entry.get("runtime_type") or "").strip().lower()
    configured = (
        str(entry.get("sentinel_runtime") or entry.get("runtime_backend") or entry.get("cli_runtime") or "")
        .strip()
        .lower()
    )
    if configured in {"claude", "claude_cli"}:
        return "claude"
    if configured in {"codex", "codex_cli"}:
        return "codex"
    if runtime_type == "codex_cli":
        return "codex"
    return "claude"


def _sentinel_session_scope(entry: dict[str, Any]) -> str:
    scope = str(entry.get("sentinel_session_scope") or entry.get("session_scope") or "agent").strip().lower()
    return scope if scope in {"agent", "thread", "message"} else "agent"


def _sentinel_session_key(entry: dict[str, Any], data: dict[str, Any] | None, message_id: str) -> str:
    scope = _sentinel_session_scope(entry)
    if scope == "message":
        return message_id or str(uuid.uuid4())
    if scope == "thread":
        data = data or {}
        return str(data.get("parent_id") or data.get("conversation_id") or message_id or "default")
    return f"space:{entry.get('space_id') or 'unknown'}:agent:{entry.get('name') or 'unknown'}"


def _sentinel_model(entry: dict[str, Any], runtime_name: str) -> str | None:
    runtime_specific_key = "codex_model" if runtime_name == "codex" else "claude_model"
    for key in ("model", "sentinel_model", f"{runtime_name}_model", runtime_specific_key):
        value = str(entry.get(key) or "").strip()
        if value:
            return value
    return None


def _build_sentinel_claude_cmd(entry: dict[str, Any], session_id: str | None) -> list[str]:
    cmd = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--dangerously-skip-permissions",
        "--add-dir",
        str(entry.get("add_dir") or "/home/ax-agent/shared/repos"),
    ]
    if session_id:
        cmd.extend(["--resume", session_id])
    model = _sentinel_model(entry, "claude")
    if model:
        cmd.extend(["--model", model])
    allowed_tools = str(entry.get("allowed_tools") or "").strip()
    if allowed_tools:
        cmd.extend(["--allowedTools", allowed_tools])
    system_prompt = str(entry.get("system_prompt") or "").strip()
    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])
    return cmd


def _build_sentinel_codex_cmd(entry: dict[str, Any], session_id: str | None) -> list[str]:
    workdir = str(entry.get("workdir") or os.getcwd())
    if session_id:
        cmd = [
            "codex",
            "exec",
            "resume",
            session_id,
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            workdir,
        ]
    else:
        cmd = [
            "codex",
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-C",
            workdir,
        ]
    if _bool_with_fallback(entry.get("disable_codex_mcp"), fallback=True):
        cmd.extend(["-c", "mcp_servers.ax-platform.enabled=false"])
    model = _sentinel_model(entry, "codex")
    if model:
        cmd.extend(["-m", model])
    return cmd


def _summarize_sentinel_command(command: str) -> str:
    short = " ".join(command.split())
    if len(short) > 90:
        short = short[:87] + "..."

    lowered = f" {short.lower()} "
    if "apply_patch" in lowered:
        return "Applying patch..."
    if any(token in lowered for token in (" rg ", " grep ", " find ", " fd ", " glob ")):
        return "Searching codebase..."
    if any(
        token in lowered
        for token in (" sed -n", " cat ", " head ", " tail ", " ls ", " pwd ", " git status", " git diff")
    ):
        return "Reading files..."
    if any(token in lowered for token in (" pytest", " npm test", " pnpm test", " uv run", " cargo test")):
        return "Running tests..."
    return f"Running: {short}..."


def _sentinel_tool_summary(tool_name: str, tool_input: dict[str, Any]) -> str:
    lowered = tool_name.lower()
    if lowered in {"read", "read_file"}:
        path = str(tool_input.get("file_path") or tool_input.get("path") or "")
        short = path.rsplit("/", 1)[-1] if "/" in path else path
        return f"Reading {short}..." if short else "Reading file..."
    if lowered in {"write", "write_file"}:
        path = str(tool_input.get("file_path") or tool_input.get("path") or "")
        short = path.rsplit("/", 1)[-1] if "/" in path else path
        return f"Writing {short}..." if short else "Writing file..."
    if lowered in {"edit", "edit_file", "patch"}:
        path = str(tool_input.get("file_path") or tool_input.get("path") or "")
        short = path.rsplit("/", 1)[-1] if "/" in path else path
        return f"Editing {short}..." if short else "Editing file..."
    if lowered in {"bash", "shell"}:
        command = str(tool_input.get("command") or "")[:60]
        return f"Running: {command}..." if command else "Running command..."
    if lowered in {"grep", "search", "search_files"}:
        pattern = str(tool_input.get("pattern") or "")
        return f"Searching: {pattern}..." if pattern else "Searching..."
    if lowered in {"glob", "glob_files"}:
        pattern = str(tool_input.get("pattern") or "")
        return f"Finding files: {pattern}..." if pattern else "Finding files..."
    return f"Using {tool_name}..."


class ManagedAgentRuntime:
    """Listener + worker pair for one managed agent."""

    def __init__(
        self,
        entry: dict[str, Any],
        *,
        client_factory: Callable[..., Any] = AxClient,
        logger: RuntimeLogger | None = None,
    ) -> None:
        self.entry = dict(entry)
        self.client_factory = client_factory
        self.logger = logger or (lambda _msg: None)
        self.stop_event = threading.Event()
        self._listener_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._queue: queue.Queue = queue.Queue(maxsize=int(entry.get("queue_size") or DEFAULT_QUEUE_SIZE))
        self._reply_anchor_ids: set[str] = set()
        self._seen_ids: set[str] = set()
        self._completed_seen_ids: set[str] = set()
        self._sentinel_sessions: dict[str, str] = {}
        self._state_lock = threading.Lock()
        self._stream_client = None
        self._send_client = None
        self._stream_response = None
        self._supervised_process: subprocess.Popen | None = None
        self._supervised_thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "effective_state": "stopped",
            "runtime_instance_id": None,
            "backlog_depth": 0,
            "dropped_count": 0,
            "processed_count": 0,
            "current_status": None,
            "current_activity": None,
            "current_tool": None,
            "current_tool_call_id": None,
            "last_error": None,
            "last_connected_at": None,
            "last_listener_error_at": None,
            "last_started_at": None,
            "last_seen_at": None,
            "last_work_received_at": None,
            "last_work_completed_at": None,
            "last_received_message_id": None,
            "last_reply_message_id": None,
            "last_reply_preview": None,
            "reconnect_backoff_seconds": 0,
        }

    @property
    def name(self) -> str:
        return str(self.entry.get("name") or "")

    @property
    def agent_id(self) -> str | None:
        value = self.entry.get("agent_id")
        return str(value) if value else None

    @property
    def base_url(self) -> str:
        return str(self.entry.get("base_url") or "")

    @property
    def space_id(self) -> str:
        return str(self.entry.get("space_id") or "")

    @property
    def token_file(self) -> Path:
        return Path(str(self.entry.get("token_file") or "")).expanduser()

    def _log(self, message: str) -> None:
        self.logger(f"{self.name}: {message}")

    def _token(self) -> str:
        return load_gateway_managed_agent_token(self.entry)

    def _new_client(self):
        return self.client_factory(
            base_url=self.base_url,
            token=self._token(),
            agent_name=self.name,
            agent_id=self.agent_id,
        )

    def _update_state(self, **fields: Any) -> None:
        with self._state_lock:
            self._state.update(fields)

    def _bump(self, field: str, amount: int = 1) -> None:
        with self._state_lock:
            self._state[field] = int(self._state.get(field) or 0) + amount

    def _mark_completed_seen(self, message_id: str) -> None:
        if not message_id:
            return
        with self._state_lock:
            self._completed_seen_ids.add(message_id)

    def _consume_completed_seen(self, message_id: str) -> bool:
        if not message_id:
            return False
        with self._state_lock:
            seen = message_id in self._completed_seen_ids
            if seen:
                self._completed_seen_ids.discard(message_id)
            return seen

    def _handle_placement_event(self, data: dict[str, Any]) -> None:
        """Handle SSE ``agent.placement.changed`` for this managed agent.

        Per ``specs/GATEWAY-PLACEMENT-POLICY-001/spec.md`` lines 81-93. The
        event carries the new placement record; we update the local Gateway
        registry to keep operator-visible state in sync, log activity, and
        best-effort POST an ack.

        Stub-resilient: if the backend hasn't shipped the ack endpoint yet
        (task ``31adc3a4``), the POST returns 404 and we log a warning. The
        inbound side still works — operators see placement changes in the
        registry without restarting agents.
        """
        try:
            outcome = _apply_placement_event(self.entry, data, agent_name=self.name)
        except Exception as exc:  # noqa: BLE001
            record_gateway_activity(
                "placement_apply_failed",
                entry=self.entry,
                error=str(exc)[:300],
                event=data.get("event_id") or data.get("id"),
            )
            self._log(f"placement event apply failed: {exc}")
            return
        record_gateway_activity(
            "placement_changed",
            entry=self.entry,
            placement_state=outcome.get("placement_state"),
            previous_space=outcome.get("previous_space"),
            new_space=outcome.get("new_space"),
            policy_revision=outcome.get("policy_revision"),
            applied=outcome.get("applied", False),
        )
        if outcome.get("applied"):
            try:
                client = self._new_client()
                _post_placement_ack(
                    client,
                    self.entry,
                    placement_state=str(outcome.get("placement_state") or "applied"),
                    policy_revision=outcome.get("policy_revision"),
                )
            except Exception as exc:  # noqa: BLE001
                # Ack is best-effort while 31adc3a4 ships. Don't kill the listener.
                self._log(f"placement ack failed (non-fatal): {exc}")

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            snapshot = dict(self._state)
        if not _is_passive_runtime(self.entry.get("runtime_type")):
            return snapshot
        registry = load_gateway_registry()
        stored = find_agent_entry(registry, self.name) or {}
        pending_items = load_agent_pending_messages(self.name)
        backlog_depth = len(pending_items)
        merged = dict(snapshot)
        for key in (
            "processed_count",
            "last_work_completed_at",
            "last_reply_message_id",
            "last_reply_preview",
            "last_received_message_id",
            "last_work_received_at",
        ):
            if key in stored:
                merged[key] = stored.get(key)
        merged["backlog_depth"] = backlog_depth
        merged["current_status"] = "queued" if backlog_depth > 0 else None
        merged["current_activity"] = (
            _gateway_pickup_activity(self.entry.get("runtime_type"), backlog_depth)[:240] if backlog_depth > 0 else None
        )
        with self._state_lock:
            self._state.update(merged)
            return dict(self._state)

    def start(self) -> None:
        runtime_type = str(self.entry.get("runtime_type") or "").lower()
        if (
            _is_hermes_sentinel_runtime(runtime_type)
            and self._supervised_process is not None
            and self._supervised_process.poll() is None
        ):
            return
        if self._listener_thread and self._listener_thread.is_alive():
            return
        self.stop_event.clear()
        self._queue = queue.Queue(maxsize=int(self.entry.get("queue_size") or DEFAULT_QUEUE_SIZE))
        self._reply_anchor_ids = set()
        self._seen_ids = set()
        self._completed_seen_ids = set()
        self._sentinel_sessions = {}
        pending_items = load_agent_pending_messages(self.name) if _is_passive_runtime(runtime_type) else []
        backlog_depth = len(pending_items)
        runtime_instance_id = str(uuid.uuid4())
        self.entry["runtime_instance_id"] = runtime_instance_id
        self._update_state(
            effective_state="starting",
            runtime_instance_id=runtime_instance_id,
            backlog_depth=backlog_depth,
            current_status="queued" if backlog_depth > 0 and _is_passive_runtime(runtime_type) else None,
            current_activity=_gateway_pickup_activity(runtime_type, backlog_depth)
            if backlog_depth > 0 and _is_passive_runtime(runtime_type)
            else None,
            current_tool=None,
            current_tool_call_id=None,
            last_error=None,
            last_listener_error_at=None,
            last_started_at=_now_iso(),
            reconnect_backoff_seconds=0,
        )
        if _is_hermes_sentinel_runtime(runtime_type):
            self._start_hermes_sentinel_process(runtime_instance_id=runtime_instance_id)
            return
        self._worker_thread = None
        if not _is_passive_runtime(self.entry.get("runtime_type")):
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                daemon=True,
                name=f"gw-worker-{self.name}",
            )
        self._listener_thread = threading.Thread(
            target=self._listener_loop,
            daemon=True,
            name=f"gw-listener-{self.name}",
        )
        if self._worker_thread is not None:
            self._worker_thread.start()
        self._listener_thread.start()
        record_gateway_activity("runtime_started", entry=self.entry, runtime_instance_id=runtime_instance_id)
        self._log("started")

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_event.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._stream_response is not None:
            try:
                self._stream_response.close()
            except Exception:
                pass
        self._stop_hermes_sentinel_process(timeout=timeout)
        for thread in (self._listener_thread, self._worker_thread, self._supervised_thread):
            if thread and thread.is_alive():
                thread.join(timeout=timeout)
        for client in (self._stream_client, self._send_client):
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        self._stream_client = None
        self._send_client = None
        self._stream_response = None
        self.entry["runtime_instance_id"] = None
        self._update_state(
            effective_state="stopped",
            runtime_instance_id=None,
            backlog_depth=0,
            current_status=None,
            current_activity=None,
            current_tool=None,
            current_tool_call_id=None,
        )
        record_gateway_activity("runtime_stopped", entry=self.entry)
        self._log("stopped")

    def _hermes_sentinel_log_path(self) -> Path:
        configured = str(self.entry.get("log_path") or "").strip()
        if configured:
            return Path(configured).expanduser()
        return _hermes_sentinel_workdir(self.entry) / "gateway-hermes-sentinel.log"

    def _start_hermes_sentinel_process(self, *, runtime_instance_id: str) -> None:
        workdir = _hermes_sentinel_workdir(self.entry)
        script = _hermes_sentinel_script(self.entry)
        if not script.exists():
            error = f"Hermes sentinel script not found: {script}"
            self._update_state(
                effective_state="error", current_status="error", current_activity=error, last_error=error
            )
            record_gateway_activity("runtime_error", entry=self.entry, error=error)
            return
        try:
            load_gateway_managed_agent_token(self.entry)
        except ValueError as exc:
            error = str(exc)
            self._update_state(
                effective_state="error", current_status="error", current_activity=error, last_error=error
            )
            record_gateway_activity("runtime_error", entry=self.entry, error=error)
            return

        workdir.mkdir(parents=True, exist_ok=True)
        log_path = self._hermes_sentinel_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = _build_hermes_sentinel_cmd(self.entry)
        env = _build_hermes_sentinel_env(self.entry)
        try:
            log_handle = log_path.open("a", encoding="utf-8")
            log_handle.write(
                f"\n[{_now_iso()}] Gateway starting Hermes sentinel: {' '.join(shlex.quote(part) for part in cmd)}\n"
            )
            log_handle.flush()
            process = subprocess.Popen(
                cmd,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(workdir),
                env=env,
                start_new_session=True,
            )
            log_handle.close()
        except Exception as exc:
            error = f"Failed to start Hermes sentinel: {str(exc)[:360]}"
            self._update_state(
                effective_state="error", current_status="error", current_activity=error, last_error=error
            )
            record_gateway_activity("runtime_error", entry=self.entry, error=error)
            return

        self._supervised_process = process
        self._update_state(
            effective_state="running",
            current_status=None,
            current_activity="Hermes sentinel listener running",
            current_tool=None,
            current_tool_call_id=None,
            last_error=None,
            last_connected_at=_now_iso(),
            last_seen_at=_now_iso(),
            reconnect_backoff_seconds=0,
        )
        record_gateway_activity(
            "runtime_started",
            entry=self.entry,
            runtime_instance_id=runtime_instance_id,
            pid=process.pid,
            log_path=str(log_path),
            supervised_runtime="hermes_sentinel",
        )
        self._supervised_thread = threading.Thread(
            target=self._monitor_hermes_sentinel_process,
            daemon=True,
            name=f"gw-hermes-sentinel-{self.name}",
        )
        self._supervised_thread.start()
        self._log(f"started hermes_sentinel pid={process.pid}")

    def _monitor_hermes_sentinel_process(self) -> None:
        process = self._supervised_process
        if process is None:
            return
        while not self.stop_event.wait(timeout=5.0):
            returncode = process.poll()
            if returncode is None:
                self._update_state(effective_state="running", last_seen_at=_now_iso(), last_error=None)
                continue
            status = "stopped" if returncode == 0 else "error"
            error = None if returncode == 0 else f"Hermes sentinel exited with code {returncode}"
            self._update_state(
                effective_state=status,
                current_status=None if returncode == 0 else "error",
                current_activity=None if returncode == 0 else error,
                current_tool=None,
                current_tool_call_id=None,
                last_error=error,
                last_seen_at=_now_iso(),
            )
            record_gateway_activity(
                "runtime_exited",
                entry=self.entry,
                pid=process.pid,
                exit_code=returncode,
                error=error,
            )
            return

    def _stop_hermes_sentinel_process(self, *, timeout: float = 5.0) -> None:
        process = self._supervised_process
        self._supervised_process = None
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=timeout)
        except ProcessLookupError:
            return
        except Exception:
            try:
                process.terminate()
                process.wait(timeout=timeout)
            except Exception:
                pass

    def _publish_processing_status(
        self,
        message_id: str,
        status: str,
        *,
        activity: str | None = None,
        tool_name: str | None = None,
        progress: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
        reason: str | None = None,
        error_message: str | None = None,
        retry_after_seconds: int | None = None,
        parent_message_id: str | None = None,
    ) -> None:
        if not self._send_client:
            return
        try:
            self._send_client.set_agent_processing_status(
                message_id,
                status,
                agent_name=self.name,
                space_id=self.space_id,
                activity=activity,
                tool_name=tool_name,
                progress=progress,
                detail=detail,
                reason=reason,
                error_message=error_message,
                retry_after_seconds=retry_after_seconds,
                parent_message_id=parent_message_id,
            )
        except Exception:
            pass

    @staticmethod
    def _processing_status_metadata(event: dict[str, Any]) -> dict[str, Any]:
        progress = event.get("progress") if isinstance(event.get("progress"), dict) else None
        detail = event.get("detail") if isinstance(event.get("detail"), dict) else None
        if detail is None and isinstance(event.get("initial_data"), dict):
            detail = event.get("initial_data")
        reason = str(event.get("reason") or "").strip() or None
        error_message = str(event.get("error_message") or "").strip() or None
        parent_message_id = str(event.get("parent_message_id") or "").strip() or None

        retry_after_seconds = None
        retry_after_raw = event.get("retry_after_seconds")
        if retry_after_raw is not None:
            try:
                retry_after_seconds = int(retry_after_raw)
            except (TypeError, ValueError):
                retry_after_seconds = None

        return {
            "progress": progress,
            "detail": detail,
            "reason": reason,
            "error_message": error_message,
            "retry_after_seconds": retry_after_seconds,
            "parent_message_id": parent_message_id,
        }

    def _record_tool_call(self, *, message_id: str, event: dict[str, Any]) -> None:
        if not self._send_client:
            return
        tool_name = str(event.get("tool_name") or event.get("tool") or "").strip()
        if not tool_name:
            return
        tool_call_id = str(event.get("tool_call_id") or uuid.uuid4())
        arguments = event.get("arguments") if isinstance(event.get("arguments"), dict) else None
        initial_data = event.get("initial_data") if isinstance(event.get("initial_data"), dict) else None
        duration_raw = event.get("duration_ms")
        try:
            duration_ms = int(duration_raw) if duration_raw is not None else None
        except (TypeError, ValueError):
            duration_ms = None
        try:
            self._send_client.record_tool_call(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                space_id=self.space_id,
                tool_action=str(event.get("tool_action") or event.get("tool_action_name") or event.get("command") or "")
                or None,
                resource_uri=str(event.get("resource_uri") or "ui://gateway/tool-call"),
                arguments_hash=_hash_tool_arguments(arguments),
                kind=str(event.get("kind_name") or event.get("result_kind") or "gateway_runtime"),
                arguments=arguments,
                initial_data=initial_data,
                status=str(event.get("status") or "success"),
                duration_ms=duration_ms,
                agent_name=self.name,
                agent_id=self.agent_id,
                message_id=message_id,
                correlation_id=str(event.get("correlation_id") or message_id),
            )
            record_gateway_activity(
                "tool_call_recorded",
                entry=self.entry,
                message_id=message_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        except Exception as exc:
            record_gateway_activity(
                "tool_call_record_failed",
                entry=self.entry,
                message_id=message_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                error=str(exc)[:400],
            )

    def _handle_exec_event(self, event: dict[str, Any], *, message_id: str) -> None:
        kind = str(event.get("kind") or event.get("type") or "").strip().lower()
        if not kind:
            return
        if kind == "status":
            status = str(event.get("status") or "processing").strip()
            if status == "completed":
                self._mark_completed_seen(message_id)
            activity = str(event.get("message") or event.get("activity") or "").strip() or None
            tool_name = str(event.get("tool") or event.get("tool_name") or "").strip() or None
            metadata = self._processing_status_metadata(event)
            updates: dict[str, Any] = {}
            updates["current_status"] = status
            if activity is not None:
                updates["current_activity"] = activity[:240]
            if tool_name is not None:
                updates["current_tool"] = tool_name[:120]
            if status == "completed":
                updates["current_status"] = None
                updates.setdefault("current_activity", None)
                updates.setdefault("current_tool", None)
                updates["current_tool_call_id"] = None
            if updates:
                self._update_state(**updates)
            if message_id:
                self._publish_processing_status(
                    message_id,
                    status,
                    activity=activity,
                    tool_name=tool_name,
                    **metadata,
                )
            record_gateway_activity(
                "runtime_status",
                entry=self.entry,
                message_id=message_id,
                status=status,
                activity_message=activity,
                tool_name=tool_name,
            )
            return

        if kind == "tool_start":
            tool_name = str(event.get("tool_name") or event.get("tool") or "tool").strip()
            tool_call_id = str(event.get("tool_call_id") or uuid.uuid4())
            activity = str(event.get("message") or f"Using {tool_name}").strip()
            status = str(event.get("status") or "tool_call").strip()
            metadata = self._processing_status_metadata(event)
            self._update_state(
                current_status=status,
                current_activity=activity[:240],
                current_tool=tool_name[:120] or None,
                current_tool_call_id=tool_call_id,
            )
            if message_id:
                self._publish_processing_status(
                    message_id,
                    status,
                    activity=activity,
                    tool_name=tool_name or None,
                    **metadata,
                )
            record_gateway_activity(
                "tool_started",
                entry=self.entry,
                message_id=message_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_action=str(event.get("tool_action") or event.get("command") or "") or None,
            )
            return

        if kind == "tool_result":
            tool_name = str(event.get("tool_name") or event.get("tool") or "tool").strip()
            tool_call_id = str(event.get("tool_call_id") or uuid.uuid4())
            status = str(event.get("status") or "success").strip()
            metadata = self._processing_status_metadata(event)
            self._record_tool_call(message_id=message_id, event=event)
            step_status = (
                "tool_complete" if status.lower() in {"success", "completed", "ok", "tool_complete"} else "error"
            )
            self._update_state(
                current_status=None if step_status == "tool_complete" else step_status,
                current_activity=None,
                current_tool=None,
                current_tool_call_id=None,
            )
            if message_id:
                self._publish_processing_status(
                    message_id,
                    step_status,
                    tool_name=tool_name or None,
                    detail=metadata["detail"],
                    reason=metadata["reason"] or (None if step_status == "tool_complete" else status),
                    error_message=metadata["error_message"],
                    retry_after_seconds=metadata["retry_after_seconds"],
                    parent_message_id=metadata["parent_message_id"],
                )
            record_gateway_activity(
                "tool_finished",
                entry=self.entry,
                message_id=message_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                status=status,
            )
            return

        if kind == "activity":
            activity = str(event.get("message") or event.get("activity") or "").strip()
            if activity:
                self._update_state(current_activity=activity[:240])
            record_gateway_activity(
                "runtime_activity",
                entry=self.entry,
                message_id=message_id,
                activity_message=activity or None,
            )

    def _sentinel_session_id(self, session_key: str) -> str | None:
        with self._state_lock:
            return self._sentinel_sessions.get(session_key)

    def _remember_sentinel_session(self, session_key: str, session_id: str | None) -> None:
        if not session_id:
            return
        with self._state_lock:
            self._sentinel_sessions[session_key] = session_id

    def _build_sentinel_cmd(self, runtime_name: str, session_id: str | None) -> list[str]:
        command_override = str(self.entry.get("sentinel_command") or "").strip()
        if command_override:
            command = shlex.split(command_override)
            if session_id:
                command.extend(["--resume", session_id])
            return command
        if runtime_name == "codex":
            return _build_sentinel_codex_cmd(self.entry, session_id)
        return _build_sentinel_claude_cmd(self.entry, session_id)

    def _handle_sentinel_cli_prompt(self, prompt: str, *, message_id: str, data: dict[str, Any] | None = None) -> str:
        runtime_name = _sentinel_runtime_name(self.entry)
        session_key = _sentinel_session_key(self.entry, data, message_id)
        existing_session = self._sentinel_session_id(session_key)
        cmd = self._build_sentinel_cmd(runtime_name, existing_session)
        env = sanitize_exec_env(prompt, self.entry)
        if message_id:
            env["AX_GATEWAY_MESSAGE_ID"] = message_id
        if self.space_id:
            env["AX_GATEWAY_SPACE_ID"] = self.space_id
        env["AX_GATEWAY_SENTINEL_SESSION_KEY"] = session_key

        start_activity = (
            f"Resuming {runtime_name} sentinel session"
            if existing_session
            else f"Starting {runtime_name} sentinel session"
        )
        self._publish_processing_status(message_id, "thinking", activity=start_activity)
        self._update_state(current_status="thinking", current_activity=start_activity[:240])
        record_gateway_activity(
            "runtime_status",
            entry=self.entry,
            message_id=message_id,
            status="thinking",
            activity_message=start_activity,
        )

        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self.entry.get("workdir") or None,
                env=env,
            )
        except FileNotFoundError:
            return f"(handler not found: {cmd[0]})"

        if process.stdin is not None:
            try:
                process.stdin.write(prompt)
                process.stdin.close()
            except Exception:
                pass

        accumulated_text = ""
        stderr_lines: list[str] = []
        new_session_id: str | None = None
        last_activity_time = time.time()
        exit_reason = "done"
        timeout_seconds = runtime_timeout_seconds(self.entry)
        finished = threading.Event()

        def _consume_stderr() -> None:
            if process.stderr is None:
                return
            for raw in process.stderr:
                stderr_lines.append(raw)

        def _timeout_watchdog() -> None:
            nonlocal exit_reason
            while not finished.wait(timeout=5.0):
                if time.time() - last_activity_time <= timeout_seconds:
                    continue
                exit_reason = "timeout"
                try:
                    process.kill()
                except Exception:
                    pass
                return

        stderr_thread = threading.Thread(target=_consume_stderr, daemon=True, name=f"gw-sentinel-stderr-{self.name}")
        watchdog_thread = threading.Thread(
            target=_timeout_watchdog, daemon=True, name=f"gw-sentinel-watchdog-{self.name}"
        )
        stderr_thread.start()
        watchdog_thread.start()

        try:
            if process.stdout is not None:
                for raw in process.stdout:
                    line = raw.strip()
                    if not line:
                        continue
                    last_activity_time = time.time()
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue

                    event_type = str(event.get("type") or "")
                    if runtime_name == "codex":
                        if event_type == "thread.started":
                            new_session_id = str(event.get("thread_id") or "") or new_session_id
                        elif event_type == "item.started":
                            item = event.get("item") if isinstance(event.get("item"), dict) else {}
                            if str(item.get("type") or "") != "agent_message":
                                self._handle_sentinel_tool_item(item, message_id=message_id, phase="start")
                        elif event_type == "item.completed":
                            item = event.get("item") if isinstance(event.get("item"), dict) else {}
                            item_type = str(item.get("type") or "")
                            if item_type == "agent_message":
                                text = str(item.get("text") or "").strip()
                                if text:
                                    accumulated_text = text
                            else:
                                self._handle_sentinel_tool_item(item, message_id=message_id, phase="result")
                        continue

                    if event_type == "assistant":
                        for block in event.get("message", {}).get("content", []):
                            if not isinstance(block, dict):
                                continue
                            block_type = str(block.get("type") or "")
                            if block_type == "text":
                                accumulated_text = str(block.get("text") or accumulated_text)
                            elif block_type == "tool_use":
                                self._handle_claude_tool_use(block, message_id=message_id)
                    elif event_type == "content_block_delta":
                        delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
                        if delta.get("type") == "text_delta":
                            accumulated_text += str(delta.get("text") or "")
                    elif event_type == "result":
                        result_text = str(event.get("result") or "").strip()
                        if result_text:
                            accumulated_text = result_text
                        new_session_id = str(event.get("session_id") or "") or new_session_id
        except Exception as exc:
            exit_reason = "crashed"
            record_gateway_activity(
                "runtime_error",
                entry=self.entry,
                message_id=message_id or None,
                error=f"sentinel stream error: {str(exc)[:360]}",
            )
        finally:
            finished.set()

        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        stderr_thread.join(timeout=1.0)

        if process.returncode != 0 and exit_reason == "done":
            exit_reason = "crashed"
        self._remember_sentinel_session(session_key, new_session_id)
        if new_session_id:
            record_gateway_activity(
                "runtime_session_saved",
                entry=self.entry,
                message_id=message_id,
                session_key=session_key,
                session_id=new_session_id[:24],
            )

        final = accumulated_text.strip()
        stderr = "".join(stderr_lines).strip()
        if exit_reason == "timeout":
            raise GatewayRuntimeTimeoutError(timeout_seconds, runtime_type=runtime_name)
        if exit_reason == "crashed":
            if final:
                return final
            if stderr:
                return f"Hit an error processing that.\n\n(stderr: {stderr[:400]})"
            return "Hit an error processing that."
        return final or "Completed with no text output."

    def _handle_sentinel_tool_item(self, item: dict[str, Any], *, message_id: str, phase: str) -> None:
        item_type = str(item.get("type") or "tool").strip() or "tool"
        tool_call_id = str(item.get("id") or item.get("call_id") or uuid.uuid4())
        if item_type == "command_execution":
            command = str(item.get("command") or "").strip()
            arguments = {"command": command} if command else None
            initial_data: dict[str, Any] = {}
            if item.get("aggregated_output"):
                initial_data["output"] = str(item.get("aggregated_output"))[:4000]
            if item.get("exit_code") is not None:
                initial_data["exit_code"] = item.get("exit_code")
            event = {
                "kind": "tool_start" if phase == "start" else "tool_result",
                "tool_name": "shell",
                "tool_action": command or "command_execution",
                "tool_call_id": tool_call_id,
                "arguments": arguments,
                "initial_data": initial_data or None,
                "message": _summarize_sentinel_command(command) if command else "Running command...",
                "status": "tool_call"
                if phase == "start"
                else ("tool_complete" if int(item.get("exit_code") or 0) == 0 else "error"),
            }
        else:
            event = {
                "kind": "tool_start" if phase == "start" else "tool_result",
                "tool_name": item_type,
                "tool_action": str(item.get("title") or item_type),
                "tool_call_id": tool_call_id,
                "initial_data": {"item": item},
                "message": f"Using {item_type}",
                "status": "tool_call" if phase == "start" else "tool_complete",
            }
        self._handle_exec_event(event, message_id=message_id)

    def _handle_claude_tool_use(self, block: dict[str, Any], *, message_id: str) -> None:
        tool_name = str(block.get("name") or "tool").strip()
        tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
        tool_call_id = str(block.get("id") or uuid.uuid4())
        event = {
            "kind": "tool_start",
            "tool_name": tool_name,
            "tool_action": str(tool_input.get("command") or tool_name),
            "tool_call_id": tool_call_id,
            "arguments": tool_input,
            "message": _sentinel_tool_summary(tool_name, tool_input),
            "status": "tool_call",
        }
        self._handle_exec_event(event, message_id=message_id)

    def _handle_prompt(self, prompt: str, *, message_id: str, data: dict[str, Any] | None = None) -> str:
        runtime_type = str(self.entry.get("runtime_type") or "echo").lower()
        if runtime_type == "echo":
            return _echo_handler(prompt, self.entry)
        if runtime_type in {"inbox", "passive", "monitor"}:
            return ""
        if _is_sentinel_cli_runtime(runtime_type):
            return self._handle_sentinel_cli_prompt(prompt, message_id=message_id, data=data)
        if runtime_type in {"exec", "command"}:
            command = str(self.entry.get("exec_command") or "").strip()
            if not command:
                raise ValueError("exec runtime requires exec_command")
            return _run_exec_handler(
                command,
                prompt,
                self.entry,
                message_id=message_id or None,
                space_id=self.space_id,
                timeout_seconds=runtime_timeout_seconds(self.entry),
                on_event=lambda event: self._handle_exec_event(event, message_id=message_id),
            )
        raise ValueError(f"Unsupported runtime_type: {runtime_type}")

    def _gateway_message_metadata(self, parent_message_id: str | None = None) -> dict[str, Any]:
        registry = load_gateway_registry()
        gateway = registry.get("gateway", {})
        metadata: dict[str, Any] = {
            "control_plane": "gateway",
            "gateway": {
                "managed": True,
                "gateway_id": gateway.get("gateway_id"),
                "agent_name": self.name,
                "agent_id": self.agent_id,
                "runtime_type": self.entry.get("runtime_type"),
                "transport": self.entry.get("transport", "gateway"),
                "credential_source": self.entry.get("credential_source", "gateway"),
            },
        }
        if parent_message_id:
            metadata["gateway"]["parent_message_id"] = parent_message_id
        return metadata

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                data = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if data is None:
                break

            message_id = str(data.get("id") or "")
            prompt = _strip_mention(str(data.get("content") or ""), self.name)
            self._update_state(backlog_depth=self._queue.qsize())
            if not prompt:
                self._queue.task_done()
                continue

            if message_id:
                runtime_type = str(self.entry.get("runtime_type") or "echo").lower()
                start_status = "processing"
                start_activity = "Preparing response"
                if runtime_type == "echo":
                    start_activity = "Composing echo reply"
                elif runtime_type in {"exec", "command"}:
                    start_activity = "Preparing runtime"
                elif _is_sentinel_cli_runtime(runtime_type):
                    start_activity = "Preparing sentinel runtime"
                if runtime_type in {"echo", "exec", "command"} or _is_sentinel_cli_runtime(runtime_type):
                    self._update_state(current_status=start_status, current_activity=start_activity[:240])
                    self._publish_processing_status(message_id, start_status, activity=start_activity)
                    record_gateway_activity(
                        "runtime_status",
                        entry=self.entry,
                        message_id=message_id,
                        status=start_status,
                        activity_message=start_activity,
                    )
            try:
                response_text = self._handle_prompt(prompt, message_id=message_id, data=data)
                if response_text and self._send_client:
                    result = self._send_client.send_message(
                        self.space_id,
                        response_text,
                        agent_id=self.agent_id,
                        parent_id=message_id or None,
                        metadata=self._gateway_message_metadata(message_id or None),
                    )
                    message = result.get("message", result) if isinstance(result, dict) else {}
                    _remember_reply_anchor(self._reply_anchor_ids, message.get("id"))
                    reply_id = message.get("id")
                    preview = response_text.strip().replace("\n", " ")
                    if len(preview) > 120:
                        preview = preview[:117] + "..."
                    self._update_state(last_reply_message_id=reply_id, last_reply_preview=preview or None)
                    record_gateway_activity(
                        "reply_sent",
                        entry=self.entry,
                        message_id=message_id or None,
                        reply_message_id=reply_id,
                        reply_preview=preview or None,
                    )
                runtime_type = str(self.entry.get("runtime_type") or "echo").lower()
                bridge_already_closed = (
                    runtime_type in {"exec", "command"} or _is_sentinel_cli_runtime(runtime_type)
                ) and self._consume_completed_seen(message_id)
                if message_id and not bridge_already_closed:
                    self._publish_processing_status(message_id, "completed")
                self._bump("processed_count")
                self._update_state(
                    current_status=None,
                    current_activity=None,
                    current_tool=None,
                    current_tool_call_id=None,
                    last_error=None,
                    last_work_completed_at=_now_iso(),
                    backlog_depth=self._queue.qsize(),
                )
            except GatewayRuntimeTimeoutError as exc:
                activity = f"Timed out after {exc.timeout_seconds}s"
                self._update_state(
                    current_status="error",
                    current_activity=activity,
                    current_tool=None,
                    current_tool_call_id=None,
                    last_error=str(exc)[:400],
                    backlog_depth=self._queue.qsize(),
                )
                if message_id:
                    self._publish_processing_status(
                        message_id,
                        "error",
                        activity=activity,
                        reason="runtime_timeout",
                        error_message=str(exc)[:400],
                        detail={"timeout_seconds": exc.timeout_seconds, "runtime_type": exc.runtime_type},
                    )
                record_gateway_activity(
                    "runtime_timeout",
                    entry=self.entry,
                    message_id=message_id or None,
                    timeout_seconds=exc.timeout_seconds,
                    runtime_type=exc.runtime_type,
                )
                self._log(f"worker timeout: {exc}")
            except Exception as exc:
                self._update_state(
                    current_status="error",
                    current_activity=None,
                    current_tool=None,
                    current_tool_call_id=None,
                    last_error=str(exc)[:400],
                    backlog_depth=self._queue.qsize(),
                )
                if message_id:
                    self._publish_processing_status(
                        message_id,
                        "error",
                        error_message=str(exc)[:400],
                    )
                record_gateway_activity(
                    "runtime_error",
                    entry=self.entry,
                    message_id=message_id or None,
                    error=str(exc)[:400],
                )
                self._log(f"worker error: {exc}")
            finally:
                self._queue.task_done()

    def _listener_loop(self) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                self._stream_client = self._new_client()
                self._send_client = self._new_client()
                timeout = httpx.Timeout(
                    connect=10.0,
                    read=SSE_IDLE_TIMEOUT_SECONDS,
                    write=10.0,
                    pool=10.0,
                )
                reconnected = backoff > 1.0
                with self._stream_client.connect_sse(space_id=self.space_id, timeout=timeout) as response:
                    self._stream_response = response
                    if response.status_code != 200:
                        raise ConnectionError(f"SSE failed: {response.status_code}")
                    self._update_state(
                        effective_state="running",
                        current_status=None,
                        last_error=None,
                        last_connected_at=_now_iso(),
                        last_listener_error_at=None,
                        last_seen_at=_now_iso(),
                        reconnect_backoff_seconds=0,
                    )
                    record_gateway_activity("listener_connected", entry=self.entry, reconnected=reconnected)
                    backoff = 1.0
                    for event_type, data in _iter_sse(response):
                        if self.stop_event.is_set():
                            break
                        if event_type in {"bootstrap", "heartbeat", "ping", "identity_bootstrap", "connected"}:
                            self._update_state(last_seen_at=_now_iso())
                            continue
                        if event_type == "agent.placement.changed" and isinstance(data, dict):
                            self._update_state(last_seen_at=_now_iso())
                            self._handle_placement_event(data)
                            continue
                        if event_type not in {"message", "mention"} or not isinstance(data, dict):
                            continue
                        message_id = str(data.get("id") or "")
                        if not message_id or message_id in self._seen_ids:
                            continue
                        if _is_self_authored(data, self.name, self.agent_id):
                            _remember_reply_anchor(self._reply_anchor_ids, message_id)
                            self._seen_ids.add(message_id)
                            continue
                        if not _should_respond(
                            data,
                            self.name,
                            self.agent_id,
                            reply_anchor_ids=self._reply_anchor_ids,
                        ):
                            continue

                        self._seen_ids.add(message_id)
                        if len(self._seen_ids) > SEEN_IDS_MAX:
                            self._seen_ids = set(list(self._seen_ids)[-SEEN_IDS_MAX // 2 :])
                        _remember_reply_anchor(self._reply_anchor_ids, message_id)
                        self._update_state(
                            last_seen_at=_now_iso(),
                            last_work_received_at=_now_iso(),
                            last_received_message_id=message_id,
                        )
                        record_gateway_activity("message_received", entry=self.entry, message_id=message_id)
                        runtime_type = str(self.entry.get("runtime_type") or "").lower()
                        try:
                            if _is_passive_runtime(runtime_type):
                                pending_items = append_agent_pending_message(self.name, data)
                                backlog_depth = len(pending_items)
                            else:
                                self._queue.put_nowait(data)
                                backlog_depth = self._queue.qsize()
                            pickup_status = "queued" if _is_passive_runtime(runtime_type) else "started"
                            accepted_activity = _gateway_pickup_activity(runtime_type, backlog_depth)
                            self._update_state(
                                backlog_depth=backlog_depth,
                                current_status=pickup_status,
                                current_activity=accepted_activity[:240],
                            )
                            self._publish_processing_status(
                                message_id,
                                pickup_status,
                                activity=accepted_activity,
                                detail={
                                    "backlog_depth": backlog_depth,
                                    "pickup_state": "queued" if _is_passive_runtime(runtime_type) else "claimed",
                                },
                            )
                            if _is_passive_runtime(self.entry.get("runtime_type")):
                                record_gateway_activity(
                                    "message_queued",
                                    entry=self.entry,
                                    message_id=message_id,
                                    backlog_depth=backlog_depth,
                                )
                            else:
                                record_gateway_activity(
                                    "message_claimed",
                                    entry=self.entry,
                                    message_id=message_id,
                                    backlog_depth=backlog_depth,
                                )
                        except queue.Full:
                            self._bump("dropped_count")
                            self._update_state(last_error="queue full", backlog_depth=self._queue.qsize())
                            self._publish_processing_status(
                                message_id,
                                "error",
                                reason="queue_full",
                                error_message="Gateway queue full",
                            )
                            record_gateway_activity(
                                "message_dropped",
                                entry=self.entry,
                                message_id=message_id,
                                error="queue full",
                            )
                            self._log("queue full; dropped message")
                        except Exception as exc:
                            self._update_state(last_error=str(exc)[:400])
                            self._publish_processing_status(
                                message_id,
                                "error",
                                error_message=str(exc)[:400],
                            )
                            record_gateway_activity(
                                "message_queue_error",
                                entry=self.entry,
                                message_id=message_id,
                                error=str(exc)[:400],
                            )
                            self._log(f"queue error: {exc}")
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                error_text = str(exc)[:400]
                event_name = "listener_error"
                if isinstance(exc, httpx.ReadTimeout):
                    error_text = f"idle timeout after {int(SSE_IDLE_TIMEOUT_SECONDS)}s without SSE heartbeat"
                    event_name = "listener_timeout"
                self._update_state(
                    effective_state="reconnecting",
                    last_error=error_text,
                    last_listener_error_at=_now_iso(),
                    reconnect_backoff_seconds=int(backoff),
                )
                record_gateway_activity(
                    event_name, entry=self.entry, error=error_text, reconnect_in_seconds=int(backoff)
                )
                self._log(f"listener error: {error_text}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                self._stream_response = None
                if self._stream_client is not None:
                    try:
                        self._stream_client.close()
                    except Exception:
                        pass
                    self._stream_client = None
        self._update_state(
            effective_state="stopped",
            backlog_depth=self._queue.qsize(),
            current_status=None,
            current_activity=None,
            current_tool=None,
            current_tool_call_id=None,
        )


class GatewayDaemon:
    """Foreground Gateway supervisor."""

    def __init__(
        self,
        *,
        client_factory: Callable[..., Any] = AxClient,
        logger: RuntimeLogger | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        self.client_factory = client_factory
        self.logger = logger or (lambda _msg: None)
        self.poll_interval = poll_interval
        self._runtimes: dict[str, ManagedAgentRuntime] = {}
        self._stop = threading.Event()

    def _log(self, message: str) -> None:
        self.logger(message)

    def stop(self) -> None:
        self._stop.set()

    def _reconcile_runtime(self, entry: dict[str, Any]) -> None:
        name = str(entry.get("name") or "")
        desired_state = str(entry.get("desired_state") or "stopped").lower()
        attestation_state = _normalized_optional_controlled(
            entry.get("attestation_state"), _CONTROLLED_ATTESTATION_STATES
        )
        approval_state = _normalized_optional_controlled(entry.get("approval_state"), _CONTROLLED_APPROVAL_STATES)
        identity_status = _normalized_optional_controlled(entry.get("identity_status"), _CONTROLLED_IDENTITY_STATUSES)
        environment_status = _normalized_optional_controlled(
            entry.get("environment_status"), _CONTROLLED_ENVIRONMENT_STATUSES
        )
        space_status = _normalized_optional_controlled(entry.get("space_status"), _CONTROLLED_SPACE_STATUSES)
        runtime = self._runtimes.get(name)
        hermes_status = hermes_setup_status(entry)
        if not hermes_status.get("ready", True):
            if runtime is not None:
                runtime.stop()
                self._runtimes.pop(name, None)
            entry.update(
                {
                    "effective_state": "error",
                    "runtime_instance_id": None,
                    "last_error": str(
                        hermes_status.get("detail") or hermes_status.get("summary") or "Hermes setup is incomplete."
                    ),
                    "current_status": None,
                    "current_activity": str(hermes_status.get("summary") or "Hermes setup is incomplete."),
                    "current_tool": None,
                    "current_tool_call_id": None,
                    "backlog_depth": 0,
                }
            )
            return
        if hermes_status.get("resolved_path"):
            entry["hermes_repo_path"] = str(hermes_status["resolved_path"])
        allowed_to_run = (
            desired_state == "running"
            and attestation_state in {None, "verified"}
            and approval_state not in {"pending", "rejected"}
            and identity_status in {None, "verified"}
            and environment_status not in {"environment_mismatch", "environment_blocked"}
            and space_status not in {"active_not_allowed", "no_active_space"}
        )
        if allowed_to_run:
            if runtime is None:
                runtime = ManagedAgentRuntime(entry, client_factory=self.client_factory, logger=self.logger)
                self._runtimes[name] = runtime
                runtime.start()
            else:
                runtime.entry.update(entry)
                runtime.start()
        else:
            if runtime is not None:
                runtime.stop()
                self._runtimes.pop(name, None)

    def _reconcile_registry(self, registry: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
        _ensure_registry_lists(registry)
        agents = registry.setdefault("agents", [])
        agent_names = {str(entry.get("name") or "") for entry in agents}
        for name, runtime in list(self._runtimes.items()):
            if name not in agent_names:
                runtime.stop()
                self._runtimes.pop(name, None)

        for entry in agents:
            entry.setdefault("transport", "gateway")
            entry.setdefault("credential_source", "gateway")
            entry.setdefault("runtime_type", "echo")
            entry.setdefault("desired_state", "stopped")
            if not str(entry.get("install_id") or "").strip():
                entry["install_id"] = str(uuid.uuid4())

            asset_id = _asset_id_for_entry(entry)
            existing_binding = (
                find_binding(registry, install_id=str(entry.get("install_id") or "").strip()) if asset_id else None
            )
            if not existing_binding and asset_id and not _bindings_for_asset(registry, asset_id):
                ensure_local_asset_binding(
                    registry,
                    entry,
                    created_via=str(entry.get("created_via") or "legacy_registry"),
                    auto_approve=True,
                )
            ensure_gateway_identity_binding(
                registry,
                entry,
                session=session,
                created_via=str(entry.get("created_via") or "legacy_registry"),
            )
            entry.update(evaluate_identity_space_binding(registry, entry))

            previous_attestation = (
                str(entry.get("attestation_state") or ""),
                str(entry.get("approval_state") or ""),
                str(entry.get("approval_id") or ""),
                str(entry.get("drift_reason") or ""),
            )
            attestation = evaluate_runtime_attestation(registry, entry)
            entry.update(attestation)
            current_attestation = (
                str(entry.get("attestation_state") or ""),
                str(entry.get("approval_state") or ""),
                str(entry.get("approval_id") or ""),
                str(entry.get("drift_reason") or ""),
            )
            if current_attestation != previous_attestation:
                state = str(entry.get("attestation_state") or "")
                if state == "verified":
                    record_gateway_activity(
                        "runtime_attested",
                        entry=entry,
                        install_id=entry.get("install_id"),
                        attestation_state=state,
                    )
                elif state == "drifted":
                    record_gateway_activity(
                        "attestation_drift_detected",
                        entry=entry,
                        install_id=entry.get("install_id"),
                        attestation_state=state,
                        approval_id=entry.get("approval_id"),
                        drift_reason=entry.get("drift_reason"),
                    )
                elif state in {"unknown", "blocked"}:
                    record_gateway_activity(
                        "invocation_blocked",
                        entry=entry,
                        install_id=entry.get("install_id"),
                        attestation_state=state,
                        approval_id=entry.get("approval_id"),
                        reason=entry.get("confidence_reason"),
                    )
            self._reconcile_runtime(entry)
            runtime = self._runtimes.get(str(entry.get("name") or ""))
            snapshot = (
                runtime.snapshot()
                if runtime is not None
                else {
                    "effective_state": entry.get("effective_state") or "stopped",
                    "runtime_instance_id": None,
                    "last_error": entry.get("last_error"),
                    "current_status": entry.get("current_status"),
                    "current_activity": entry.get("current_activity"),
                    "current_tool": entry.get("current_tool"),
                    "current_tool_call_id": entry.get("current_tool_call_id"),
                    "backlog_depth": int(entry.get("backlog_depth") or 0),
                }
            )
            entry.update(snapshot)
            entry.update(annotate_runtime_health(entry, registry=registry))

        gateway = registry.setdefault("gateway", {})
        gateway.update(
            {
                "desired_state": "running",
                "effective_state": "running" if session else "stopped",
                "session_connected": bool(session),
                "pid": os.getpid(),
                "last_started_at": gateway.get("last_started_at") or _now_iso(),
                "last_reconcile_at": _now_iso(),
            }
        )
        return registry

    def run(self, *, once: bool = False) -> None:
        session = load_gateway_session()
        if not session:
            raise RuntimeError("Gateway login required. Run `ax gateway login` first.")

        existing_pids = active_gateway_pids()
        if existing_pids:
            existing_pid = existing_pids[0]
            record_gateway_activity(
                "gateway_start_blocked",
                pid=os.getpid(),
                existing_pid=existing_pid,
                existing_pids=existing_pids,
            )
            raise RuntimeError(f"Gateway already running (pid {existing_pid}).")

        write_gateway_pid(os.getpid())
        registry = load_gateway_registry()
        registry.setdefault("gateway", {})
        registry["gateway"]["last_started_at"] = registry["gateway"].get("last_started_at") or _now_iso()
        record_gateway_activity("gateway_started", pid=os.getpid())
        previous_handlers: dict[signal.Signals, Any] = {}

        def _request_stop(_signum: int, _frame: Any) -> None:
            self.stop()

        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, _request_stop)
        try:
            while not self._stop.is_set():
                registry = load_gateway_registry()
                registry = self._reconcile_registry(registry, session)
                save_gateway_registry(registry)
                if once:
                    break
                time.sleep(self.poll_interval)
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
            runtimes = list(self._runtimes.values())
            for runtime in runtimes:
                if _is_hermes_sentinel_runtime(runtime.entry.get("runtime_type")):
                    runtime.stop(timeout=2.0)
            for runtime in runtimes:
                if not _is_hermes_sentinel_runtime(runtime.entry.get("runtime_type")):
                    runtime.stop(timeout=1.0)
            final_registry = load_gateway_registry()
            final_gateway = final_registry.setdefault("gateway", {})
            final_gateway.update(
                {
                    "desired_state": final_gateway.get("desired_state") or "stopped",
                    "effective_state": "stopped",
                    "session_connected": bool(session),
                    "pid": None,
                    "last_reconcile_at": _now_iso(),
                }
            )
            for entry in final_registry.get("agents", []):
                name = str(entry.get("name") or "")
                entry.update({"effective_state": "stopped", "backlog_depth": 0})
                runtime = self._runtimes.get(name)
                if runtime is not None:
                    entry.update(runtime.snapshot())
                entry.update(annotate_runtime_health(entry, registry=final_registry))
            save_gateway_registry(final_registry)
            record_gateway_activity("gateway_stopped")
            clear_gateway_pid(os.getpid())
