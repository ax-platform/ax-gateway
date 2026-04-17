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
    def __init__(self, client, *, agent_id="agent-123", processing_status=True):
        super().__init__(
            client=client,
            agent_name="anvil",
            agent_id=agent_id,
            space_id="space-123",
            queue_size=10,
            debug=False,
            processing_status=processing_status,
        )
        self.writes = []

    async def write_message(self, payload):
        self.writes.append(payload)


class FakeSseResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        yield "event: message"
        yield f"data: {json.dumps(self.payload)}"
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
            "agent_name": "anvil",
            "space_id": "space-123",
        }
    ]
    result = bridge.writes[0]["result"]
    assert result["content"][0]["text"] == "sent reply to incoming-123 (msg-123)"
    assert "msg-123" in bridge._reply_anchor_ids


def test_channel_can_publish_working_status_on_delivery():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)

    asyncio.run(bridge.publish_processing_status("incoming-123", "working"))

    assert client.processing_statuses == [
        {
            "message_id": "incoming-123",
            "status": "working",
            "agent_name": "anvil",
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
                    "content": "@anvil please check this",
                    "author": {"id": "user-123", "name": "madtank", "type": "user"},
                    "mentions": ["anvil"],
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


def test_channel_materializes_shared_task_metadata_for_agent_prompt(monkeypatch):
    class FakeSseClient(FakeClient):
        def connect_sse(self, *, space_id):
            assert space_id == "space-123"
            return FakeSseResponse(
                {
                    "id": "incoming-share",
                    "content": "@anvil can you see what I shared?",
                    "author": {"id": "user-123", "name": "madtank", "type": "user"},
                    "mentions": ["anvil"],
                    "metadata": {
                        "forward": {
                            "intent": "share",
                            "resource_type": "task",
                            "resource_id": "task-123",
                            "task_id": "task-123",
                            "source_message_id": "source-msg-123",
                            "source_card_id": "task-signal:task-123",
                            "title": "Fix Share delivery context",
                            "summary": "The recipient should know this is a task.",
                        }
                    },
                }
            )

        def get_message(self, message_id):
            raise AssertionError("SSE metadata was already complete")

    client = FakeSseClient()
    bridge = CaptureBridge(client)
    delivered: list[MentionEvent] = []

    def capture_delivery(event):
        delivered.append(event)
        bridge.shutdown.set()

    bridge.enqueue_from_thread = capture_delivery
    monkeypatch.setattr(channel_mod.time, "monotonic", lambda: 0)

    channel_mod._sse_loop(bridge)

    assert [event.message_id for event in delivered] == ["incoming-share"]
    assert "can you see what I shared?" in delivered[0].prompt
    assert "Shared object:" in delivered[0].prompt
    assert "- resource_type: task" in delivered[0].prompt
    assert "- task_id: task-123" in delivered[0].prompt
    assert "axctl tasks get task-123 --space-id space-123 --json" in delivered[0].prompt
    assert delivered[0].metadata["forward"]["resource_type"] == "task"


def test_channel_fetches_attachment_metadata_and_adds_inspection_hint(monkeypatch):
    class FakeSseClient(FakeClient):
        def connect_sse(self, *, space_id):
            assert space_id == "space-123"
            return FakeSseResponse(
                {
                    "id": "incoming-image",
                    "content": "@anvil please inspect this image",
                    "author": {"id": "user-123", "name": "madtank", "type": "user"},
                    "mentions": ["anvil"],
                    "metadata": {},
                }
            )

        def get_message(self, message_id):
            assert message_id == "incoming-image"
            attachment = {
                "id": "att-123",
                "filename": "image.png",
                "content_type": "image/png",
                "context_key": "upload:image.png:att-123",
            }
            return {"message": {"metadata": {"accepted_attachments": [attachment]}}}

    client = FakeSseClient()
    bridge = CaptureBridge(client)
    delivered: list[MentionEvent] = []

    def capture_delivery(event):
        delivered.append(event)
        bridge.shutdown.set()

    bridge.enqueue_from_thread = capture_delivery
    monkeypatch.setattr(channel_mod.time, "monotonic", lambda: 0)

    channel_mod._sse_loop(bridge)

    assert [event.message_id for event in delivered] == ["incoming-image"]
    assert "Attachments:" in delivered[0].prompt
    assert "image.png (image/png, id=att-123, context_key=upload:image.png:att-123)" in delivered[0].prompt
    assert "axctl context get 'upload:image.png:att-123' --space-id space-123 --json" in delivered[0].prompt
    assert delivered[0].attachments == [
        {
            "id": "att-123",
            "filename": "image.png",
            "content_type": "image/png",
            "context_key": "upload:image.png:att-123",
        }
    ]


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
            author="madtank",
            prompt="please check this",
            raw_content="@anvil please check this",
            created_at="2026-04-15T23:00:00Z",
            space_id="space-123",
            attachments=[{"id": "att-1", "filename": "notes.md"}],
            metadata={"forward": {"resource_type": "context"}},
        )
    )

    asyncio.run(bridge.handle_tool_call(1, {"name": "get_messages", "arguments": {"limit": 1}}))

    result = bridge.writes[0]["result"]
    assert "incoming-123" in result["content"][0]["text"]
    assert "please check this" in result["content"][0]["text"]
    assert "notes.md" in result["content"][0]["text"]
    assert "resource_type" in result["content"][0]["text"]
    assert bridge._pending_mentions == []


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
                author="madtank",
                prompt="please check this",
                raw_content="@anvil please check this",
                created_at=None,
                space_id="space-123",
                metadata={"forward": {"resource_type": "task", "task_id": "task-123"}},
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
    assert meta["forward"] == {"resource_type": "task", "task_id": "task-123"}


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
        "author": {"id": "other-agent", "name": "orion", "type": "agent"},
        "mentions": [],
    }

    assert _should_respond(data, "anvil", "agent-123", reply_anchor_ids=anchors) is True


def test_listener_treats_conversation_reply_as_delivery_signal():
    anchors = {"agent-message-1"}
    data = {
        "id": "reply-1",
        "content": "I looked at this",
        "conversation_id": "agent-message-1",
        "author": {"id": "other-agent", "name": "orion", "type": "agent"},
        "mentions": [],
    }

    assert _should_respond(data, "anvil", "agent-123", reply_anchor_ids=anchors) is True


def test_listener_tracks_self_authored_messages_without_responding():
    anchors: set[str] = set()
    data = {
        "id": "agent-message-1",
        "content": "@orion please check this",
        "author": {"id": "agent-123", "name": "anvil", "type": "agent"},
        "mentions": ["orion"],
    }

    assert _is_self_authored(data, "anvil", "agent-123") is True
    _remember_reply_anchor(anchors, data["id"])
    assert _should_respond(data, "anvil", "agent-123", reply_anchor_ids=anchors) is False
    assert anchors == {"agent-message-1"}
