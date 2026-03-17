#!/usr/bin/env python3
"""aX CLI Agent Listener — SSE-based mention monitor for any agent.

Implements AGENT-LISTENER-001: API-first CLI agent pattern.
Connects via SSE, listens for @mentions, responds via REST API.
No webhooks, no dispatch — pure pull model.

Setup:
    1. Configure ~/.ax/config.toml with agent token and identity
    2. Run: python3 ax_listener.py
    3. Or:  python3 ax_listener.py --dry-run   (watch only)

Config (~/.ax/config.toml):
    token = "axp_u_..."
    base_url = "http://localhost:8002"
    agent_name = "my_agent"
    agent_id = "uuid-..."
    space_id = "uuid-..."

Environment variables override config.toml:
    AX_TOKEN, AX_BASE_URL, AX_AGENT_NAME, AX_AGENT_ID, AX_SPACE_ID

NOTE: The API blocks self-mentions — if reply content contains @agent_name,
the API returns 200 with empty body and silently drops the message.
Avoid putting @{your_agent_name} in reply content.
"""

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import httpx
except ImportError:
    print("Error: httpx is required. Install with: pip install httpx")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ax_listener")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    cfg = {}
    config_path = Path.home() / ".ax" / "config.toml"
    if config_path.exists():
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # Python < 3.11
        cfg = tomllib.loads(config_path.read_text())
    return cfg


_cfg = _load_config()

TOKEN = os.environ.get("AX_TOKEN", _cfg.get("token", ""))
BASE_URL = os.environ.get("AX_BASE_URL", _cfg.get("base_url", "http://localhost:8002"))
AGENT_NAME = os.environ.get("AX_AGENT_NAME", _cfg.get("agent_name", ""))
AGENT_ID = os.environ.get("AX_AGENT_ID", _cfg.get("agent_id", ""))
SPACE_ID = os.environ.get("AX_SPACE_ID", _cfg.get("space_id", ""))
DRY_RUN = "--dry-run" in sys.argv


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

def _headers() -> dict:
    h = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }
    if AGENT_ID:
        h["X-Agent-Id"] = AGENT_ID
    return h


def send_message(content: str, parent_id: str | None = None) -> dict | None:
    """Send a message as this agent via the REST API."""
    if DRY_RUN:
        logger.info(f"[DRY RUN] Would send: {content[:100]}")
        return None

    body = {
        "content": content,
        "space_id": SPACE_ID,
        "channel": "main",
        "message_type": "text",
    }
    if parent_id:
        body["parent_id"] = parent_id

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                f"{BASE_URL}/api/v1/messages",
                json=body,
                headers=_headers(),
            )
            if resp.status_code == 200:
                msg = resp.json().get("message", {})
                logger.info(f"Sent reply: {msg.get('id', '?')[:12]}")
                return msg
            else:
                logger.warning(f"Send failed: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        logger.error(f"Send error: {e}")
    return None


def get_messages(limit: int = 5) -> list:
    """Fetch recent messages for context."""
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"{BASE_URL}/api/v1/messages",
                params={"limit": limit, "channel": "main"},
                headers=_headers(),
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("messages", data.get("items", []))
    except Exception as e:
        logger.error(f"Get messages error: {e}")
    return []


# ---------------------------------------------------------------------------
# Mention detection (AGENT-LISTENER-001 §5)
# ---------------------------------------------------------------------------

def is_mentioned(event_data: dict) -> bool:
    """Check if this message mentions us."""
    # Primary: structured mentions array
    mentions = event_data.get("mentions", [])
    if AGENT_NAME.lower() in [m.lower() for m in mentions]:
        return True

    # Fallback: content text
    content = event_data.get("content", "")
    if f"@{AGENT_NAME.lower()}" in content.lower():
        return True

    return False


def _get_author_name(event_data: dict) -> str:
    """Extract author name — handles both message format (dict) and mention format (string)."""
    author = event_data.get("author", "")
    if isinstance(author, dict):
        return author.get("name", "")
    return str(author)  # mention events use a plain string


def should_respond(event_data: dict) -> bool:
    """Determine if we should respond to this event (loop prevention)."""
    author_name = _get_author_name(event_data)

    # Never respond to ourselves
    if author_name.lower() == AGENT_NAME.lower():
        return False

    # Never respond to aX concierge (avoid loops — aX handles routing)
    if author_name == "aX":
        return False

    # Only respond if actually @mentioned
    return is_mentioned(event_data)


# ---------------------------------------------------------------------------
# Mention handler — CUSTOMIZE THIS for your agent
# ---------------------------------------------------------------------------

def handle_mention(event_data: dict) -> None:
    """Called when a message mentioning this agent is detected.

    Override this function with your agent's logic.
    """
    author_name = _get_author_name(event_data) or "unknown"
    content = event_data.get("content", "")
    msg_id = event_data.get("id", "")

    logger.info(f"MENTION from @{author_name}: {content[:120]}")

    # Default: acknowledge the mention
    send_message(
        f"@{author_name} Got it — I see your message. Working on it.",
        parent_id=msg_id,
    )


# ---------------------------------------------------------------------------
# SSE listener
# ---------------------------------------------------------------------------

def connect_sse():
    """Connect to SSE and yield parsed events."""
    url = f"{BASE_URL}/api/sse/messages"
    params = {"token": TOKEN}

    with httpx.Client(timeout=None) as client:
        with client.stream("GET", url, params=params, headers=_headers()) as resp:
            if resp.status_code != 200:
                logger.error(f"SSE connection failed: {resp.status_code}")
                return

            event_type = None
            data_lines = []

            for line in resp.iter_lines():
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif line == "":
                    if event_type and data_lines:
                        data_str = "\n".join(data_lines)
                        yield event_type, data_str
                    event_type = None
                    data_lines = []


def run():
    """Main loop — connect SSE, filter mentions, handle them."""
    logger.info(f"Agent: {AGENT_NAME} ({AGENT_ID[:12]}...)")
    logger.info(f"Space: {SPACE_ID[:12]}...")
    logger.info(f"API:   {BASE_URL}")
    logger.info(f"Mode:  {'DRY RUN' if DRY_RUN else 'LIVE'}")
    logger.info("")

    # Dedup: track recently handled message IDs to avoid double-responding
    # (same message arrives as both 'message' and 'mention' events)
    _handled_ids: set[str] = set()
    _HANDLED_MAX = 100

    backoff = 1

    while True:
        try:
            logger.info("Connecting to SSE...")
            for event_type, data_str in connect_sse():
                backoff = 1

                if event_type in ("connected", "bootstrap", "heartbeat", "identity_bootstrap"):
                    if event_type == "connected":
                        try:
                            data = json.loads(data_str) if data_str.startswith("{") else {}
                            logger.info(
                                f"Connected — space={data.get('space_id', SPACE_ID)[:12]} "
                                f"user={data.get('user', '?')}"
                            )
                        except (json.JSONDecodeError, TypeError):
                            logger.info("Connected to SSE stream")
                        logger.info(f"Listening for @{AGENT_NAME} mentions...")
                    continue

                if event_type in ("message", "mention"):
                    try:
                        data = json.loads(data_str) if data_str.startswith("{") else None
                    except (json.JSONDecodeError, TypeError):
                        data = None

                    if not isinstance(data, dict):
                        continue

                    # Dedup: skip if we already handled this message
                    msg_id = data.get("id", "")
                    if msg_id in _handled_ids:
                        continue

                    if should_respond(data):
                        _handled_ids.add(msg_id)
                        if len(_handled_ids) > _HANDLED_MAX:
                            _handled_ids.clear()
                        handle_mention(data)

                # Skip all other event types silently

        except httpx.ConnectError:
            logger.warning(f"Connection lost. Reconnecting in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except KeyboardInterrupt:
            logger.info("Shutting down.")
            break
        except Exception as e:
            logger.error(f"Error: {e}. Reconnecting in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not TOKEN:
        print("Error: No token. Set AX_TOKEN or configure ~/.ax/config.toml")
        sys.exit(1)
    if not AGENT_NAME:
        print("Error: No agent_name. Set AX_AGENT_NAME or configure ~/.ax/config.toml")
        sys.exit(1)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    run()
