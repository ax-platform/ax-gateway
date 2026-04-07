"""ax channel — Claude Code channel bridge over MCP stdio.

Reuses the ax listen SSE/auth/@mention plumbing, but exposes it as a thin
MCP server so Claude Code can receive messages and reply in-thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import typer

from ..config import get_client, resolve_agent_name, resolve_space_id
from ..output import console
from .listen import _iter_sse, _should_respond, _strip_mention

app = typer.Typer(name="channel", help="Run an aX Claude Code channel over MCP stdio", no_args_is_help=False)

PROTOCOL_VERSION = "2025-11-25"
SERVER_NAME = "ax-channel"
SERVER_VERSION = "0.1.0"
SEEN_MAX = 500


@dataclass(slots=True)
class MentionEvent:
    message_id: str
    parent_id: str | None
    author: str
    prompt: str
    raw_content: str
    created_at: str | None
    space_id: str


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
    ) -> None:
        self.client = client
        self.agent_name = agent_name
        self.agent_id = agent_id
        self.space_id = space_id
        self.debug = debug
        self.loop: asyncio.AbstractEventLoop | None = None
        self.mention_queue: asyncio.Queue[MentionEvent] = asyncio.Queue(maxsize=queue_size)
        self.initialized = asyncio.Event()
        self.shutdown = threading.Event()
        self._stderr_lock = threading.Lock()
        self._write_lock = asyncio.Lock()
        self._last_message_id: str | None = None

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
                await self.send_notification(
                    "notifications/claude/channel",
                    {
                        "content": event.prompt,
                        "meta": {
                            "chat_id": event.space_id,
                            "message_id": event.message_id,
                            "parent_id": event.parent_id,
                            "user": event.author,
                            "sender": event.author,
                            "source": "ax",
                            "space_id": event.space_id,
                            "ts": event.created_at,
                            "raw_content": event.raw_content,
                        },
                    },
                )
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
                    }
                ]
            },
        )

    async def handle_tool_call(self, request_id: Any, params: dict[str, Any]) -> None:
        name = params.get("name")
        arguments = params.get("arguments") or {}
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

        try:

            def _send_as_agent():
                """Send message as agent, adding X-Agent-Id header for user tokens."""
                body = {
                    "content": text,
                    "space_id": self.space_id,
                    "channel": "main",
                    "message_type": "text",
                    "parent_id": reply_to,
                }
                # Build auth headers, then add agent identity
                headers = self.client._auth_headers()
                if self.agent_id:
                    headers["X-Agent-Id"] = self.agent_id
                r = self.client._http.post("/api/v1/messages", json=body, headers=headers)
                r.raise_for_status()
                return self.client._parse_json(r)

            data = await asyncio.to_thread(_send_as_agent)
            message = data.get("message", data)
            sent_id = message.get("id") or data.get("id")
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
            with bridge.client.connect_sse() as response:
                if response.status_code != 200:
                    raise ConnectionError(f"SSE failed: {response.status_code}")
                backoff = 1
                bridge.log(f"SSE connected (status {response.status_code})")
                for event_type, data in _iter_sse(response):
                    if bridge.shutdown.is_set():
                        return
                    # Reconnect before JWT expires (15-min TTL, reconnect at 10 min)
                    if time.monotonic() - connect_time > _SSE_RECONNECT_INTERVAL:
                        bridge.log("SSE reconnecting to refresh JWT")
                        break
                    if event_type in {"bootstrap", "heartbeat", "ping", "connected", "identity_bootstrap"}:
                        bridge.log(f"skip {event_type}")
                        continue
                    if event_type not in {"message", "mention"} or not isinstance(data, dict):
                        bridge.log(f"skip non-msg: {event_type}")
                        continue

                    message_id = data.get("id") or ""
                    content_preview = (data.get("content") or "")[:60]
                    bridge.log(f"event {event_type} id={message_id[:12]} content={content_preview!r}")
                    if not message_id or message_id in seen_ids:
                        bridge.log("  -> skip: dup or no id")
                        continue
                    if not _should_respond(data, bridge.agent_name, bridge.agent_id):
                        bridge.log(f"  -> skip: not for @{bridge.agent_name}")
                        continue
                    bridge.log("  -> MATCH! delivering")

                    prompt = _strip_mention(data.get("content", ""), bridge.agent_name)
                    if not prompt:
                        continue

                    seen_ids.add(message_id)
                    if len(seen_ids) > SEEN_MAX:
                        seen_ids = set(list(seen_ids)[-SEEN_MAX // 2 :])

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
                    bridge.enqueue_from_thread(
                        MentionEvent(
                            message_id=message_id,
                            parent_id=data.get("parent_id"),
                            author=author,
                            prompt=prompt,
                            raw_content=data.get("content", ""),
                            created_at=data.get("created_at"),
                            space_id=bridge.space_id,
                        )
                    )
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
):
    """Run an MCP stdio server that bridges aX mentions into Claude Code."""
    client = get_client()
    agent_name = agent or resolve_agent_name(client=client)
    if not agent_name:
        console.print(
            "[red]Error: No agent name.[/red] Use --agent or set agent_name in .ax/config.toml or AX_AGENT_NAME."
        )
        raise typer.Exit(1)

    sid = resolve_space_id(client, explicit=space_id)
    agent_id = _resolve_agent_id(client, agent_name)
    bridge = ChannelBridge(
        client=client,
        agent_name=agent_name,
        agent_id=agent_id,
        space_id=sid,
        queue_size=queue_size,
        debug=debug,
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
