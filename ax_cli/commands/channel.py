"""ax channel — Claude Code channel bridge over MCP stdio.

Reuses the ax listen SSE/auth/@mention plumbing, but exposes it as a thin
MCP server so Claude Code can receive messages and reply in-thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
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
from .listen import _is_self_authored, _iter_sse, _remember_reply_anchor, _should_respond, _strip_mention

app = typer.Typer(name="channel", help="Run an aX Claude Code channel over MCP stdio", no_args_is_help=False)

PROTOCOL_VERSION = "2025-11-25"
SERVER_NAME = "ax-channel"
SERVER_VERSION = "0.1.0"
SEEN_MAX = 500
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
    metadata: dict[str, Any] | None = None


def _string_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        return text or None
    return None


def _format_shared_object(metadata: dict[str, Any] | None, *, space_id: str) -> str | None:
    if not isinstance(metadata, dict):
        return None
    forward = metadata.get("forward")
    if not isinstance(forward, dict):
        return None

    fields = [
        ("resource_type", "resource_type"),
        ("resource_id", "resource_id"),
        ("task_id", "task_id"),
        ("context_key", "context_key"),
        ("resource_uri", "resource_uri"),
        ("source_message_id", "source_message_id"),
        ("source_card_id", "source_card_id"),
        ("title", "title"),
    ]
    lines = ["Shared object:"]
    for label, key in fields:
        value = _string_value(forward.get(key))
        if value:
            lines.append(f"- {label}: {value}")

    summary = _string_value(forward.get("summary"))
    if summary:
        lines.append(f"- summary: {summary}")

    task_id = _string_value(forward.get("task_id"))
    context_key = _string_value(forward.get("context_key"))
    if task_id or context_key:
        lines.append("")
        lines.append("Suggested inspection:")
        if task_id:
            lines.append(f"- axctl tasks get {task_id} --space-id {space_id} --json")
        if context_key:
            lines.append(f"- axctl context get '{context_key}' --space-id {space_id} --json")

    return "\n".join(lines) if len(lines) > 1 else None


def _format_attachments(attachments: list[dict[str, Any]] | None, *, space_id: str) -> str | None:
    if not attachments:
        return None
    lines = ["Attachments:"]
    context_keys: list[str] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        filename = _string_value(attachment.get("filename")) or "attachment"
        content_type = _string_value(attachment.get("content_type"))
        attachment_id = _string_value(attachment.get("id") or attachment.get("attachment_id"))
        context_key = _string_value(attachment.get("context_key") or attachment.get("key"))
        details = [part for part in (content_type, f"id={attachment_id}" if attachment_id else None) if part]
        if context_key:
            details.append(f"context_key={context_key}")
            context_keys.append(context_key)
        lines.append(f"- {filename}" + (f" ({', '.join(details)})" if details else ""))
    if context_keys:
        lines.append("")
        lines.append("Suggested attachment inspection:")
        for key in context_keys:
            lines.append(f"- axctl context get '{key}' --space-id {space_id} --json")
    return "\n".join(lines) if len(lines) > 1 else None


def _enrich_prompt_for_agent(
    prompt: str,
    *,
    metadata: dict[str, Any] | None,
    attachments: list[dict[str, Any]] | None,
    space_id: str,
) -> str:
    blocks = [
        block
        for block in (
            _format_shared_object(metadata, space_id=space_id),
            _format_attachments(attachments, space_id=space_id),
        )
        if block
    ]
    if not blocks:
        return prompt
    return prompt.rstrip() + "\n\n---\n" + "\n\n".join(blocks)


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
    ) -> None:
        self.client = client
        self.agent_name = agent_name
        self.agent_id = agent_id
        self.space_id = space_id
        self.debug = debug
        self.processing_status = processing_status
        self.loop: asyncio.AbstractEventLoop | None = None
        self.mention_queue: asyncio.Queue[MentionEvent] = asyncio.Queue(maxsize=queue_size)
        self.initialized = asyncio.Event()
        self.shutdown = threading.Event()
        self._stderr_lock = threading.Lock()
        self._write_lock = asyncio.Lock()
        self._last_message_id: str | None = None
        self._reply_anchor_ids: set[str] = set()
        self._pending_mentions: list[MentionEvent] = []

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
                if isinstance(event.metadata, dict):
                    forward = event.metadata.get("forward")
                    if isinstance(forward, dict):
                        meta["forward"] = forward
                await self.send_notification(
                    "notifications/claude/channel",
                    {
                        "content": event.prompt,
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
                [
                    {
                        "message_id": event.message_id,
                        "author": event.author,
                        "content": event.prompt,
                        "parent_id": event.parent_id,
                        "ts": event.created_at,
                        "attachments": event.attachments or [],
                        "metadata": event.metadata or {},
                    }
                    for event in pending
                ],
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

        try:

            def _send_as_agent():
                """Send using the agent_access JWT produced by the agent-bound PAT."""
                return self.client.send_message(self.space_id, text, parent_id=reply_to)

            data = await asyncio.to_thread(_send_as_agent)
            message = data.get("message", data)
            sent_id = message.get("id") or data.get("id")
            _remember_reply_anchor(self._reply_anchor_ids, sent_id)
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


def _sse_loop(bridge: ChannelBridge) -> None:
    seen_ids: set[str] = set()
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
                    if event_type not in {"message", "mention"} or not isinstance(data, dict):
                        bridge.log(f"skip non-msg: {event_type}")
                        if reconnect_after_event:
                            bridge.log("SSE reconnecting to refresh JWT")
                            break
                        continue

                    message_id = data.get("id") or ""
                    content_preview = (data.get("content") or "")[:60]
                    bridge.log(f"event {event_type} id={message_id[:12]} content={content_preview!r}")
                    if not message_id or message_id in seen_ids:
                        bridge.log("  -> skip: dup or no id")
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

                    # Extract share + attachment metadata.  SSE events often
                    # omit the full metadata.attachments that the REST API
                    # returns, so we first check the SSE payload and fall back
                    # to a lightweight GET /messages/{id} call when needed.
                    attachments = None
                    msg_metadata = data.get("metadata") or {}
                    if not isinstance(msg_metadata, dict):
                        msg_metadata = {}
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
                            if not isinstance(full_meta, dict):
                                full_meta = {}
                            merged_meta = dict(full_meta)
                            merged_meta.update(msg_metadata)
                            msg_metadata = merged_meta
                            api_attachments = full_meta.get("attachments") or full_meta.get("accepted_attachments")
                            if api_attachments and isinstance(api_attachments, list):
                                attachments = api_attachments
                                bridge.log(f"  fetched {len(attachments)} attachment(s) from REST API")
                        except Exception as exc:
                            bridge.log(f"  attachment fetch failed: {exc}")
                    prompt = _enrich_prompt_for_agent(
                        prompt,
                        metadata=msg_metadata,
                        attachments=attachments,
                        space_id=bridge.space_id,
                    )

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
                            metadata=msg_metadata,
                        )
                    )
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
