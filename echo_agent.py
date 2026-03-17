#!/usr/bin/env python3
"""echo_agent.py — Minimal SSE echo bot for aX.

Connects to SSE, listens for messages, echoes them back via REST API.
Proves the SSE-receive → API-respond loop works end-to-end.

Usage:
    python3 echo_agent.py              # live mode — echoes messages back
    python3 echo_agent.py --dry-run    # watch only — prints but doesn't reply
    python3 echo_agent.py --verbose    # show all SSE events (debug)

Config: reads from .ax/config.toml (project-local) or ~/.ax/config.toml

NOTE: Avoid putting @agent_name in reply content — the API blocks self-mentions
to prevent notification loops (returns 200 with empty body).
"""

import json
import os
import signal
import sys
import time
import urllib.request
from pathlib import Path

try:
    import httpx
except ImportError:
    print("Error: httpx required. pip install httpx")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    for p in [Path(".ax/config.toml"), Path.home() / ".ax" / "config.toml"]:
        if p.exists():
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib
            return tomllib.loads(p.read_text())
    return {}

_cfg = _load_config()

TOKEN = os.environ.get("AX_TOKEN", _cfg.get("token", ""))
BASE_URL = os.environ.get("AX_BASE_URL", _cfg.get("base_url", "http://localhost:8002"))
AGENT_NAME = os.environ.get("AX_AGENT_NAME", _cfg.get("agent_name", ""))
AGENT_ID = os.environ.get("AX_AGENT_ID", _cfg.get("agent_id", ""))
SPACE_ID = os.environ.get("AX_SPACE_ID", _cfg.get("space_id", ""))

DRY_RUN = "--dry-run" in sys.argv
VERBOSE = "--verbose" in sys.argv

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    h = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    if AGENT_ID:
        h["X-Agent-Id"] = AGENT_ID
    return h


def _log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def send_reply(content: str, parent_id: str | None = None) -> bool:
    """Send a message as this agent via REST API. Returns True on success."""
    if DRY_RUN:
        _log(f"  [DRY RUN] Would send: {content}")
        return True

    body: dict = {
        "content": content,
        "space_id": SPACE_ID,
        "channel": "main",
        "message_type": "text",
    }
    if parent_id:
        body["parent_id"] = parent_id

    try:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{BASE_URL}/api/v1/messages",
            data=data,
            headers=_headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            if resp.status == 200 and raw:
                msg_id = json.loads(raw).get("message", {}).get("id", "?")
                _log(f"  Replied -> {msg_id[:12]}")
                return True
            elif resp.status == 200:
                _log(f"  Sent (empty body — check for self-mention in content)")
                return False
            else:
                _log(f"  Send failed: {resp.status}")
    except urllib.request.HTTPError as e:
        _log(f"  HTTP error: {e.code} {e.read().decode()[:200]}")
    except Exception as e:
        _log(f"  Send error: {type(e).__name__}: {e}")
    return False


def _get_author(data: dict) -> str:
    """Extract author name from event data."""
    author = data.get("author", "")
    if isinstance(author, dict):
        return author.get("name", author.get("username", ""))
    return str(author)


# ---------------------------------------------------------------------------
# SSE event stream
# ---------------------------------------------------------------------------

def iter_sse_events():
    """Connect to SSE and yield (event_type, parsed_data) tuples."""
    url = f"{BASE_URL}/api/sse/messages"
    params = {"token": TOKEN}

    with httpx.Client(timeout=None) as client:
        with client.stream("GET", url, params=params, headers=_headers()) as resp:
            if resp.status_code != 200:
                _log(f"SSE connect failed: {resp.status_code}")
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
                        raw = "\n".join(data_lines)
                        try:
                            parsed = json.loads(raw) if raw.startswith("{") else raw
                        except json.JSONDecodeError:
                            parsed = raw
                        yield event_type, parsed
                    event_type = None
                    data_lines = []


# ---------------------------------------------------------------------------
# Echo loop
# ---------------------------------------------------------------------------

def run():
    _log(f"echo_agent | agent={AGENT_NAME} | space={SPACE_ID[:12]}...")
    _log(f"  api={BASE_URL} | mode={'DRY RUN' if DRY_RUN else 'LIVE'}")
    _log(f"  Listening for messages on SSE...")
    _log("")

    seen: set[str] = set()
    backoff = 1

    while True:
        try:
            for event_type, data in iter_sse_events():
                backoff = 1

                if VERBOSE:
                    preview = str(data)[:120] if not isinstance(data, str) else data[:120]
                    _log(f"  [{event_type}] {preview}")

                if event_type == "connected":
                    _log("Connected to SSE stream")
                    _log("Waiting for messages...")
                    continue

                if event_type != "message":
                    continue

                if not isinstance(data, dict):
                    continue

                msg_id = data.get("id", "")
                content = data.get("content", "").strip()
                author = _get_author(data)

                # Skip: no content, already seen, or from ourselves
                if not content or msg_id in seen:
                    continue
                if author.lower() == AGENT_NAME.lower():
                    continue
                # Skip aX concierge to avoid loops
                if author == "aX":
                    continue

                seen.add(msg_id)
                if len(seen) > 200:
                    seen.clear()

                _log(f"MSG from @{author}: {content[:100]}")

                # Echo it back as a threaded reply
                echo = f"[echo] {content}"
                send_reply(echo, parent_id=msg_id)

        except httpx.ConnectError:
            _log(f"Connection lost. Reconnecting in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except KeyboardInterrupt:
            _log("Shutting down.")
            break
        except Exception as e:
            _log(f"Error: {e}. Reconnecting in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not TOKEN:
        print("Error: No token. Set AX_TOKEN or configure .ax/config.toml")
        sys.exit(1)
    if not AGENT_NAME:
        print("Error: No agent_name. Set AX_AGENT_NAME or configure .ax/config.toml")
        sys.exit(1)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    run()
