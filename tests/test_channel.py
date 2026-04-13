"""Tests for the Claude Code channel bridge identity boundary."""

import asyncio

from ax_cli.commands.channel import ChannelBridge
from ax_cli.commands.listen import _is_self_authored, _remember_reply_anchor, _should_respond


class FakeClient:
    def __init__(self, token: str = "axp_a_AgentKey.Secret", *, agent_id: str = "agent-123"):
        self.token = token
        self.agent_id = agent_id
        self._use_exchange = token.startswith("axp_")
        self.sent = []

    def send_message(self, space_id, content, *, parent_id=None, **kwargs):
        self.sent.append({"space_id": space_id, "content": content, "parent_id": parent_id, **kwargs})
        return {"message": {"id": "msg-123"}}


class CaptureBridge(ChannelBridge):
    def __init__(self, client, *, agent_id="agent-123"):
        super().__init__(
            client=client,
            agent_name="anvil",
            agent_id=agent_id,
            space_id="space-123",
            queue_size=10,
            debug=False,
        )
        self.writes = []

    async def write_message(self, payload):
        self.writes.append(payload)


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
    result = bridge.writes[0]["result"]
    assert result["content"][0]["text"] == "sent reply to incoming-123 (msg-123)"
    assert "msg-123" in bridge._reply_anchor_ids


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
