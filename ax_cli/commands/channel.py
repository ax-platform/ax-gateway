"""ax channel — Claude Code channel bridge over MCP stdio.

Reuses the ax listen SSE/auth/@mention plumbing, but exposes it as a thin
MCP server so Claude Code can receive messages and reply in-thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
import typer

from ..config import get_client, resolve_agent_name, resolve_space_id
from ..output import console
from .listen import (
    _is_self_authored,
    _iter_sse,
    _message_sender_identity,
    _remember_reply_anchor,
    _should_respond,
    _strip_mention,
)

app = typer.Typer(name="channel", help="Run an aX Claude Code channel over MCP stdio", no_args_is_help=False)

PROTOCOL_VERSION = "2025-11-25"
SERVER_NAME = "ax-channel"
SERVER_VERSION = "0.1.0"
SEEN_MAX = 500
BACKLOG_BUFFER_MAX = 100
CHANNEL_ENV_PATH = Path.home() / ".claude" / "channels" / "ax-channel" / ".env"


def _load_channel_env(path: Path = CHANNEL_ENV_PATH) -> None:
    """Load KEY=VALUE channel env defaults without overriding real env vars."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip("\"'")


@dataclass(slots=True)
class MentionEvent:
    message_id: str
    parent_id: str | None
    conversation_id: str | None
    author: str
    prompt: str
    raw_content: str
    created_at: str | None
    space_id: str
    attachments: list[dict[str, Any]] | None = None
    backlog: list[dict[str, Any]] | None = None


class ChannelBridge:
    def __init__(
        self,
        *,
        client,
        agent_name: str,
        agent_id: str | None,
        space_id: str,
        queue_size: int,
        debug: bool,
        processing_status: bool,
        pre_exit_drain: bool,
        backlog_limit: int,
        backlog_in_content: bool,
    ) -> None:
        self.client = client
        self.agent_name = agent_name
        self.agent_id = agent_id
        self.space_id = space_id
        self.debug = debug
        self.processing_status = processing_status
        self.pre_exit_drain = pre_exit_drain
        self.backlog_limit = max(0, backlog_limit)
        self.backlog_in_content = backlog_in_content
        self.loop: asyncio.AbstractEventLoop | None = None
        self.mention_queue: asyncio.Queue[MentionEvent] = asyncio.Queue(maxsize=queue_size)
        self.initialized = asyncio.Event()
        self.shutdown = threading.Event()
        self._stderr_lock = threading.Lock()
        self._write_lock = asyncio.Lock()
        self._last_message_id: str | None = None
        self._reply_anchor_ids: set[str] = set()
        self._pending_mentions: list[MentionEvent] = []

    def _format_delivery_content(self, event: MentionEvent) -> str:
        if not self.backlog_in_content or self.backlog_limit <= 0 or not event.backlog:
            return event.prompt
        lines = [
            "Context since your previous channel wake-up, capped and newest last:",
        ]
        for item in event.backlog[-self.backlog_limit :]:
            author = str(item.get("author") or "unknown")
            content = str(item.get("content") or "").replace("\n", " ").strip()
            if len(content) > 240:
                content = content[:237].rstrip() + "..."
            lines.append(f"- @{author}: {content}")
        lines.extend(["", "Trigger mention:", event.prompt])
        return "\n".join(lines)

    def _pending_drain_events(self, reply_to: str | None, *, limit: int) -> list[MentionEvent]:
        if not self._pending_mentions:
            return []
        if reply_to:
            ids = [event.message_id for event in self._pending_mentions]
            if reply_to in ids:
                pending = self._pending_mentions[ids.index(reply_to) + 1 :]
            else:
                pending = [event for event in self._pending_mentions if event.message_id != reply_to]
        else:
            pending = list(self._pending_mentions)
        return pending[: max(1, limit)]

    @staticmethod
    def _serialize_pending(events: list[MentionEvent]) -> list[dict[str, Any]]:
        return [
            {
                "message_id": event.message_id,
                "author": event.author,
                "content": event.prompt,
                "parent_id": event.parent_id,
                "ts": event.created_at,
            }
            for event in events
        ]

    def log(self, message: str) -> None:
        if not self.debug:
            return
        with self._stderr_lock:
            print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)

    def enqueue_from_thread(self, event: MentionEvent) -> None:
        if not self.loop or self.shutdown.is_set():
            self.log(f"enqueue_from_thread: dropped (loop={self.loop is not None}, shutdown={self.shutdown.is_set()})")
            return

        def _push() -> None:
            try:
                self.mention_queue.put_nowait(event)
                self.log(f"enqueue: queued {event.message_id[:12]} (qsize={self.mention_queue.qsize()})")
            except asyncio.QueueFull:
                self.log(f"queue full — dropping mention {event.message_id}")

        self.loop.call_soon_threadsafe(_push)

    async def write_message(self, payload: dict[str, Any]) -> None:
        async with self._write_lock:
            raw = json.dumps(payload, separators=(",", ":")) + "\n"
            sys.stdout.write(raw)
            sys.stdout.flush()
            method = payload.get("method", payload.get("id", "?"))
            self.log(f"wrote to stdout: {method} ({len(raw)} bytes)")

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        await self.write_message({"jsonrpc": "2.0", "method": method, "params": params})

    async def publish_processing_status(self, message_id: str, status: str) -> None:
        """Best-effort Activity Stream signal for channel delivery/progress.

        This lets the frontend show the same inline "agent is working" affordance
        for Claude Code channel sessions that it shows for other agent runtimes.
        It is intentionally non-blocking: channel delivery/replies must still
        work if the progress endpoint is unavailable.
        """
        if not self.processing_status:
            return
        try:

            def _send_status():
                return self.client.set_agent_processing_status(
                    message_id,
                    status,
                    agent_name=self.agent_name,
                    space_id=self.space_id,
                )

            await asyncio.to_thread(_send_status)
            self.log(f"processing status {status} for {message_id[:12]}")
        except Exception as exc:  # pragma: no cover - live best-effort path
            self.log(f"processing status failed for {message_id[:12]}: {exc}")

    async def send_response(self, request_id: Any, result: dict[str, Any]) -> None:
        await self.write_message({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def send_error(self, request_id: Any, code: int, message: str) -> None:
        await self.write_message({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    async def emit_mentions(self) -> None:
        self.log("emit_mentions: task started")
        while True:
            self.log(f"emit_mentions: waiting for event (initialized={self.initialized.is_set()})")
            event = await self.mention_queue.get()
            self.log(f"emit_mentions: got event {event.message_id[:12]}")
            try:
                self.log(f"emit_mentions: waiting initialized (is_set={self.initialized.is_set()})")
                await self.initialized.wait()
                self.log("emit_mentions: initialized done, sending notification")

                self._last_message_id = event.message_id
                self._pending_mentions.append(event)
                if len(self._pending_mentions) > SEEN_MAX:
                    self._pending_mentions = self._pending_mentions[-SEEN_MAX // 2 :]
                ts = event.created_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                meta: dict[str, Any] = {
                    "chat_id": event.space_id,
                    "message_id": event.message_id,
                    "user": event.author,
                    "sender": event.author,
                    "source": "ax",
                    "space_id": event.space_id,
                    "ts": ts,
                }
                if event.parent_id:
                    meta["parent_id"] = event.parent_id
                if event.attachments:
                    meta["attachments"] = event.attachments
                if event.backlog:
                    meta["backlog"] = event.backlog[-self.backlog_limit :] if self.backlog_limit > 0 else []
                    meta["backlog_count"] = len(event.backlog)
                await self.send_notification(
                    "notifications/claude/channel",
                    {
                        "content": self._format_delivery_content(event),
                        "meta": meta,
                    },
                )
                await self.publish_processing_status(event.message_id, "working")
                self.log(f"delivered mention {event.message_id} from {event.author}")
            finally:
                self.mention_queue.task_done()

    async def handle_initialize(self, request_id: Any) -> None:
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "tools": {},
                "experimental": {"claude/channel": {}},
            },
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Messages from aX arrive via notifications/claude/channel. "
                "Your transcript is not sent back to aX automatically. "
                "Use the reply tool for every response you want posted back to aX. "
                "Pass reply_to to target a specific incoming aX message_id; if omitted, the latest inbound message is used."
            ),
        }
        await self.send_response(request_id, result)

    async def handle_tools_list(self, request_id: Any) -> None:
        await self.send_response(
            request_id,
            {
                "tools": [
                    {
                        "name": "reply",
                        "description": "Reply to an aX channel message in-thread.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "Message text to send back to aX."},
                                "reply_to": {
                                    "type": "string",
                                    "description": "aX message_id to reply to. Defaults to the latest inbound message.",
                                },
                                "force": {
                                    "type": "boolean",
                                    "description": (
                                        "Send even when pre-exit drain finds newer pending channel messages."
                                    ),
                                },
                                "drain_limit": {
                                    "type": "number",
                                    "description": "Maximum pending messages to return when pre-exit drain pauses.",
                                },
                            },
                            "required": ["text"],
                        },
                    },
                    {
                        "name": "get_messages",
                        "description": "Get pending aX channel messages for clients that need a polling fallback.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "limit": {
                                    "type": "number",
                                    "description": "Max messages to return (default: 10).",
                                },
                                "mark_read": {
                                    "type": "boolean",
                                    "description": "Remove returned messages from the pending fallback queue (default: true).",
                                },
                            },
                        },
                    },
                ]
            },
        )

    async def handle_empty_list(self, request_id: Any, key: str) -> None:
        await self.send_response(request_id, {key: []})

    async def handle_get_messages(self, request_id: Any, arguments: dict[str, Any]) -> None:
        try:
            limit = max(1, int(arguments.get("limit") or 10))
        except (TypeError, ValueError):
            limit = 10
        mark_read = arguments.get("mark_read") is not False
        pending = self._pending_mentions[:limit]
        if mark_read:
            self._pending_mentions = self._pending_mentions[len(pending) :]
        if not pending:
            text = "No pending messages."
        else:
            text = json.dumps(
                self._serialize_pending(pending),
                indent=2,
            )
        await self.send_response(request_id, {"content": [{"type": "text", "text": text}]})

    async def handle_tool_call(self, request_id: Any, params: dict[str, Any]) -> None:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "get_messages":
            await self.handle_get_messages(request_id, arguments)
            return
        if name != "reply":
            await self.send_error(request_id, -32601, f"Unknown tool: {name}")
            return

        text = str(arguments.get("text") or "").strip()
        reply_to = arguments.get("reply_to") or self._last_message_id
        force = bool(arguments.get("force"))
        try:
            drain_limit = max(1, int(arguments.get("drain_limit") or 10))
        except (TypeError, ValueError):
            drain_limit = 10
        if not text:
            await self.send_error(request_id, -32602, "reply.text is required")
            return
        if not reply_to:
            await self.send_error(request_id, -32602, "reply_to is required until at least one aX message has arrived")
            return
        if getattr(self.client, "_use_exchange", False) and not str(getattr(self.client, "token", "")).startswith(
            "axp_a_"
        ):
            await self.send_response(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "reply failed: ax channel requires an agent-bound PAT (axp_a_). "
                                "User PATs may create agent PATs but cannot speak as an agent."
                            ),
                        }
                    ],
                    "isError": True,
                },
            )
            return
        if not self.agent_id:
            await self.send_response(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": "reply failed: channel agent_id is required for agent runtime replies.",
                        }
                    ],
                    "isError": True,
                },
            )
            return

        drain_events = self._pending_drain_events(str(reply_to) if reply_to else None, limit=drain_limit)
        if self.pre_exit_drain and drain_events and not force:
            await self.send_response(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "reply paused: newer pending aX messages arrived before final send. "
                                "Review these first, then call reply again with force=true if this response is still correct.\n"
                                + json.dumps(self._serialize_pending(drain_events), indent=2)
                            ),
                        }
                    ],
                    "isError": True,
                    "pending_messages": self._serialize_pending(drain_events),
                },
            )
            return

        try:

            def _send_as_agent():
                """Send using the agent_access JWT produced by the agent-bound PAT."""
                return self.client.send_message(self.space_id, text, parent_id=reply_to)

            data = await asyncio.to_thread(_send_as_agent)
            message = data.get("message", data)
            sent_id = message.get("id") or data.get("id")
            _remember_reply_anchor(self._reply_anchor_ids, sent_id)
            if reply_to:
                self._pending_mentions = [
                    event for event in self._pending_mentions if event.message_id != str(reply_to)
                ]
            await self.publish_processing_status(reply_to, "completed")
            await self.send_response(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": f"sent reply to {reply_to}" + (f" ({sent_id})" if sent_id else ""),
                        }
                    ]
                },
            )
            self.log(f"replied to {reply_to}")
        except Exception as exc:  # pragma: no cover - exercised in live runs
            await self.send_response(
                request_id,
                {
                    "content": [{"type": "text", "text": f"reply failed: {exc}"}],
                    "isError": True,
                },
            )

    async def handle_request(self, request: dict[str, Any]) -> None:
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        if method == "initialize":
            await self.handle_initialize(request_id)
        elif method == "tools/list":
            await self.handle_tools_list(request_id)
        elif method == "resources/list":
            await self.handle_empty_list(request_id, "resources")
        elif method == "resources/templates/list":
            await self.handle_empty_list(request_id, "resourceTemplates")
        elif method == "prompts/list":
            await self.handle_empty_list(request_id, "prompts")
        elif method == "tools/call":
            await self.handle_tool_call(request_id, params)
        elif method == "ping":
            await self.send_response(request_id, {})
        else:
            await self.send_error(request_id, -32601, f"Method not found: {method}")

    async def handle_notification(self, notification: dict[str, Any]) -> None:
        method = notification.get("method")
        if method == "notifications/initialized":
            self.initialized.set()
            self.log("client initialized")
        elif method == "notifications/cancelled":
            self.log("received cancellation notification")
        else:
            self.log(f"ignored notification: {method}")

    async def serve_stdio(self) -> None:
        self.loop = asyncio.get_running_loop()
        emitter = asyncio.create_task(self.emit_mentions())
        try:
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
                if line == "":
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    self.log(f"invalid json: {exc}")
                    continue
                if "id" in message:
                    await self.handle_request(message)
                else:
                    await self.handle_notification(message)
        finally:
            self.shutdown.set()
            emitter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await emitter


def _resolve_agent_id(client, agent_name: str | None) -> str | None:
    if not agent_name:
        return None
    try:
        agents_data = client.list_agents()
    except Exception:
        return None
    agents = agents_data if isinstance(agents_data, list) else agents_data.get("agents", [])
    for agent in agents:
        if agent.get("name", "").lower() == agent_name.lower():
            return agent.get("id")
    return None


_SSE_RECONNECT_INTERVAL = 600  # reconnect every 10 min to refresh JWT before 15-min expiry

# Defensive fallback for runtime progress messages that arrive without the
# `metadata.streaming_reply.final=false` hint. Every branch is anchored with
# \Z so we only suppress lines that are EXACTLY progress chatter — never
# legitimate user prompts that merely start with the same word. Example:
# "Working…" is dropped, but "Working-state cleanup proposal" is delivered.
#
# Emitters that feed this:
#   - hermes_sdk / ax-agents callbacks: "Working…", "Received", "Thinking...",
#     "Processing..." (exact word + trailing whitespace/dot/ellipsis only).
#   - channel/server.ts: "No response after <N>[smh] - session may need
#     attention." — the `\d+[smh]` anchor keeps user phrases like "No response
#     after the deploy broke things" from matching.
_RUNTIME_PROGRESS_RE = re.compile(
    r"^(?:"
    r"(?:Working|Received|Thinking|Processing)[\s.\u2026]*"
    r"|"
    r"No response after\s+\d+\s*[smh]\b.*"
    r")\Z",
    re.IGNORECASE,
)
_LEADING_MENTION_RE = re.compile(r"^@[\w-]+\s*[-\u2014]?\s*")


def _compact_backlog_event(data: dict[str, Any]) -> dict[str, Any]:
    author, _ = _message_sender_identity(data)
    content = str(data.get("content") or "").replace("\n", " ").strip()
    if len(content) > 280:
        content = content[:277].rstrip() + "..."
    return {
        "message_id": data.get("id") or "",
        "author": author or "unknown",
        "content": content,
        "parent_id": data.get("parent_id"),
        "conversation_id": data.get("conversation_id"),
        "created_at": data.get("created_at") or data.get("timestamp"),
    }


def _append_recent_event(recent_events: list[dict[str, Any]], data: dict[str, Any]) -> None:
    item = _compact_backlog_event(data)
    if not item.get("message_id") or not item.get("content"):
        return
    recent_events.append(item)
    if len(recent_events) > BACKLOG_BUFFER_MAX:
        del recent_events[: len(recent_events) - BACKLOG_BUFFER_MAX]


def _sse_loop(bridge: ChannelBridge) -> None:
    seen_ids: set[str] = set()
    recent_events: list[dict[str, Any]] = []
    backoff = 1
    bridge.log(f"listening for @{bridge.agent_name} in {bridge.space_id}")

    while not bridge.shutdown.is_set():
        try:
            connect_time = time.monotonic()
            with bridge.client.connect_sse(space_id=bridge.space_id) as response:
                if response.status_code != 200:
                    raise ConnectionError(f"SSE failed: {response.status_code}")
                backoff = 1
                bridge.log(f"SSE connected (status {response.status_code})")
                for event_type, data in _iter_sse(response):
                    if bridge.shutdown.is_set():
                        return
                    # Reconnect before JWT expires (15-min TTL, reconnect at
                    # 10 min), but never drop the event that woke an idle
                    # stream. Process the current event first, then reconnect.
                    reconnect_after_event = time.monotonic() - connect_time > _SSE_RECONNECT_INTERVAL
                    if event_type in {"bootstrap", "heartbeat", "ping", "connected", "identity_bootstrap"}:
                        bridge.log(f"skip {event_type}")
                        if reconnect_after_event:
                            bridge.log("SSE reconnecting to refresh JWT")
                            break
                        continue
                    # Accept message/mention (creation) and message_updated
                    # (final streamed content). Hermes-runtime sentinels seed a
                    # placeholder message on start and overwrite it in place
                    # via message_updated events as the reply streams in.
                    if event_type not in {"message", "mention", "message_updated"} or not isinstance(data, dict):
                        bridge.log(f"skip non-msg: {event_type}")
                        if reconnect_after_event:
                            bridge.log("SSE reconnecting to refresh JWT")
                            break
                        continue

                    is_update = event_type == "message_updated"
                    message_id = data.get("id") or ""
                    content_preview = (data.get("content") or "")[:60]
                    bridge.log(f"event {event_type} id={message_id[:12]} content={content_preview!r}")
                    # New messages: skip if already delivered. Updates bypass
                    # the dedup so the final streamed payload can supersede the
                    # placeholder.
                    if not message_id or (not is_update and message_id in seen_ids):
                        bridge.log("  -> skip: dup or no id")
                        if reconnect_after_event:
                            bridge.log("SSE reconnecting to refresh JWT")
                            break
                        continue
                    if is_update and message_id in seen_ids:
                        bridge.log("  -> skip: update for already-delivered msg")
                        if reconnect_after_event:
                            bridge.log("SSE reconnecting to refresh JWT")
                            break
                        continue

                    # Skip runtime progress chatter. Two signals:
                    #   1. metadata.streaming_reply.final is explicitly false,
                    #      meaning the payload is a placeholder/progress chunk
                    #      the runtime will overwrite via message_updated.
                    #   2. Defensive regex: first line matches the known
                    #      progress patterns ("Working…", "Received",
                    #      "Thinking", "Processing", "No response after").
                    # We skip WITHOUT adding to seen_ids so the subsequent
                    # final message_updated for the same id can be delivered.
                    metadata_obj = data.get("metadata") or {}
                    streaming = metadata_obj.get("streaming_reply") if isinstance(metadata_obj, dict) else None
                    if isinstance(streaming, dict) and streaming.get("final") is False:
                        bridge.log("  -> skip: streaming_reply non-final")
                        if reconnect_after_event:
                            bridge.log("SSE reconnecting to refresh JWT")
                            break
                        continue
                    raw_first_line = (data.get("content") or "").strip().split("\n", 1)[0].strip()
                    stripped_first_line = _LEADING_MENTION_RE.sub("", raw_first_line, count=1).strip()
                    if _RUNTIME_PROGRESS_RE.match(stripped_first_line):
                        bridge.log(f"  -> skip: runtime progress message ({raw_first_line!r})")
                        if reconnect_after_event:
                            bridge.log("SSE reconnecting to refresh JWT")
                            break
                        continue

                    if _is_self_authored(data, bridge.agent_name, bridge.agent_id):
                        _remember_reply_anchor(bridge._reply_anchor_ids, message_id)
                        seen_ids.add(message_id)
                        bridge.log("  -> skip self-authored, remembered as reply anchor")
                        if reconnect_after_event:
                            bridge.log("SSE reconnecting to refresh JWT")
                            break
                        continue
                    if not _should_respond(
                        data,
                        bridge.agent_name,
                        bridge.agent_id,
                        reply_anchor_ids=bridge._reply_anchor_ids,
                    ):
                        _append_recent_event(recent_events, data)
                        bridge.log(f"  -> skip: not for @{bridge.agent_name}")
                        if reconnect_after_event:
                            bridge.log("SSE reconnecting to refresh JWT")
                            break
                        continue
                    bridge.log("  -> MATCH! delivering")

                    prompt = _strip_mention(data.get("content", ""), bridge.agent_name)
                    if not prompt:
                        if reconnect_after_event:
                            bridge.log("SSE reconnecting to refresh JWT")
                            break
                        continue

                    seen_ids.add(message_id)
                    if len(seen_ids) > SEEN_MAX:
                        seen_ids = set(list(seen_ids)[-SEEN_MAX // 2 :])
                    _remember_reply_anchor(bridge._reply_anchor_ids, message_id)
                    backlog = recent_events[-bridge.backlog_limit :] if bridge.backlog_limit > 0 else []

                    author_raw = data.get("author")
                    if isinstance(author_raw, dict):
                        author = author_raw.get("name", "unknown")
                    else:
                        author = (
                            data.get("display_name")
                            or data.get("username")
                            or data.get("sender_name")
                            or (author_raw if isinstance(author_raw, str) else "unknown")
                        )

                    # Extract attachment metadata.  SSE events often omit
                    # the full metadata.attachments that the REST API returns,
                    # so we first check the SSE payload and fall back to a
                    # lightweight GET /messages/{id} call when needed.
                    attachments = None
                    msg_metadata = data.get("metadata") or {}
                    if isinstance(msg_metadata, dict):
                        raw_attachments = msg_metadata.get("attachments") or msg_metadata.get("accepted_attachments")
                        if raw_attachments and isinstance(raw_attachments, list):
                            attachments = raw_attachments
                    if not attachments:
                        raw_top = data.get("attachments")
                        if raw_top and isinstance(raw_top, list):
                            attachments = raw_top
                    # Fallback: fetch full message from REST API to get attachments
                    if not attachments and message_id:
                        try:
                            full_msg = bridge.client.get_message(message_id)
                            if isinstance(full_msg, dict):
                                full_msg = full_msg.get("message", full_msg)
                            full_meta = (full_msg or {}).get("metadata") or {}
                            api_attachments = full_meta.get("attachments") or full_meta.get("accepted_attachments")
                            if api_attachments and isinstance(api_attachments, list):
                                attachments = api_attachments
                                bridge.log(f"  fetched {len(attachments)} attachment(s) from REST API")
                        except Exception as exc:
                            bridge.log(f"  attachment fetch failed: {exc}")

                    bridge.enqueue_from_thread(
                        MentionEvent(
                            message_id=message_id,
                            parent_id=data.get("parent_id"),
                            conversation_id=data.get("conversation_id"),
                            author=author,
                            prompt=prompt,
                            raw_content=data.get("content", ""),
                            created_at=data.get("created_at"),
                            space_id=bridge.space_id,
                            attachments=attachments,
                            backlog=backlog,
                        )
                    )
                    _append_recent_event(recent_events, data)
                    if reconnect_after_event:
                        bridge.log("SSE reconnecting to refresh JWT")
                        break
        except (httpx.ConnectError, httpx.ReadTimeout, ConnectionError) as exc:
            bridge.log(f"SSE reconnect in {backoff}s after: {exc}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as exc:  # pragma: no cover - live path
            bridge.log(f"unexpected SSE error: {exc}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


@app.callback(invoke_without_command=True)
def channel(
    agent: Optional[str] = typer.Option(None, "--agent", "-a", help="Agent name to listen as (default: from config)"),
    space_id: Optional[str] = typer.Option(None, "--space-id", "-s", help="Space to bridge (default: from config)"),
    queue_size: int = typer.Option(50, "--queue-size", help="Max queued mentions before dropping"),
    backlog_limit: int = typer.Option(
        20,
        "--backlog-limit",
        help="Max recent non-matching messages to attach to a wake mention (0 disables backlog packets).",
    ),
    backlog_in_content: bool = typer.Option(
        True,
        "--backlog-in-content/--no-backlog-in-content",
        help="Prepend the capped backlog to delivered channel content so the agent sees pre-mention context.",
    ),
    pre_exit_drain: bool = typer.Option(
        True,
        "--pre-exit-drain/--no-pre-exit-drain",
        help="Pause reply if newer pending mentions arrived before final send; pass reply force=true to override.",
    ),
    debug: bool = typer.Option(False, "--debug", help="Log bridge activity to stderr"),
    processing_status: bool = typer.Option(
        True,
        "--processing-status/--no-processing-status",
        help="Publish agent_processing events when messages are delivered and replies complete.",
    ),
):
    """Run an MCP stdio server that bridges aX mentions into Claude Code."""
    _load_channel_env()
    client = get_client()
    agent_name = agent or resolve_agent_name(client=client)
    if not agent_name:
        console.print(
            "[red]Error: No agent name.[/red] Use --agent or set agent_name in .ax/config.toml or AX_AGENT_NAME."
        )
        raise typer.Exit(1)

    sid = resolve_space_id(client, explicit=space_id)
    agent_id = client.agent_id or _resolve_agent_id(client, agent_name)
    if agent_id and not client.agent_id:
        client.agent_id = agent_id
    bridge = ChannelBridge(
        client=client,
        agent_name=agent_name,
        agent_id=agent_id,
        space_id=sid,
        queue_size=queue_size,
        debug=debug,
        processing_status=processing_status,
        pre_exit_drain=pre_exit_drain,
        backlog_limit=backlog_limit,
        backlog_in_content=backlog_in_content,
    )

    listener = threading.Thread(target=_sse_loop, args=(bridge,), daemon=True)
    listener.start()
    try:
        asyncio.run(bridge.serve_stdio())
    except KeyboardInterrupt:
        bridge.shutdown.set()
    finally:
        bridge.shutdown.set()
        listener.join(timeout=5)
