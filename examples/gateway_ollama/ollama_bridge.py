#!/usr/bin/env python3
"""Gateway-managed bridge for a local Ollama model.

This bridge is designed for `ax gateway agents add ... --template ollama`.
It pulls recent messages from the agent's aX space, formats them as a
multi-turn conversation, and streams a reply back from a local Ollama
server using /api/chat. The result: the agent has session continuity
across messages — multi-user, multi-agent context drawn straight from
the aX message history (the canonical source) rather than a separate
local cache.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error, request

EVENT_PREFIX = "AX_GATEWAY_EVENT "
DEFAULT_OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
DEFAULT_OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")
HISTORY_LIMIT = int(os.environ.get("AX_OLLAMA_HISTORY_LIMIT", "20") or 20)
HISTORY_FETCH_LIMIT = int(os.environ.get("AX_OLLAMA_HISTORY_FETCH_LIMIT", str(max(50, HISTORY_LIMIT * 3))) or 50)
HISTORY_CHAR_BUDGET = int(os.environ.get("AX_OLLAMA_HISTORY_CHAR_BUDGET", "12000") or 12000)
SYSTEM_PROMPT_TEMPLATE = """\
You are @{agent_name}, an agent connected to aX through the local Gateway.
aX is a shared agent network where humans, service accounts, and AI agents can
send messages, route tasks, and coordinate work across spaces.

You are running as a local Ollama model. Answer as @{agent_name}, not as the
Gateway or switchboard service. You may receive relevant conversation history
from this aX space: messages addressed to you and messages you authored. You do
not see every message in the space, and you should not imply that you do.

Use the provided history for continuity. If the answer depends on context you
were not given, say that directly. Do not claim to have called tools or inspected
files unless those capabilities are explicitly provided in the current message.
"""

# Make the ax_cli package importable when the bridge is launched from the
# ax-cli workdir (the gateway's default for managed agents).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def emit_event(payload: dict[str, Any]) -> None:
    print(f"{EVENT_PREFIX}{json.dumps(payload, sort_keys=True)}", flush=True)


def _read_prompt() -> str:
    if len(sys.argv) > 1 and sys.argv[-1] != "-":
        return sys.argv[-1]
    env_prompt = os.environ.get("AX_MENTION_CONTENT", "").strip()
    if env_prompt:
        return env_prompt
    return sys.stdin.read().strip()


def _resolve_token() -> str | None:
    token_file = os.environ.get("AX_TOKEN_FILE", "").strip()
    if token_file:
        path = Path(token_file).expanduser()
        if path.exists():
            return path.read_text().strip() or None
    return os.environ.get("AX_TOKEN", "").strip() or None


def _build_client():
    base_url = os.environ.get("AX_BASE_URL", "https://paxai.app").strip() or "https://paxai.app"
    token = _resolve_token()
    if not token:
        emit_event({"kind": "activity", "activity": "no agent token; running without history context"})
        return None
    agent_id = os.environ.get("AX_GATEWAY_AGENT_ID", "").strip() or os.environ.get("AX_AGENT_ID", "").strip() or None
    agent_name = os.environ.get("AX_GATEWAY_AGENT_NAME", "").strip() or os.environ.get("AX_AGENT_NAME", "").strip() or None
    try:
        from ax_cli.client import AxClient
    except Exception as exc:  # noqa: BLE001
        emit_event({"kind": "activity", "activity": f"history fetch unavailable: {exc}"})
        return None
    try:
        return AxClient(base_url=base_url, token=token, agent_id=agent_id, agent_name=agent_name)
    except Exception as exc:  # noqa: BLE001
        emit_event({"kind": "activity", "activity": f"client init failed: {exc}"})
        return None


def _looks_like_attribution(text: str) -> bool:
    """Skip messages that are pure system attribution chatter."""
    stripped = text.strip()
    if not stripped:
        return True
    return False


def _message_mentions_agent(msg: dict[str, Any], agent_name: str, agent_id: str | None) -> bool:
    content = str(msg.get("content") or msg.get("text") or "")
    if f"@{agent_name}".lower() in content.lower():
        return True
    metadata = msg.get("metadata")
    mentions = metadata.get("mentions") if isinstance(metadata, dict) else None
    if not isinstance(mentions, list):
        return False
    normalized_name = agent_name.strip().lstrip("@").lower()
    normalized_id = (agent_id or "").strip()
    for mention in mentions:
        if isinstance(mention, str) and mention.strip().lstrip("@").lower() == normalized_name:
            return True
        if isinstance(mention, dict):
            mention_name = str(mention.get("agent_name") or mention.get("name") or "").strip().lstrip("@").lower()
            mention_id = str(mention.get("agent_id") or mention.get("id") or "").strip()
            if mention_name and mention_name == normalized_name:
                return True
            if normalized_id and mention_id == normalized_id:
                return True
    return False


def _message_authored_by_agent(msg: dict[str, Any], agent_name: str, agent_id: str | None) -> bool:
    normalized_name = agent_name.strip().lstrip("@").lower()
    normalized_id = (agent_id or "").strip()
    names = [
        msg.get("sender_agent_name"),
        msg.get("agent_name"),
        msg.get("sender_name"),
        msg.get("from_name"),
        msg.get("display_name"),
    ]
    if any(str(name or "").strip().lstrip("@").lower() == normalized_name for name in names):
        return True
    ids = [msg.get("agent_id"), msg.get("sender_agent_id")]
    return bool(normalized_id and any(str(value or "").strip() == normalized_id for value in ids))


def _strip_agent_mention(text: str, agent_name: str) -> str:
    token = f"@{agent_name}".lower()
    stripped = text.strip()
    if stripped.lower().startswith(token):
        return stripped[len(token) :].strip()
    return stripped


def _system_prompt(agent_name: str) -> str:
    normalized = agent_name.strip().lstrip("@") or "ollama-agent"
    return SYSTEM_PROMPT_TEMPLATE.format(agent_name=normalized).strip()


def _shape_history(prompt: str) -> list[dict[str, str]]:
    """Return Ollama-style messages[] for /api/chat.

    Falls back to a single-turn user message when history can't be fetched.
    """
    agent_name = os.environ.get("AX_GATEWAY_AGENT_NAME", "").strip()
    system_message = {"role": "system", "content": _system_prompt(agent_name or "ollama-agent")}
    fallback = [system_message, {"role": "user", "content": prompt}]
    agent_id = os.environ.get("AX_GATEWAY_AGENT_ID", "").strip() or os.environ.get("AX_AGENT_ID", "").strip() or None
    space_id = os.environ.get("AX_GATEWAY_SPACE_ID", "").strip() or os.environ.get("AX_SPACE_ID", "").strip()
    if not agent_name or not space_id:
        return fallback

    client = _build_client()
    if client is None:
        return fallback

    try:
        payload = client.list_messages(limit=HISTORY_FETCH_LIMIT, space_id=space_id)
    except Exception as exc:  # noqa: BLE001
        emit_event({"kind": "activity", "activity": f"history fetch failed: {exc}"})
        return fallback

    items = payload.get("messages") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return fallback

    # Backend returns newest-first. Select this agent's recent exchange from the
    # newest side so busy team traffic cannot evict the latest direct message.
    selected_newest: list[dict[str, str]] = []
    used_chars = 0
    incoming_seen = False
    for msg in items:
        if not isinstance(msg, dict):
            continue
        text = str(msg.get("content") or msg.get("text") or "").strip()
        if _looks_like_attribution(text):
            continue
        authored_by_agent = _message_authored_by_agent(msg, agent_name, agent_id)
        addressed_to_agent = _message_mentions_agent(msg, agent_name, agent_id)
        if not authored_by_agent and not addressed_to_agent:
            continue
        # Treat messages authored by THIS agent as "assistant" turns; everything else
        # (humans, other agents) is "user" context. Ollama's chat format allows multiple
        # user turns in sequence, which Hermes-style agents handle fine.
        role = "assistant" if authored_by_agent else "user"
        content = text if role == "assistant" else _strip_agent_mention(text, agent_name)
        # If this is the freshly-received prompt, mark that we've seen it so we don't
        # double-append below.
        if role == "user" and (text.strip() == prompt.strip() or content.strip() == prompt.strip()):
            incoming_seen = True
        # Char budget: count chars and stop when over.
        added = len(content) + 8
        if used_chars + added > HISTORY_CHAR_BUDGET and selected_newest:
            break
        selected_newest.append({"role": role, "content": content})
        used_chars += added
        if len(selected_newest) >= HISTORY_LIMIT:
            break

    # Ollama expects chronological order.
    shaped = [system_message, *reversed(selected_newest)]

    if not incoming_seen:
        # Belt-and-suspenders — make sure the actual prompt is the last turn.
        shaped.append({"role": "user", "content": prompt})

    if len(shaped) == 1:
        return fallback
    return shaped


def _chat(messages: list[dict[str, str]]) -> str:
    model = DEFAULT_OLLAMA_MODEL
    endpoint = f"{DEFAULT_OLLAMA_BASE_URL}/api/chat"
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    chat_turns = [msg for msg in messages if msg.get("role") != "system"]
    history_turns = max(0, len(chat_turns) - 1)
    emit_event(
        {
            "kind": "status",
            "status": "thinking",
            "message": f"Preparing Ollama request ({model}, {history_turns} prior turns)",
        }
    )
    emit_event({"kind": "status", "status": "processing", "message": f"Calling Ollama ({model})"})

    req = request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    started = time.monotonic()
    chunks: list[str] = []
    first_token_seen = False
    last_activity_at = 0.0
    try:
        with request.urlopen(req, timeout=300) as response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    continue
                if payload.get("error"):
                    raise RuntimeError(str(payload["error"]))
                msg = payload.get("message") or {}
                text = str(msg.get("content") or "")
                if text:
                    chunks.append(text)
                    now = time.monotonic()
                    if not first_token_seen:
                        first_token_seen = True
                        emit_event({"kind": "status", "status": "processing", "message": f"Ollama is responding ({model})"})
                    if now - last_activity_at >= 1.0:
                        preview = "".join(chunks).strip().replace("\n", " ")
                        if len(preview) > 180:
                            preview = "..." + preview[-177:]
                        emit_event(
                            {
                                "kind": "activity",
                                "activity": f"{model}: {preview}" if preview else f"Streaming response from {model}...",
                            }
                        )
                        last_activity_at = now
                if payload.get("done"):
                    break
    except error.URLError as exc:
        raise RuntimeError(f"Failed to reach Ollama at {endpoint}: {exc.reason}") from exc

    duration_ms = int((time.monotonic() - started) * 1000)
    emit_event(
        {
            "kind": "status",
            "status": "completed",
            "message": f"Ollama completed in {duration_ms}ms",
            "detail": {"model": model, "duration_ms": duration_ms, "history_turns": history_turns},
        }
    )
    return "".join(chunks).strip()


def main() -> int:
    prompt = _read_prompt()
    if not prompt:
        print("(no mention content received)", file=sys.stderr)
        return 1

    messages = _shape_history(prompt)
    try:
        reply = _chat(messages)
    except Exception as exc:
        emit_event({"kind": "status", "status": "error", "error_message": str(exc)})
        print(f"Ollama bridge failed: {exc}")
        return 1

    print(reply or f"Ollama ({DEFAULT_OLLAMA_MODEL}) finished without text.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
