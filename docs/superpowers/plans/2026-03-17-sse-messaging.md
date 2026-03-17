# SSE-Based CLI Messaging Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace polling-based reply waiting with SSE across the CLI, and add `ax listen --exec` to turn any CLI tool into an aX agent.

**Architecture:** A shared `SSEStream` class (in `ax_cli/sse.py`) handles SSE connection, parsing, reconnect, and dedup. `ax listen` uses it for long-running mention monitoring with optional `--exec`. `ax send` uses it for near-instant reply detection with polling fallback.

**Tech Stack:** Python 3.11+, httpx (SSE streaming), Typer (CLI), pytest (testing)

**Spec:** `docs/superpowers/specs/2026-03-17-sse-messaging-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `pyproject.toml` | Modify | Add pytest dev dependency |
| `tests/__init__.py` | Create | Test package marker |
| `tests/conftest.py` | Create | Shared fixtures: fake SSE streams, mock client |
| `ax_cli/sse.py` | Create | `SSEEvent` dataclass, `parse_sse_events()` generator, `SSEStream` class |
| `tests/test_sse.py` | Create | SSE parser, dedup, reconnect tests |
| `ax_cli/commands/listen.py` | Create | `ax listen` command with `--exec`, mention detection, exec runner |
| `tests/test_listen.py` | Create | Listen command: mention filtering, exec, loop prevention |
| `ax_cli/main.py` | Modify | Register `listen` sub-app and `monitor` alias |
| `ax_cli/commands/messages.py` | Modify | SSE-based `_wait_for_reply_sse()`, fallback to polling |
| `tests/test_messages_sse.py` | Create | `ax send` SSE reply detection, fallback |
| `ax_cli/client.py` | Modify | Update `connect_sse()` to canonical path |

---

## Task 1: Test infrastructure setup

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Add pytest dev dependency to pyproject.toml**

Add after the `[tool.setuptools.packages.find]` section:

```toml
[project.optional-dependencies]
dev = ["pytest>=7.0"]
```

- [ ] **Step 2: Create test package**

```bash
mkdir -p tests
touch tests/__init__.py
```

- [ ] **Step 3: Write conftest.py with SSE stream fixtures**

Create `tests/conftest.py`:

```python
"""Shared test fixtures for ax-cli tests."""
import json
import pytest


def make_sse_lines(events: list[tuple[str, dict | str]]) -> list[str]:
    """Build raw SSE text lines from a list of (event_type, data) tuples.

    Usage:
        lines = make_sse_lines([
            ("message", {"id": "msg-1", "content": "hello"}),
            ("heartbeat", ""),
        ])
    """
    lines = []
    for event_type, data in events:
        lines.append(f"event: {event_type}")
        if isinstance(data, dict):
            lines.append(f"data: {json.dumps(data)}")
        else:
            lines.append(f"data: {data}")
        lines.append("")  # blank line = end of event
    return lines


@pytest.fixture
def sse_lines():
    """Factory fixture for building SSE line sequences."""
    return make_sse_lines
```

- [ ] **Step 4: Install dev dependencies and verify pytest runs**

```bash
cd /home/ax-agent/shared/repos/ax-cli
pip install -e ".[dev]"
pytest --co -q
```

Expected: `no tests ran` (collection succeeds, no tests yet)

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/
git commit -m "chore: add pytest and test infrastructure"
```

---

## Task 2: SSE parser — `SSEEvent` and `parse_sse_events()`

**Files:**
- Create: `tests/test_sse.py` (parser tests only)
- Create: `ax_cli/sse.py`

- [ ] **Step 1: Write failing tests for SSE parser**

Create `tests/test_sse.py`:

```python
"""Tests for SSE parser, dedup, and stream management."""
import json
import pytest

from tests.conftest import make_sse_lines


class TestParseSSEEvents:
    """Test parse_sse_events() generator."""

    def test_single_message_event(self):
        from ax_cli.sse import parse_sse_events, SSEEvent

        lines = [
            "event: message",
            'data: {"id": "msg-1", "content": "hello"}',
            "",
        ]
        events = list(parse_sse_events(iter(lines)))
        assert len(events) == 1
        assert events[0].type == "message"
        assert events[0].data["id"] == "msg-1"
        assert events[0].data["content"] == "hello"

    def test_multiple_events(self):
        from ax_cli.sse import parse_sse_events

        lines = make_sse_lines([
            ("message", {"id": "msg-1", "content": "hello"}),
            ("heartbeat", {"ts": "2026-01-01"}),
            ("message", {"id": "msg-2", "content": "world"}),
        ])
        events = list(parse_sse_events(iter(lines)))
        assert len(events) == 3
        assert events[0].type == "message"
        assert events[2].data["id"] == "msg-2"

    def test_multiline_data(self):
        from ax_cli.sse import parse_sse_events

        lines = [
            "event: message",
            'data: {"id": "msg-1",',
            'data:  "content": "hello"}',
            "",
        ]
        events = list(parse_sse_events(iter(lines)))
        assert len(events) == 1
        assert events[0].data["id"] == "msg-1"
        assert events[0].data["content"] == "hello"

    def test_missing_event_type_defaults_to_message(self):
        from ax_cli.sse import parse_sse_events

        lines = [
            'data: {"id": "msg-1"}',
            "",
        ]
        events = list(parse_sse_events(iter(lines)))
        assert len(events) == 1
        assert events[0].type == "message"

    def test_malformed_json_preserved_as_raw(self):
        from ax_cli.sse import parse_sse_events

        lines = [
            "event: heartbeat",
            "data: not-json",
            "",
        ]
        events = list(parse_sse_events(iter(lines)))
        assert len(events) == 1
        assert events[0].data == {}
        assert events[0].raw == "not-json"

    def test_comment_lines_ignored(self):
        from ax_cli.sse import parse_sse_events

        lines = [
            ":keepalive",
            "event: message",
            'data: {"id": "msg-1"}',
            "",
        ]
        events = list(parse_sse_events(iter(lines)))
        assert len(events) == 1

    def test_empty_input(self):
        from ax_cli.sse import parse_sse_events

        events = list(parse_sse_events(iter([])))
        assert events == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_sse.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'ax_cli.sse'`

- [ ] **Step 3: Implement SSEEvent and parse_sse_events**

Create `ax_cli/sse.py`:

```python
"""SSE (Server-Sent Events) parser and stream management for aX CLI.

Provides:
- SSEEvent: typed dataclass for parsed events
- parse_sse_events(): generator that parses raw SSE text lines into SSEEvent objects
- DedupTracker: bounded OrderedDict dedup (added in Task 3)
- SSEStream: managed SSE connection with reconnect and dedup (added in Task 4)
"""
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class SSEEvent:
    """A parsed SSE event."""
    type: str = "message"
    data: dict = field(default_factory=dict)
    raw: str = ""


def parse_sse_events(lines: Iterator[str]) -> Iterator[SSEEvent]:
    """Parse raw SSE text lines into SSEEvent objects.

    Handles multi-line data fields, missing event types, and malformed JSON.
    Comment lines (starting with ':') are ignored.
    """
    event_type: str | None = None
    data_parts: list[str] = []

    for line in lines:
        if line.startswith(":"):
            # SSE comment (keepalive, etc.)
            continue
        elif line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_parts.append(line[5:].strip())
        elif line == "" or line == "\n":
            # Blank line = end of event
            if data_parts:
                raw = "\n".join(data_parts) if len(data_parts) > 1 else data_parts[0]
                joined = "".join(data_parts) if len(data_parts) > 1 else data_parts[0]
                try:
                    data = json.loads(joined)
                except (json.JSONDecodeError, ValueError):
                    data = {}

                yield SSEEvent(
                    type=event_type or "message",
                    data=data if isinstance(data, dict) else {},
                    raw=raw,
                )
            event_type = None
            data_parts = []
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_sse.py -v
```

Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ax_cli/sse.py tests/test_sse.py
git commit -m "feat: SSE parser with SSEEvent dataclass and parse_sse_events"
```

---

## Task 3: Dedup tracker

**Files:**
- Modify: `tests/test_sse.py` (add dedup tests)
- Modify: `ax_cli/sse.py` (add DedupTracker)

- [ ] **Step 1: Write failing tests for DedupTracker**

Add to `tests/test_sse.py`:

```python
class TestDedupTracker:
    """Test OrderedDict-based dedup with bounded eviction."""

    def test_new_id_returns_false(self):
        from ax_cli.sse import DedupTracker
        tracker = DedupTracker(max_size=5)
        assert tracker.is_seen("msg-1") is False

    def test_seen_id_returns_true(self):
        from ax_cli.sse import DedupTracker
        tracker = DedupTracker(max_size=5)
        tracker.is_seen("msg-1")  # first time, marks as seen
        assert tracker.is_seen("msg-1") is True

    def test_eviction_at_max_size(self):
        from ax_cli.sse import DedupTracker
        tracker = DedupTracker(max_size=4)
        for i in range(4):
            tracker.is_seen(f"msg-{i}")
        # All 4 are seen
        assert tracker.is_seen("msg-0") is True
        # Add one more — triggers eviction of oldest half (2 entries)
        tracker.is_seen("msg-4")
        # msg-0 and msg-1 should be evicted
        assert tracker.is_seen("msg-0") is False
        assert tracker.is_seen("msg-1") is False
        # msg-2, msg-3, msg-4 remain
        assert tracker.is_seen("msg-2") is True
        assert tracker.is_seen("msg-3") is True
        assert tracker.is_seen("msg-4") is True

    def test_empty_string_id_ignored(self):
        from ax_cli.sse import DedupTracker
        tracker = DedupTracker(max_size=5)
        assert tracker.is_seen("") is False
        assert tracker.is_seen("") is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_sse.py::TestDedupTracker -v
```

Expected: FAIL with `ImportError: cannot import name 'DedupTracker'`

- [ ] **Step 3: Implement DedupTracker**

Add to `ax_cli/sse.py` after the `SSEEvent` dataclass:

```python
class DedupTracker:
    """Bounded dedup tracker using OrderedDict for insertion-order eviction."""

    def __init__(self, max_size: int = 500):
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._max_size = max_size

    def is_seen(self, msg_id: str) -> bool:
        """Check if msg_id was already seen. If new, mark it as seen.

        Returns True if this is a duplicate, False if it's new.
        Empty IDs are never tracked.
        """
        if not msg_id:
            return False
        if msg_id in self._seen:
            return True
        self._seen[msg_id] = None
        if len(self._seen) > self._max_size:
            evict_count = self._max_size // 2
            for _ in range(evict_count):
                self._seen.popitem(last=False)
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_sse.py -v
```

Expected: All 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ax_cli/sse.py tests/test_sse.py
git commit -m "feat: add DedupTracker with bounded OrderedDict eviction"
```

---

## Task 4: SSEStream class with reconnect

**Files:**
- Modify: `tests/test_sse.py` (add SSEStream tests)
- Modify: `ax_cli/sse.py` (add SSEStream)

- [ ] **Step 1: Write failing tests for SSEStream**

Add to `tests/test_sse.py`:

```python
from unittest.mock import patch, MagicMock


class TestSSEStream:
    """Test SSEStream connection and event yielding."""

    def _fake_stream_response(self, lines: list[str]):
        """Create a mock httpx stream response that yields lines."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_lines.return_value = iter(lines)
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_yields_parsed_events(self):
        from ax_cli.sse import SSEStream

        lines = make_sse_lines([
            ("connected", {"status": "connected"}),
            ("message", {"id": "msg-1", "content": "hello"}),
        ])

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.stream.return_value = self._fake_stream_response(lines)

            stream = SSEStream("http://localhost:8002", "test-token")
            events = list(stream.events_once())

        assert len(events) == 2
        assert events[0].type == "connected"
        assert events[1].data["id"] == "msg-1"

    def test_dedup_skips_duplicate_message_ids(self):
        from ax_cli.sse import SSEStream

        lines = make_sse_lines([
            ("message", {"id": "msg-1", "content": "first"}),
            ("message", {"id": "msg-1", "content": "dupe"}),
            ("message", {"id": "msg-2", "content": "second"}),
        ])

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.stream.return_value = self._fake_stream_response(lines)

            stream = SSEStream("http://localhost:8002", "test-token")
            events = [e for e in stream.events_once() if e.type == "message"]

        assert len(events) == 2
        assert events[0].data["content"] == "first"
        assert events[1].data["content"] == "second"

    def test_constructs_correct_url_and_params(self):
        from ax_cli.sse import SSEStream

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.stream.return_value = self._fake_stream_response([])

            stream = SSEStream("http://localhost:8002", "tok-123", headers={"X-Agent-Id": "agent-1"})
            list(stream.events_once())

        mock_client.stream.assert_called_once_with(
            "GET",
            "http://localhost:8002/api/sse/messages",
            params={"token": "tok-123"},
            headers={"X-Agent-Id": "agent-1"},
            timeout=None,
        )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_sse.py::TestSSEStream -v
```

Expected: FAIL with `ImportError: cannot import name 'SSEStream'`

- [ ] **Step 3: Implement SSEStream**

Add to `ax_cli/sse.py`:

```python
import httpx


class SSEStream:
    """Managed SSE connection with dedup.

    Usage:
        stream = SSEStream(base_url, token)
        for event in stream.events_once():  # single connection
            handle(event)

        for event in stream.events():  # reconnect loop
            handle(event)
    """

    def __init__(self, base_url: str, token: str, *, headers: dict | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.headers = headers or {}
        self._dedup = DedupTracker(max_size=500)
        self._closed = False

    def events_once(self) -> Iterator[SSEEvent]:
        """Connect once and yield events. No reconnect."""
        url = f"{self.base_url}/api/sse/messages"
        with httpx.Client(timeout=None) as client:
            with client.stream(
                "GET", url,
                params={"token": self.token},
                headers=self.headers,
                timeout=None,
            ) as resp:
                if resp.status_code != 200:
                    return
                for event in parse_sse_events(resp.iter_lines()):
                    if self._closed:
                        return
                    msg_id = event.data.get("id", "")
                    if event.type in ("message", "mention") and msg_id:
                        if self._dedup.is_seen(msg_id):
                            continue
                    yield event

    def events(self) -> Iterator[SSEEvent]:
        """Connect with reconnect loop. Yields events across reconnections."""
        import time
        backoff = 1
        while not self._closed:
            try:
                for event in self.events_once():
                    backoff = 1
                    yield event
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
                if self._closed:
                    return
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def close(self):
        """Signal the stream to stop."""
        self._closed = True
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_sse.py -v
```

Expected: All 14 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ax_cli/sse.py tests/test_sse.py
git commit -m "feat: SSEStream class with dedup and reconnect"
```

---

## Task 5: Mention detection helpers

**Files:**
- Create: `tests/test_listen.py`
- Create: `ax_cli/commands/listen.py` (mention helpers only)

- [ ] **Step 1: Write failing tests for mention detection**

Create `tests/test_listen.py`:

```python
"""Tests for ax listen command — mention detection, exec, loop prevention."""
import pytest


class TestMentionDetection:
    """Test is_mentioned() and should_respond()."""

    def test_mentioned_in_structured_mentions(self):
        from ax_cli.commands.listen import is_mentioned
        event = {"mentions": ["wire_tap", "orion"], "content": "hey"}
        assert is_mentioned(event, "wire_tap") is True

    def test_mentioned_in_content_fallback(self):
        from ax_cli.commands.listen import is_mentioned
        event = {"mentions": [], "content": "hey @wire_tap check this"}
        assert is_mentioned(event, "wire_tap") is True

    def test_not_mentioned(self):
        from ax_cli.commands.listen import is_mentioned
        event = {"mentions": ["orion"], "content": "hey @orion"}
        assert is_mentioned(event, "wire_tap") is False

    def test_mention_case_insensitive(self):
        from ax_cli.commands.listen import is_mentioned
        event = {"mentions": ["Wire_Tap"], "content": ""}
        assert is_mentioned(event, "wire_tap") is True

    def test_should_respond_skips_self(self):
        from ax_cli.commands.listen import should_respond
        event = {
            "mentions": ["wire_tap"],
            "content": "@wire_tap hello",
            "username": "wire_tap",
        }
        assert should_respond(event, "wire_tap") is False

    def test_should_respond_skips_ax(self):
        from ax_cli.commands.listen import should_respond
        event = {
            "mentions": ["wire_tap"],
            "content": "@wire_tap hello",
            "username": "aX",
        }
        assert should_respond(event, "wire_tap") is False

    def test_should_respond_true_for_valid_mention(self):
        from ax_cli.commands.listen import should_respond
        event = {
            "mentions": ["wire_tap"],
            "content": "@wire_tap do something",
            "username": "orion",
        }
        assert should_respond(event, "wire_tap") is True


class TestStripMention:
    """Test stripping @mention from message content."""

    def test_strips_mention_prefix(self):
        from ax_cli.commands.listen import strip_mention
        assert strip_mention("@wire_tap do the thing", "wire_tap") == "do the thing"

    def test_strips_mention_anywhere(self):
        from ax_cli.commands.listen import strip_mention
        assert strip_mention("hey @wire_tap do it", "wire_tap") == "hey do it"

    def test_no_mention_unchanged(self):
        from ax_cli.commands.listen import strip_mention
        assert strip_mention("hello world", "wire_tap") == "hello world"

    def test_case_insensitive(self):
        from ax_cli.commands.listen import strip_mention
        assert strip_mention("@Wire_Tap check this", "wire_tap") == "check this"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_listen.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'ax_cli.commands.listen'`

- [ ] **Step 3: Implement mention helpers**

Create `ax_cli/commands/listen.py`:

```python
"""ax listen — SSE-based mention listener with optional --exec handler.

Turns any CLI command into an aX agent:
    ax listen --exec 'claude -p "$AX_MESSAGE"'
"""
import re


def is_mentioned(event_data: dict, agent_name: str) -> bool:
    """Check if agent_name is mentioned in the event."""
    mentions = event_data.get("mentions", [])
    if agent_name.lower() in [str(m).lower() for m in mentions]:
        return True
    content = event_data.get("content", "")
    if f"@{agent_name.lower()}" in content.lower():
        return True
    return False


def _get_sender(event_data: dict) -> str:
    """Extract sender name — handles both message format (username) and mention format (author dict/string)."""
    # message events use "username"
    sender = event_data.get("username", "")
    if sender:
        return sender
    # mention events use "author" (can be dict with "name" key or plain string)
    author = event_data.get("author", "")
    if isinstance(author, dict):
        return author.get("name", "")
    return str(author)


def should_respond(event_data: dict, agent_name: str) -> bool:
    """Check if we should respond (mentioned + not self + not aX)."""
    sender = _get_sender(event_data)
    if sender.lower() == agent_name.lower():
        return False
    if sender == "aX":
        return False
    return is_mentioned(event_data, agent_name)


def strip_mention(content: str, agent_name: str) -> str:
    """Remove @agent_name from content, case-insensitive. Cleans up extra whitespace."""
    result = re.sub(rf"@{re.escape(agent_name)}\b\s*", "", content, flags=re.IGNORECASE)
    return " ".join(result.split())  # normalize whitespace
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_listen.py -v
```

Expected: All 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ax_cli/commands/listen.py tests/test_listen.py
git commit -m "feat: mention detection helpers for ax listen"
```

---

## Task 6: Exec runner

**Files:**
- Modify: `tests/test_listen.py` (add exec tests)
- Modify: `ax_cli/commands/listen.py` (add run_exec)

Note: The exec runner uses `subprocess.run(shell=True)`. Message content is passed via environment variables and stdin only — never interpolated into the shell command string. This prevents shell injection from malicious message content. See spec security notes.

- [ ] **Step 1: Write failing tests for exec runner**

Add to `tests/test_listen.py`:

```python
class TestRunExec:
    """Test the exec runner — subprocess with env vars and stdin."""

    def test_captures_stdout(self):
        from ax_cli.commands.listen import run_exec
        result = run_exec("echo hello", message="test", event_data={})
        assert result == "hello"

    def test_returns_none_on_nonzero_exit(self):
        from ax_cli.commands.listen import run_exec
        result = run_exec("exit 1", message="test", event_data={})
        assert result is None

    def test_returns_none_on_timeout(self):
        from ax_cli.commands.listen import run_exec
        result = run_exec("sleep 10", message="test", event_data={}, timeout=1)
        assert result is None

    def test_sets_env_variables(self):
        from ax_cli.commands.listen import run_exec
        result = run_exec(
            'echo "$AX_AUTHOR"',
            message="hello",
            event_data={
                "content": "@wire_tap hello",
                "username": "orion",
                "agent_type": "agent",
                "id": "msg-123",
                "parent_id": "parent-456",
                "space_id": "space-789",
            },
        )
        assert result == "orion"

    def test_pipes_message_to_stdin(self):
        from ax_cli.commands.listen import run_exec
        result = run_exec("cat", message="piped content", event_data={})
        assert result == "piped content"

    def test_empty_stdout_returns_none(self):
        from ax_cli.commands.listen import run_exec
        result = run_exec("echo -n ''", message="test", event_data={})
        assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_listen.py::TestRunExec -v
```

Expected: FAIL with `ImportError: cannot import name 'run_exec'`

- [ ] **Step 3: Implement run_exec**

Add to `ax_cli/commands/listen.py`:

```python
import logging
import os
import subprocess

logger = logging.getLogger("ax_listen")


def run_exec(
    command: str,
    *,
    message: str,
    event_data: dict,
    timeout: int = 300,
) -> str | None:
    """Run an --exec command with message data via env vars and stdin.

    Security: message content is passed ONLY via environment variables and stdin.
    The command string is never modified with user content — preventing shell injection.

    Returns stdout on success (exit 0 + non-empty output), None otherwise.
    """
    env = os.environ.copy()
    env["AX_MESSAGE"] = message
    env["AX_RAW_MESSAGE"] = event_data.get("content", "")
    env["AX_AUTHOR"] = event_data.get("username", "")
    env["AX_AUTHOR_TYPE"] = event_data.get("agent_type", "unknown")
    env["AX_MSG_ID"] = event_data.get("id", "")
    env["AX_PARENT_ID"] = event_data.get("parent_id", "") or ""
    env["AX_SPACE_ID"] = event_data.get("space_id", "")

    try:
        result = subprocess.run(
            command,
            shell=True,
            input=message,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"Exec timed out after {timeout}s: {command[:60]}")
        return None

    if result.returncode != 0:
        logger.warning(f"Exec failed (exit {result.returncode}): {result.stderr[:200]}")
        return None

    stdout = result.stdout.strip()
    return stdout if stdout else None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_listen.py -v
```

Expected: All 17 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ax_cli/commands/listen.py tests/test_listen.py
git commit -m "feat: exec runner with env vars and stdin piping"
```

---

## Task 7: `ax listen` command (Typer integration)

**Files:**
- Modify: `ax_cli/commands/listen.py` (add Typer command)
- Modify: `ax_cli/main.py` (register listen + monitor alias)
- Modify: `tests/test_listen.py` (add integration test)

- [ ] **Step 1: Write failing integration test**

Add to `tests/test_listen.py`:

```python
from typer.testing import CliRunner
from unittest.mock import patch, MagicMock
from tests.conftest import make_sse_lines


class TestListenCommand:
    """Integration tests for the ax listen CLI command."""

    def test_listen_dry_run_prints_mention(self):
        from ax_cli.main import app
        from ax_cli.sse import SSEEvent

        runner = CliRunner()

        events = [
            SSEEvent(type="connected", data={"status": "connected"}, raw=""),
            SSEEvent(type="message", data={
                "id": "msg-1",
                "content": "@wire_tap hello",
                "username": "orion",
                "mentions": ["wire_tap"],
            }, raw=""),
        ]

        mock_stream = MagicMock()
        mock_stream.events.return_value = iter(events)

        with patch("ax_cli.commands.listen.get_client") as mock_gc, \
             patch("ax_cli.commands.listen.SSEStream", return_value=mock_stream), \
             patch("ax_cli.commands.listen.resolve_agent_name", return_value="wire_tap"):
            mock_client = MagicMock()
            mock_client.base_url = "http://localhost:8002"
            mock_client.token = "test-token"
            mock_client._headers = {}
            mock_gc.return_value = mock_client

            result = runner.invoke(app, ["listen", "--dry-run"])

        assert result.exit_code == 0
        assert "MENTION" in result.output or "orion" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_listen.py::TestListenCommand -v
```

Expected: FAIL — `listen` command not registered

- [ ] **Step 3: Add Typer command to listen.py**

Add to `ax_cli/commands/listen.py` (at top, after existing imports):

```python
import json
import signal
import sys
from typing import Optional

import typer

from ..config import get_client, resolve_agent_name
from ..output import console
from ..sse import SSEStream

app = typer.Typer(name="listen", help="Listen for messages via SSE", no_args_is_help=False)


@app.callback(invoke_without_command=True)
def listen(
    exec_cmd: Optional[str] = typer.Option(None, "--exec", help="Command to run on @mention (message via stdin + AX_* env vars)"),
    filter_type: str = typer.Option("mentions", "--filter", help="Filter: 'mentions' (default), 'all', or event type"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Log events without executing or replying"),
    timeout: int = typer.Option(300, "--timeout", help="Exec command timeout in seconds"),
    as_json: bool = typer.Option(False, "--json", help="Output events as JSON"),
):
    """Listen for messages via SSE. With --exec, run a command on each @mention."""
    client = get_client()
    agent_name = resolve_agent_name(client=client) or ""
    if not agent_name:
        console.print("[red]Error: No agent_name configured. Set in .ax/config.toml or AX_AGENT_NAME.[/red]")
        raise typer.Exit(1)

    stream = SSEStream(
        client.base_url,
        client.token,
        headers={k: v for k, v in client._headers.items() if k.startswith("X-")},
    )

    def _shutdown(signum, frame):
        stream.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    console.print(f"[bold]Listening as @{agent_name}[/bold]")
    if exec_cmd:
        console.print(f"  exec: {exec_cmd}")
    if dry_run:
        console.print("  [dim]dry-run mode — no replies will be sent[/dim]")
    console.print()

    # Note: SSEStream already does dedup internally for message/mention events.
    # No additional DedupTracker needed here.

    for event in stream.events():
        if event.type in ("connected", "bootstrap", "heartbeat"):
            if event.type == "connected":
                console.print("[green]Connected to SSE[/green]")
            continue

        if filter_type == "mentions":
            if event.type not in ("message", "mention"):
                continue
            if not should_respond(event.data, agent_name):
                continue

        if as_json:
            print(json.dumps({"event": event.type, "data": event.data}, default=str))
            sys.stdout.flush()
            continue

        sender = event.data.get("username", "?")
        content = event.data.get("content", "")

        if filter_type == "mentions":
            console.print(f"\n[bold yellow]MENTION[/bold yellow] from @{sender}")
            console.print(f"  {content[:200]}")
            console.print(f"  id={msg_id}")

            if exec_cmd and not dry_run:
                message = strip_mention(content, agent_name)
                output = run_exec(exec_cmd, message=message, event_data=event.data, timeout=timeout)
                if output:
                    try:
                        client.send_message(
                            event.data.get("space_id", ""),
                            output,
                            parent_id=msg_id,
                        )
                        console.print("  [green]Reply sent[/green]")
                    except Exception as e:
                        console.print(f"  [red]Reply failed: {e}[/red]")
                else:
                    console.print("  [dim]No output from exec (no reply sent)[/dim]")
            elif exec_cmd and dry_run:
                console.print(f"  [dim]dry-run: would exec: {exec_cmd}[/dim]")
        else:
            console.print(f"[cyan][{event.type}][/cyan] @{sender}: {content[:120]}")
```

- [ ] **Step 4: Register listen command and monitor alias in main.py**

In `ax_cli/main.py`, add `listen` to the import:

```python
from .commands import auth, keys, agents, messages, tasks, events, listen
```

Add after existing `app.add_typer` calls:

```python
app.add_typer(listen.app, name="listen")
```

Add monitor alias after the `send_shortcut` function:

```python
@app.command("monitor")
def monitor_shortcut(
    exec_cmd: Optional[str] = typer.Option(None, "--exec", help="Command to run on @mention"),
    filter_type: str = typer.Option("mentions", "--filter", help="Event filter"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Watch only"),
    timeout: int = typer.Option(300, "--timeout", help="Exec timeout seconds"),
    as_json: bool = typer.Option(False, "--json", help="JSON output"),
):
    """Alias for 'ax listen'. Monitor SSE events and optionally exec on @mention."""
    listen.listen(
        exec_cmd=exec_cmd,
        filter_type=filter_type,
        dry_run=dry_run,
        timeout=timeout,
        as_json=as_json,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_listen.py -v
```

Expected: All 18 tests PASS

- [ ] **Step 6: Verify CLI registration works**

```bash
ax listen --help
ax monitor --help
```

Expected: Both show help text with `--exec`, `--filter`, `--dry-run`, `--timeout`, `--json` options.

- [ ] **Step 7: Commit**

```bash
git add ax_cli/commands/listen.py ax_cli/main.py tests/test_listen.py
git commit -m "feat: ax listen command with --exec and monitor alias"
```

---

## Task 8: `ax send` SSE-based reply waiting

**Files:**
- Create: `tests/test_messages_sse.py`
- Modify: `ax_cli/commands/messages.py`

- [ ] **Step 1: Write failing tests for SSE reply waiting**

Create `tests/test_messages_sse.py`:

```python
"""Tests for SSE-based reply waiting in ax send."""
import pytest
from unittest.mock import patch, MagicMock
from ax_cli.sse import SSEEvent


class TestWaitForReplySSE:
    """Test _wait_for_reply_sse() function."""

    def test_detects_reply_by_parent_id(self):
        from ax_cli.commands.messages import _wait_for_reply_sse

        events = [
            SSEEvent("connected", {"status": "connected"}, ""),
            SSEEvent("message", {
                "id": "reply-1",
                "parent_id": "sent-msg",
                "content": "aX response",
                "username": "aX",
            }, ""),
        ]

        mock_stream = MagicMock()
        mock_stream.events_once.return_value = iter(events)

        with patch("ax_cli.commands.messages.SSEStream", return_value=mock_stream):
            reply = _wait_for_reply_sse(
                base_url="http://localhost:8002",
                token="test",
                headers={},
                message_id="sent-msg",
                timeout=5,
            )

        assert reply is not None
        assert reply["id"] == "reply-1"
        assert reply["content"] == "aX response"

    def test_detects_reply_by_conversation_id(self):
        from ax_cli.commands.messages import _wait_for_reply_sse

        events = [
            SSEEvent("message", {
                "id": "reply-1",
                "conversation_id": "sent-msg",
                "content": "response",
            }, ""),
        ]

        mock_stream = MagicMock()
        mock_stream.events_once.return_value = iter(events)

        with patch("ax_cli.commands.messages.SSEStream", return_value=mock_stream):
            reply = _wait_for_reply_sse(
                base_url="http://localhost:8002",
                token="test",
                headers={},
                message_id="sent-msg",
                timeout=5,
            )

        assert reply is not None

    def test_skips_ax_relay(self):
        from ax_cli.commands.messages import _wait_for_reply_sse

        events = [
            SSEEvent("message", {
                "id": "relay-1",
                "parent_id": "sent-msg",
                "content": "routing",
                "metadata": {"routing": {"mode": "ax_relay", "target_agent_name": "nova_sage"}},
            }, ""),
            SSEEvent("message", {
                "id": "reply-1",
                "parent_id": "sent-msg",
                "content": "actual reply",
            }, ""),
        ]

        mock_stream = MagicMock()
        mock_stream.events_once.return_value = iter(events)

        with patch("ax_cli.commands.messages.SSEStream", return_value=mock_stream):
            reply = _wait_for_reply_sse(
                base_url="http://localhost:8002",
                token="test",
                headers={},
                message_id="sent-msg",
                timeout=5,
            )

        assert reply is not None
        assert reply["id"] == "reply-1"

    def test_returns_none_on_empty_stream(self):
        from ax_cli.commands.messages import _wait_for_reply_sse

        mock_stream = MagicMock()
        mock_stream.events_once.return_value = iter([])

        with patch("ax_cli.commands.messages.SSEStream", return_value=mock_stream):
            reply = _wait_for_reply_sse(
                base_url="http://localhost:8002",
                token="test",
                headers={},
                message_id="sent-msg",
                timeout=1,
            )

        assert reply is None

    def test_ignores_unrelated_messages(self):
        from ax_cli.commands.messages import _wait_for_reply_sse

        events = [
            SSEEvent("message", {
                "id": "other-1",
                "parent_id": "some-other-msg",
                "content": "unrelated",
            }, ""),
            SSEEvent("message", {
                "id": "reply-1",
                "parent_id": "sent-msg",
                "content": "the reply",
            }, ""),
        ]

        mock_stream = MagicMock()
        mock_stream.events_once.return_value = iter(events)

        with patch("ax_cli.commands.messages.SSEStream", return_value=mock_stream):
            reply = _wait_for_reply_sse(
                base_url="http://localhost:8002",
                token="test",
                headers={},
                message_id="sent-msg",
                timeout=5,
            )

        assert reply["id"] == "reply-1"

    def test_returns_none_on_connect_error(self):
        from ax_cli.commands.messages import _wait_for_reply_sse
        import httpx

        mock_stream = MagicMock()
        mock_stream.events_once.side_effect = httpx.ConnectError("refused")

        with patch("ax_cli.commands.messages.SSEStream", return_value=mock_stream):
            reply = _wait_for_reply_sse(
                base_url="http://localhost:8002",
                token="test",
                headers={},
                message_id="sent-msg",
                timeout=1,
            )

        assert reply is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_messages_sse.py -v
```

Expected: FAIL with `ImportError: cannot import name '_wait_for_reply_sse'`

- [ ] **Step 3: Implement _wait_for_reply_sse**

Add to `ax_cli/commands/messages.py` after existing imports:

```python
from ..sse import SSEStream
```

Add this function after `_wait_for_reply_polling` and before `_wait_for_reply`:

```python
def _wait_for_reply_sse(
    *,
    base_url: str,
    token: str,
    headers: dict,
    message_id: str,
    timeout: int = 60,
) -> dict | None:
    """Wait for a reply via SSE stream. Returns the reply dict or None."""
    import time as _time
    deadline = _time.time() + timeout
    seen_ids: set[str] = {message_id}

    try:
        stream = SSEStream(base_url, token, headers=headers)
        for event in stream.events_once():
            if _time.time() >= deadline:
                break

            if event.type not in ("message", "mention"):
                if event.type == "agent_processing":
                    console.print(" " * 60, end="\r")
                    console.print("  [dim]aX is processing...[/dim]", end="\r")
                continue

            msg = event.data
            msg_id = msg.get("id", "")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)

            matches = (
                msg.get("parent_id") == message_id
                or msg.get("conversation_id") == message_id
            )
            if not matches:
                continue

            metadata = msg.get("metadata", {}) or {}
            routing = metadata.get("routing", {})
            if routing.get("mode") == "ax_relay":
                target = routing.get("target_agent_name", "specialist")
                console.print(" " * 60, end="\r")
                console.print(f"  [cyan]aX is routing to @{target}...[/cyan]")
                continue

            console.print(" " * 60, end="\r")
            stream.close()
            return msg

        stream.close()
    except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
        pass

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_messages_sse.py -v
```

Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ax_cli/commands/messages.py tests/test_messages_sse.py
git commit -m "feat: SSE-based reply waiting for ax send"
```

---

## Task 9: Wire SSE into `ax send` with polling fallback

**Files:**
- Modify: `ax_cli/commands/messages.py` (update `_wait_for_reply`)

- [ ] **Step 1: Replace _wait_for_reply to use SSE first, then poll**

Replace the existing `_wait_for_reply` function in `ax_cli/commands/messages.py` (lines 83-94):

```python
def _wait_for_reply(client, message_id: str, timeout: int = 60) -> dict | None:
    """Wait for a reply — SSE first, polling fallback."""
    reply = _wait_for_reply_sse(
        base_url=client.base_url,
        token=client.token,
        headers={k: v for k, v in client._headers.items() if k.startswith("X-")},
        message_id=message_id,
        timeout=timeout,
    )
    if reply:
        return reply

    # SSE didn't find a reply — fall back to polling for remaining time
    deadline = time.time() + max(timeout // 4, 10)
    seen_ids: set[str] = {message_id}
    console.print("  [dim]checking via polling...[/dim]", end="\r")

    return _wait_for_reply_polling(
        client,
        message_id,
        deadline=deadline,
        seen_ids=seen_ids,
        poll_interval=1.0,
    )
```

- [ ] **Step 2: Run all tests**

```bash
pytest tests/ -v
```

Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add ax_cli/commands/messages.py
git commit -m "feat: wire SSE into ax send with polling fallback"
```

---

## Task 10: Update client.py SSE endpoint path

**Files:**
- Modify: `ax_cli/client.py`
- Modify: `ax_cli/commands/events.py` (verify path)

- [ ] **Step 1: Fix connect_sse path in client.py**

In `ax_cli/client.py`, change line 282 from `/api/v1/sse/messages` to `/api/sse/messages`:

```python
def connect_sse(self) -> httpx.Response:
    """GET /api/sse/messages — returns streaming response."""
    return self._http.stream(
        "GET", "/api/sse/messages",
        params={"token": self.token},
        timeout=None,
    )
```

- [ ] **Step 2: Verify events.py uses correct path**

Check `ax_cli/commands/events.py` line 26. It should read:

```python
url = f"{client.base_url}/api/sse/messages"
```

Update if it says `/api/v1/sse/messages`.

- [ ] **Step 3: Run all tests**

```bash
pytest tests/ -v
```

Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add ax_cli/client.py ax_cli/commands/events.py
git commit -m "fix: standardize SSE endpoint to /api/sse/messages"
```

---

## Task 11: End-to-end verification

**Files:** None new — verification only

- [ ] **Step 1: Run full test suite**

```bash
pytest tests/ -v --tb=short
```

Expected: All tests PASS (should be ~30+ tests)

- [ ] **Step 2: Test ax listen --dry-run**

```bash
timeout 15 ax listen --dry-run 2>&1 || true
```

Expected: Shows "Listening as @wire_tap", "Connected to SSE", prints any incoming events.

- [ ] **Step 3: Test ax listen --exec with echo**

```bash
# Start listener in background:
ax listen --exec 'echo "ack: $AX_AUTHOR"' &
LISTEN_PID=$!

# Send a test message from another process:
ax send "@wire_tap ping" --skip-ax

# Wait a moment, then check messages for the reply
sleep 5
ax messages list --limit 3

# Clean up
kill $LISTEN_PID 2>/dev/null
```

Expected: A reply "ack: wire_tap" (or similar) appears in the message list.

- [ ] **Step 4: Test ax send SSE reply detection**

```bash
ax send "wire_tap testing SSE reply path" --timeout 30
```

Expected: Reply detected near-instantly (no visible 1s countdown steps).

- [ ] **Step 5: Test ax monitor alias**

```bash
ax monitor --help
```

Expected: Same options as `ax listen`.

- [ ] **Step 6: Commit any fixes**

```bash
# Only if fixes were needed during verification
git add -A
git commit -m "fix: end-to-end verification fixes"
```
