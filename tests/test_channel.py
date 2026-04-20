"""Tests for the Claude Code channel bridge identity boundary."""

import asyncio
import json
import os

from ax_cli.commands import channel as channel_mod
from ax_cli.commands.channel import ChannelBridge, MentionEvent, _load_channel_env
from ax_cli.commands.listen import _is_self_authored, _remember_reply_anchor, _should_respond


class FakeClient:
    def __init__(self, token: str = "axp_a_AgentKey.Secret", *, agent_id: str = "agent-123"):
        self.token = token
        self.agent_id = agent_id
        self._use_exchange = token.startswith("axp_")
        self.sent = []
        self.processing_statuses = []

    def send_message(self, space_id, content, *, parent_id=None, **kwargs):
        self.sent.append({"space_id": space_id, "content": content, "parent_id": parent_id, **kwargs})
        return {"message": {"id": "msg-123"}}

    def set_agent_processing_status(self, message_id, status, *, agent_name=None, space_id=None):
        self.processing_statuses.append(
            {
                "message_id": message_id,
                "status": status,
                "agent_name": agent_name,
                "space_id": space_id,
            }
        )
        return {"ok": True, "status": status}


class CaptureBridge(ChannelBridge):
    def __init__(
        self,
        client,
        *,
        agent_id="agent-123",
        processing_status=True,
        pre_exit_drain=True,
        backlog_limit=20,
        backlog_in_content=True,
    ):
        super().__init__(
            client=client,
            agent_name="peer-agent",
            agent_id=agent_id,
            space_id="space-123",
            queue_size=10,
            debug=False,
            processing_status=processing_status,
            pre_exit_drain=pre_exit_drain,
            backlog_limit=backlog_limit,
            backlog_in_content=backlog_in_content,
        )
        self.writes = []

    async def write_message(self, payload):
        self.writes.append(payload)


class FakeSseResponse:
    status_code = 200

    def __init__(self, payload, *, event_type: str = "message"):
        self.payload = payload
        self.event_type = event_type

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        yield f"event: {self.event_type}"
        yield f"data: {json.dumps(self.payload)}"
        yield ""


class FakeMultiEventSseResponse:
    """SSE response that yields a scripted sequence of (event_type, payload) events."""

    status_code = 200

    def __init__(self, events: list[tuple[str, dict]]):
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        for event_type, payload in self.events:
            yield f"event: {event_type}"
            yield f"data: {json.dumps(payload)}"
            yield ""


def test_channel_rejects_user_pat_for_agent_reply():
    client = FakeClient("axp_u_UserKey.Secret")
    bridge = CaptureBridge(client)
    bridge._last_message_id = "incoming-123"

    asyncio.run(
        bridge.handle_tool_call(
            1,
            {"name": "reply", "arguments": {"text": "hello"}},
        )
    )

    assert client.sent == []
    result = bridge.writes[0]["result"]
    assert result["isError"] is True
    assert "agent-bound PAT" in result["content"][0]["text"]


def test_channel_sends_with_agent_bound_pat():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)
    bridge._last_message_id = "incoming-123"

    asyncio.run(
        bridge.handle_tool_call(
            1,
            {"name": "reply", "arguments": {"text": "hello"}},
        )
    )

    assert client.sent == [{"space_id": "space-123", "content": "hello", "parent_id": "incoming-123"}]
    assert client.processing_statuses == [
        {
            "message_id": "incoming-123",
            "status": "completed",
            "agent_name": "peer-agent",
            "space_id": "space-123",
        }
    ]
    result = bridge.writes[0]["result"]
    assert result["content"][0]["text"] == "sent reply to incoming-123 (msg-123)"
    assert "msg-123" in bridge._reply_anchor_ids


def test_channel_reply_pauses_when_newer_pending_message_arrived():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)
    bridge._last_message_id = "incoming-2"
    bridge._pending_mentions.extend(
        [
            MentionEvent(
                message_id="incoming-1",
                parent_id=None,
                conversation_id=None,
                author="alex",
                prompt="first prompt",
                raw_content="@peer-agent first prompt",
                created_at="2026-04-15T23:00:00Z",
                space_id="space-123",
            ),
            MentionEvent(
                message_id="incoming-2",
                parent_id=None,
                conversation_id=None,
                author="sam",
                prompt="second prompt",
                raw_content="@peer-agent second prompt",
                created_at="2026-04-15T23:01:00Z",
                space_id="space-123",
            ),
        ]
    )

    asyncio.run(
        bridge.handle_tool_call(
            1,
            {"name": "reply", "arguments": {"text": "final answer", "reply_to": "incoming-1"}},
        )
    )

    assert client.sent == []
    result = bridge.writes[0]["result"]
    assert result["isError"] is True
    assert "reply paused" in result["content"][0]["text"]
    assert result["pending_messages"][0]["message_id"] == "incoming-2"


def test_channel_reply_force_skips_pre_exit_drain():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)
    bridge._last_message_id = "incoming-2"
    bridge._pending_mentions.extend(
        [
            MentionEvent(
                message_id="incoming-1",
                parent_id=None,
                conversation_id=None,
                author="alex",
                prompt="first prompt",
                raw_content="@peer-agent first prompt",
                created_at="2026-04-15T23:00:00Z",
                space_id="space-123",
            ),
            MentionEvent(
                message_id="incoming-2",
                parent_id=None,
                conversation_id=None,
                author="sam",
                prompt="second prompt",
                raw_content="@peer-agent second prompt",
                created_at="2026-04-15T23:01:00Z",
                space_id="space-123",
            ),
        ]
    )

    asyncio.run(
        bridge.handle_tool_call(
            1,
            {"name": "reply", "arguments": {"text": "final answer", "reply_to": "incoming-1", "force": True}},
        )
    )

    assert client.sent == [{"space_id": "space-123", "content": "final answer", "parent_id": "incoming-1"}]
    assert [event.message_id for event in bridge._pending_mentions] == ["incoming-2"]


def test_channel_can_publish_working_status_on_delivery():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)

    asyncio.run(bridge.publish_processing_status("incoming-123", "working"))

    assert client.processing_statuses == [
        {
            "message_id": "incoming-123",
            "status": "working",
            "agent_name": "peer-agent",
            "space_id": "space-123",
        }
    ]


def test_channel_processes_idle_event_before_jwt_reconnect(monkeypatch):
    """The event that wakes an idle stream must not be dropped for reconnect."""

    class FakeSseClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.connect_calls = 0

        def connect_sse(self, *, space_id):
            self.connect_calls += 1
            assert space_id == "space-123"
            return FakeSseResponse(
                {
                    "id": "incoming-123",
                    "content": "@peer-agent please check this",
                    "author": {"id": "user-123", "name": "alex", "type": "user"},
                    "mentions": ["peer-agent"],
                }
            )

        def get_message(self, message_id):
            assert message_id == "incoming-123"
            return {"message": {"metadata": {}}}

    client = FakeSseClient()
    bridge = CaptureBridge(client)
    delivered: list[MentionEvent] = []

    def capture_delivery(event):
        delivered.append(event)
        bridge.shutdown.set()

    bridge.enqueue_from_thread = capture_delivery
    ticks = iter([0, channel_mod._SSE_RECONNECT_INTERVAL + 1])
    monkeypatch.setattr(channel_mod.time, "monotonic", lambda: next(ticks, channel_mod._SSE_RECONNECT_INTERVAL + 2))

    channel_mod._sse_loop(bridge)

    assert [event.message_id for event in delivered] == ["incoming-123"]
    assert delivered[0].prompt == "please check this"


def _run_sse_loop_with_events(
    events: list[tuple[str, dict]],
    *,
    stop_after_delivery: int = 1,
    monkeypatch=None,
):
    """Drive `_sse_loop` against a scripted SSE event list and capture deliveries."""

    class ScriptedClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.connect_calls = 0
            self.get_message_calls: list[str] = []

        def connect_sse(self, *, space_id):
            self.connect_calls += 1
            return FakeMultiEventSseResponse(events)

        def get_message(self, message_id):
            self.get_message_calls.append(message_id)
            return {"message": {"metadata": {}}}

    client = ScriptedClient()
    bridge = CaptureBridge(client)
    delivered: list[MentionEvent] = []
    deliveries_needed = stop_after_delivery

    def capture_delivery(event):
        delivered.append(event)
        if len(delivered) >= deliveries_needed:
            bridge.shutdown.set()

    bridge.enqueue_from_thread = capture_delivery

    # Make reconnect path inert so the bridge processes all scripted events in
    # one pass and only stops when shutdown is set (either by delivery capture
    # or because the scripted stream is exhausted).
    if monkeypatch is not None:
        monkeypatch.setattr(channel_mod.time, "monotonic", lambda: 0.0)

    # Run the loop; it exits when the scripted iter_lines generator completes
    # and ConnectionError propagates, or when shutdown is set inside capture.
    # We wrap in a bounded number of connect_sse calls to avoid infinite loop
    # if the test script doesn't produce deliveries.
    original_connect = client.connect_sse

    def limited_connect(*args, **kwargs):
        if client.connect_calls >= 2:
            bridge.shutdown.set()
            raise ConnectionError("test: exhausted scripted connects")
        return original_connect(*args, **kwargs)

    client.connect_sse = limited_connect  # type: ignore[assignment]

    channel_mod._sse_loop(bridge)
    return bridge, client, delivered


def test_channel_skips_streaming_reply_non_final(monkeypatch):
    """Placeholder/progress chunks marked non-final must not wake the session."""

    events = [
        (
            "message",
            {
                "id": "stream-1",
                "content": "Working…",
                "author": {"id": "user-1", "name": "alex", "type": "user"},
                "mentions": ["peer-agent"],
                "metadata": {
                    "streaming_reply": {"enabled": True, "final": False, "runtime": "hermes_sdk"},
                },
            },
        ),
    ]
    _, _, delivered = _run_sse_loop_with_events(events, monkeypatch=monkeypatch)
    assert delivered == []


def test_channel_skips_working_progress_message(monkeypatch):
    """Defensive regex catches progress payloads even without streaming metadata."""

    events = [
        (
            "message",
            {
                "id": "progress-1",
                "content": "@peer-agent Working…",
                "author": {"id": "user-1", "name": "alex", "type": "user"},
                "mentions": ["peer-agent"],
            },
        ),
        (
            "message",
            {
                "id": "progress-2",
                "content": "Received",
                "author": {"id": "user-1", "name": "alex", "type": "user"},
                "mentions": ["peer-agent"],
            },
        ),
        (
            "message",
            {
                "id": "progress-3",
                "content": "@peer-agent Thinking...",
                "author": {"id": "user-1", "name": "alex", "type": "user"},
                "mentions": ["peer-agent"],
            },
        ),
        (
            "message",
            {
                "id": "progress-4",
                "content": "@peer-agent No response after 5m - session may need attention.",
                "author": {"id": "user-1", "name": "alex", "type": "user"},
                "mentions": ["peer-agent"],
            },
        ),
    ]
    _, _, delivered = _run_sse_loop_with_events(events, monkeypatch=monkeypatch)
    assert delivered == []


def test_channel_delivers_prompts_that_merely_start_with_progress_words(monkeypatch):
    """A legitimate prompt like '@peer-agent Working-state cleanup proposal' must land.

    The fallback progress regex must be anchored — otherwise user messages that
    happen to start with Working/Processing/Thinking/Received would be dropped
    silently. Regression for PR #70 review (2026-04-18).
    """

    events = [
        (
            "message",
            {
                "id": "real-prompt-1",
                "content": "@peer-agent Working-state cleanup proposal",
                "author": {"id": "user-1", "name": "alex", "type": "user"},
                "mentions": ["peer-agent"],
            },
        ),
    ]
    _, _, delivered = _run_sse_loop_with_events(events, monkeypatch=monkeypatch)
    assert len(delivered) == 1
    assert "Working-state cleanup proposal" in delivered[0].prompt


def test_channel_attaches_recent_backlog_to_wake_mention(monkeypatch):
    events = [
        (
            "message",
            {
                "id": "context-1",
                "content": "We merged the frontend polish branch.",
                "author": {"id": "user-1", "name": "alex", "type": "user"},
                "mentions": [],
            },
        ),
        (
            "message",
            {
                "id": "wake-1",
                "content": "@peer-agent please review the resulting UI state",
                "author": {"id": "user-1", "name": "alex", "type": "user"},
                "mentions": ["peer-agent"],
            },
        ),
    ]
    _, _, delivered = _run_sse_loop_with_events(events, monkeypatch=monkeypatch)
    assert len(delivered) == 1
    assert delivered[0].message_id == "wake-1"
    assert delivered[0].backlog == [
        {
            "message_id": "context-1",
            "author": "alex",
            "content": "We merged the frontend polish branch.",
            "parent_id": None,
            "conversation_id": None,
            "created_at": None,
        }
    ]


def test_channel_delivers_processing_webhook_errors_prompt(monkeypatch):
    """`Processing webhook errors` is a real user prompt, not a progress marker."""

    events = [
        (
            "message",
            {
                "id": "real-prompt-2",
                "content": "@peer-agent Processing webhook errors in the dispatch queue",
                "author": {"id": "user-1", "name": "alex", "type": "user"},
                "mentions": ["peer-agent"],
            },
        ),
    ]
    _, _, delivered = _run_sse_loop_with_events(events, monkeypatch=monkeypatch)
    assert len(delivered) == 1
    assert "Processing webhook errors" in delivered[0].prompt


def test_channel_delivers_thinking_through_issue_prompt(monkeypatch):
    """`Thinking through this API issue` must be delivered, not suppressed."""

    events = [
        (
            "message",
            {
                "id": "real-prompt-3",
                "content": "@peer-agent Thinking through this API issue",
                "author": {"id": "user-1", "name": "alex", "type": "user"},
                "mentions": ["peer-agent"],
            },
        ),
    ]
    _, _, delivered = _run_sse_loop_with_events(events, monkeypatch=monkeypatch)
    assert len(delivered) == 1
    assert "Thinking through this API issue" in delivered[0].prompt


def test_channel_delivers_message_updated_final(monkeypatch):
    """When hermes streams a final payload via message_updated we deliver it."""

    placeholder = {
        "id": "hermes-1",
        "content": "Working…",
        "author": {"id": "agent-2", "name": "frontend_sentinel", "type": "agent"},
        "mentions": ["peer-agent"],
        "metadata": {
            "streaming_reply": {"enabled": True, "final": False, "runtime": "hermes_sdk"},
        },
    }
    final_update = {
        "id": "hermes-1",
        "content": "@peer-agent here is the real reply",
        "author": {"id": "agent-2", "name": "frontend_sentinel", "type": "agent"},
        "mentions": ["peer-agent"],
        "metadata": {
            "streaming_reply": {"enabled": True, "final": True, "runtime": "hermes_sdk"},
        },
    }
    events = [("message", placeholder), ("message_updated", final_update)]
    _, _, delivered = _run_sse_loop_with_events(events, monkeypatch=monkeypatch)
    assert [e.message_id for e in delivered] == ["hermes-1"]
    assert delivered[0].prompt == "here is the real reply"


def test_channel_skips_message_updated_for_already_delivered(monkeypatch):
    """Once a final payload is delivered, subsequent updates for that id do not re-wake."""

    payload = {
        "id": "msg-dup",
        "content": "@peer-agent please review",
        "author": {"id": "user-1", "name": "alex", "type": "user"},
        "mentions": ["peer-agent"],
    }
    events = [("message", payload), ("message_updated", payload)]
    _, _, delivered = _run_sse_loop_with_events(events, monkeypatch=monkeypatch)
    assert [e.message_id for e in delivered] == ["msg-dup"]


def test_channel_processing_status_can_be_disabled():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client, processing_status=False)

    asyncio.run(bridge.publish_processing_status("incoming-123", "working"))

    assert client.processing_statuses == []


def test_channel_returns_empty_optional_mcp_lists():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)

    asyncio.run(bridge.handle_request({"id": 1, "method": "resources/list"}))
    asyncio.run(bridge.handle_request({"id": 2, "method": "resources/templates/list"}))
    asyncio.run(bridge.handle_request({"id": 3, "method": "prompts/list"}))

    assert bridge.writes == [
        {"jsonrpc": "2.0", "id": 1, "result": {"resources": []}},
        {"jsonrpc": "2.0", "id": 2, "result": {"resourceTemplates": []}},
        {"jsonrpc": "2.0", "id": 3, "result": {"prompts": []}},
    ]


def test_channel_tools_include_polling_fallback():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)

    asyncio.run(bridge.handle_tools_list(1))

    tools = bridge.writes[0]["result"]["tools"]
    assert {tool["name"] for tool in tools} == {"reply", "get_messages"}


def test_channel_get_messages_returns_pending_mentions():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)
    bridge._pending_mentions.append(
        MentionEvent(
            message_id="incoming-123",
            parent_id=None,
            conversation_id=None,
            author="alex",
            prompt="please check this",
            raw_content="@peer-agent please check this",
            created_at="2026-04-15T23:00:00Z",
            space_id="space-123",
        )
    )

    asyncio.run(bridge.handle_tool_call(1, {"name": "get_messages", "arguments": {"limit": 1}}))

    result = bridge.writes[0]["result"]
    assert "incoming-123" in result["content"][0]["text"]
    assert "please check this" in result["content"][0]["text"]
    assert bridge._pending_mentions == []


def test_channel_notification_includes_backlog_packet():
    async def run():
        client = FakeClient("axp_a_AgentKey.Secret")
        bridge = CaptureBridge(client)
        bridge.initialized.set()
        await bridge.mention_queue.put(
            MentionEvent(
                message_id="incoming-123",
                parent_id=None,
                conversation_id=None,
                author="alex",
                prompt="please check this",
                raw_content="@peer-agent please check this",
                created_at=None,
                space_id="space-123",
                backlog=[
                    {
                        "message_id": "context-1",
                        "author": "sam",
                        "content": "Earlier context",
                        "parent_id": None,
                        "conversation_id": None,
                        "created_at": "2026-04-15T22:59:00Z",
                    }
                ],
            )
        )
        task = asyncio.create_task(bridge.emit_mentions())
        await asyncio.wait_for(bridge.mention_queue.join(), timeout=1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return bridge

    bridge = asyncio.run(run())

    payload = bridge.writes[0]
    assert "Earlier context" in payload["params"]["content"]
    assert "Trigger mention:" in payload["params"]["content"]
    assert payload["params"]["meta"]["backlog_count"] == 1
    assert payload["params"]["meta"]["backlog"][0]["message_id"] == "context-1"


def test_channel_notification_metadata_matches_claude_channel_contract():
    async def run():
        client = FakeClient("axp_a_AgentKey.Secret")
        bridge = CaptureBridge(client)
        bridge.initialized.set()
        await bridge.mention_queue.put(
            MentionEvent(
                message_id="incoming-123",
                parent_id=None,
                conversation_id="conversation-ignored",
                author="alex",
                prompt="please check this",
                raw_content="@peer-agent please check this",
                created_at=None,
                space_id="space-123",
            )
        )
        task = asyncio.create_task(bridge.emit_mentions())
        await asyncio.wait_for(bridge.mention_queue.join(), timeout=1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return bridge

    bridge = asyncio.run(run())

    payload = bridge.writes[0]
    assert payload["method"] == "notifications/claude/channel"
    meta = payload["params"]["meta"]
    assert meta["message_id"] == "incoming-123"
    assert isinstance(meta["ts"], str)
    assert meta["ts"]
    assert "raw_content" not in meta
    assert "conversation_id" not in meta
    assert "parent_id" not in meta


def test_channel_env_file_sets_missing_runtime_env(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AX_CONFIG_FILE=/tmp/agent/.ax/config.toml\n"
        "AX_SPACE_ID=space-123\n"
        "AX_AGENT_NAME=ignored-agent\n"
    )
    monkeypatch.setenv("AX_AGENT_NAME", "existing-agent")

    _load_channel_env(env_file)

    assert os.environ["AX_CONFIG_FILE"] == "/tmp/agent/.ax/config.toml"
    assert os.environ["AX_SPACE_ID"] == "space-123"
    assert os.environ["AX_AGENT_NAME"] == "existing-agent"


def test_listener_treats_parent_reply_as_delivery_signal():
    anchors = {"agent-message-1"}
    data = {
        "id": "reply-1",
        "content": "I looked at this",
        "parent_id": "agent-message-1",
        "author": {"id": "other-agent", "name": "demo-agent", "type": "agent"},
        "mentions": [],
    }

    assert _should_respond(data, "peer-agent", "agent-123", reply_anchor_ids=anchors) is True


def test_listener_treats_conversation_reply_as_delivery_signal():
    anchors = {"agent-message-1"}
    data = {
        "id": "reply-1",
        "content": "I looked at this",
        "conversation_id": "agent-message-1",
        "author": {"id": "other-agent", "name": "demo-agent", "type": "agent"},
        "mentions": [],
    }

    assert _should_respond(data, "peer-agent", "agent-123", reply_anchor_ids=anchors) is True


def test_listener_tracks_self_authored_messages_without_responding():
    anchors: set[str] = set()
    data = {
        "id": "agent-message-1",
        "content": "@demo-agent please check this",
        "author": {"id": "agent-123", "name": "peer-agent", "type": "agent"},
        "mentions": ["demo-agent"],
    }

    assert _is_self_authored(data, "peer-agent", "agent-123") is True
    _remember_reply_anchor(anchors, data["id"])
    assert _should_respond(data, "peer-agent", "agent-123", reply_anchor_ids=anchors) is False
    assert anchors == {"agent-message-1"}
