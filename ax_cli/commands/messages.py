"""ax messages — send, list, get, edit, delete, search."""

import json
import queue
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
import typer

from ..config import get_client, resolve_agent_name, resolve_gateway_config, resolve_space_id
from ..context_keys import build_upload_context_key
from ..output import JSON_OPTION, console, handle_error, print_json, print_kv, print_table
from .gateway import _approval_required_guidance, _local_process_fingerprint
from .watch import _iter_sse

app = typer.Typer(name="messages", help="Message operations", no_args_is_help=True)


def _gateway_local_connect(
    *,
    gateway_url: str,
    agent_name: str | None,
    registry_ref: str | None,
    workdir: str | None,
    space_id: str | None,
) -> dict:
    display_name = str(agent_name or registry_ref or "").strip()
    if not display_name:
        raise typer.BadParameter("Gateway config requires [agent].agent_name or [agent].registry_ref.")
    body: dict = {"fingerprint": _local_process_fingerprint(agent_name=display_name, cwd=workdir)}
    if agent_name:
        body["agent_name"] = agent_name
    if registry_ref:
        body["registry_ref"] = registry_ref
    if space_id:
        body["space_id"] = space_id
    try:
        response = httpx.post(f"{gateway_url.rstrip('/')}/local/connect", json=body, timeout=10.0)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        try:
            detail = exc.response.json().get("error", detail)
        except Exception:
            pass
        raise typer.BadParameter(f"Gateway local connect failed: {detail}") from exc
    except Exception as exc:
        raise typer.BadParameter(f"Gateway local connect failed: {exc}") from exc


def _gateway_local_send(
    *,
    gateway_cfg: dict,
    content: str,
    space_id: str | None,
    parent_id: str | None,
) -> dict:
    gateway_url = str(gateway_cfg.get("url") or "http://127.0.0.1:8765")
    connect_payload = _gateway_local_connect(
        gateway_url=gateway_url,
        agent_name=gateway_cfg.get("agent_name"),
        registry_ref=gateway_cfg.get("registry_ref"),
        workdir=gateway_cfg.get("workdir"),
        space_id=space_id,
    )
    session_token = str(connect_payload.get("session_token") or "").strip()
    if not session_token:
        status = str(connect_payload.get("status") or "pending")
        if status == "pending":
            raise typer.BadParameter(
                _approval_required_guidance(
                    connect_payload=connect_payload,
                    gateway_url=gateway_url,
                    agent_name=gateway_cfg.get("agent_name"),
                    workdir=gateway_cfg.get("workdir"),
                    action="send this message",
                )
            )
        raise typer.BadParameter(f"Gateway local session is {status}; approve the agent before sending.")
    body = {"content": content, "space_id": space_id, "parent_id": parent_id}
    try:
        response = httpx.post(
            f"{gateway_url.rstrip('/')}/local/send",
            json=body,
            headers={"X-Gateway-Session": session_token},
            timeout=20.0,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        try:
            detail = exc.response.json().get("error", detail)
        except Exception:
            pass
        raise typer.BadParameter(f"Gateway local send failed: {detail}") from exc
    except Exception as exc:
        raise typer.BadParameter(f"Gateway local send failed: {exc}") from exc
    payload["connect"] = {
        "status": connect_payload.get("status"),
        "registry_ref": connect_payload.get("registry_ref"),
        "agent": (connect_payload.get("agent") or {}).get("name")
        if isinstance(connect_payload.get("agent"), dict)
        else None,
    }
    return payload


def _print_wait_status(remaining: int, last_remaining: int | None, wait_label: str = "reply") -> int:
    if remaining != last_remaining:
        console.print(f"  [dim]waiting for {wait_label}... ({remaining}s remaining)[/dim]", end="\r")
    return remaining


def _processing_status_from_event(message_id: str, event_type: str | None, data: object) -> dict | None:
    """Return an agent_processing event for this message, if one was emitted."""
    if event_type != "agent_processing" or not isinstance(data, dict):
        return None
    event_message_id = str(data.get("message_id") or data.get("source_message_id") or "")
    if event_message_id != message_id:
        return None
    status = str(data.get("status") or "").strip()
    if not status:
        return None
    event = {
        "message_id": event_message_id,
        "status": status,
        "agent_id": data.get("agent_id"),
        "agent_name": data.get("agent_name"),
    }
    for field in (
        "activity",
        "tool_name",
        "progress",
        "detail",
        "reason",
        "error_message",
        "retry_after_seconds",
        "parent_message_id",
    ):
        if data.get(field) is not None:
            event[field] = data.get(field)
    return event


def _processing_status_text(status_event: dict, *, wait_label: str = "reply") -> str:
    """Render tooling-side delivery/progress in human-friendly language."""
    status = str(status_event.get("status") or "").strip().lower()
    agent_name = str(status_event.get("agent_name") or wait_label).strip().lstrip("@")
    target = f"@{agent_name}" if agent_name else wait_label
    activity = str(status_event.get("activity") or "").strip()
    tool_name = str(status_event.get("tool_name") or "").strip()

    if status == "accepted":
        base = f"tooling: {target} acknowledged the message"
    elif status in {"started", "claimed", "forwarded"}:
        base = f"tooling: {target} picked up the message"
    elif status in {"queued", "queued locally"}:
        base = f"tooling: {target} queued the message"
    elif status in {"working", "processing"}:
        base = f"tooling: {target} is working"
    elif status == "thinking":
        base = f"tooling: {target} is thinking"
    elif status in {"tool_use", "tool_call"}:
        base = f"tooling: {target} is using tools"
    elif status == "tool_complete":
        base = f"tooling: {target} finished a tool step"
    elif status == "streaming":
        base = f"tooling: {target} is streaming a reply"
    elif status == "completed":
        base = f"tooling: {target} finished processing"
    elif status in {"no_reply", "declined", "skipped", "not_responding"}:
        base = f"tooling: {target} chose not to respond"
    elif status == "error":
        base = f"tooling: {target} hit an error"
    else:
        base = f"tooling: {target} status={status}"

    if activity:
        return f"{base} — {activity}"
    if tool_name:
        return f"{base} — {tool_name}"
    return base


class _ProcessingStatusWatcher:
    """Best-effort SSE watcher for delivery/working events emitted by channel runtimes."""

    def __init__(self, client, *, space_id: str, timeout: int) -> None:
        self.client = client
        self.space_id = space_id
        self.deadline = time.time() + max(1, timeout)
        self.message_id: str | None = None
        self.events: list[dict] = []
        self._queue: queue.Queue[dict] = queue.Queue()
        self._pending: list[dict] = []
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="ax-send-processing-watch", daemon=True)
        self._thread.start()

    def wait_ready(self, timeout: float = 1.5) -> bool:
        return self._ready.wait(timeout)

    def set_message_id(self, message_id: str) -> None:
        with self._lock:
            self.message_id = message_id
            queued = [event for event in self._pending if event.get("message_id") == message_id]
            self._pending = [event for event in self._pending if event.get("message_id") != message_id]
        for event in queued:
            self._queue.put(event)

    def close(self) -> None:
        self._stop.set()

    def drain(self) -> list[dict]:
        drained: list[dict] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return drained
            self.events.append(item)
            drained.append(item)

    def _accept_status_event(self, status_event: dict) -> None:
        with self._lock:
            message_id = self.message_id
            if not message_id:
                self._pending.append(status_event)
                if len(self._pending) > 100:
                    self._pending = self._pending[-100:]
                return
        if status_event.get("message_id") == message_id:
            self._queue.put(status_event)

    def _run(self) -> None:
        while not self._stop.is_set() and time.time() < self.deadline:
            try:
                timeout = httpx.Timeout(connect=5, read=1, write=5, pool=5)
                with self.client.connect_sse(space_id=self.space_id, timeout=timeout) as response:
                    self._ready.set()
                    if response.status_code != 200:
                        return
                    for event_type, data in _iter_sse(response):
                        if self._stop.is_set() or time.time() >= self.deadline:
                            return
                        if event_type != "agent_processing" or not isinstance(data, dict):
                            continue
                        event_message_id = str(data.get("message_id") or data.get("source_message_id") or "")
                        if not event_message_id:
                            continue
                        status = _processing_status_from_event(event_message_id, event_type, data)
                        if status:
                            self._accept_status_event(status)
            except httpx.ReadTimeout:
                continue
            except (httpx.HTTPError, RuntimeError, AttributeError):
                self._ready.set()
                return


def _matching_reply(message_id: str, payload, seen_ids: set[str]) -> tuple[dict | None, bool]:
    routing_announced = False

    for reply in payload:
        rid = reply.get("id", "")
        if not rid:
            continue

        matches_thread = reply.get("parent_id") == message_id or reply.get("conversation_id") == message_id
        if not matches_thread:
            continue

        if rid in seen_ids:
            continue
        seen_ids.add(rid)

        metadata = reply.get("metadata", {}) or {}
        routing = metadata.get("routing", {})
        if routing.get("mode") == "ax_relay":
            target = routing.get("target_agent_name", "specialist")
            console.print(" " * 60, end="\r")
            console.print(f"  [cyan]aX is routing to @{target}...[/cyan]")
            routing_announced = True
            continue

        console.print(" " * 60, end="\r")
        return reply, routing_announced

    return None, routing_announced


def _wait_for_reply_polling(
    client,
    message_id: str,
    *,
    deadline: float,
    seen_ids: set[str],
    wait_label: str = "reply",
    poll_interval: float = 2.0,
    processing_watcher: _ProcessingStatusWatcher | None = None,
) -> dict | None:
    """Poll for a reply as a fallback when SSE is unavailable."""
    last_remaining = None
    announced_processing: set[tuple[str | None, str, str, str]] = set()

    while time.time() < deadline:
        remaining = int(deadline - time.time())
        last_remaining = _print_wait_status(remaining, last_remaining, wait_label)
        if processing_watcher:
            for status_event in processing_watcher.drain():
                status = str(status_event.get("status") or "")
                agent_name = status_event.get("agent_name") or wait_label
                key = (
                    status_event.get("agent_id"),
                    status,
                    str(status_event.get("activity") or ""),
                    str(status_event.get("tool_name") or ""),
                )
                if status and key not in announced_processing:
                    console.print(" " * 60, end="\r")
                    console.print(f"  [cyan]{_processing_status_text(status_event, wait_label=agent_name)}[/cyan]")
                    announced_processing.add(key)

        try:
            data = client.list_replies(message_id)
        except (httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadError):
            time.sleep(poll_interval)
            continue

        replies = data if isinstance(data, list) else data.get("messages", data.get("replies", []))
        reply, _ = _matching_reply(message_id, replies, seen_ids)
        if reply:
            return reply

        time.sleep(poll_interval)

    console.print(" " * 60, end="\r")
    return None


def _wait_for_reply(
    client,
    message_id: str,
    timeout: int = 60,
    wait_label: str = "reply",
    *,
    processing_watcher: _ProcessingStatusWatcher | None = None,
) -> dict | None:
    """Wait for a reply by polling list_replies."""
    deadline = time.time() + timeout
    seen_ids: set[str] = {message_id}

    return _wait_for_reply_polling(
        client,
        message_id,
        deadline=deadline,
        seen_ids=seen_ids,
        wait_label=wait_label,
        poll_interval=1.0,
        processing_watcher=processing_watcher,
    )


def _message_items(data) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("messages", [])
    return []


def _resolve_message_id(client, message_id: str, *, space_id: str | None = None) -> str:
    """Resolve table-friendly short message IDs against recent messages."""
    candidate = message_id.strip()
    if not candidate or "-" in candidate or len(candidate) >= 32:
        return candidate

    sid = space_id or resolve_space_id(client)
    data = client.list_messages(limit=100, space_id=sid)
    matches = [
        str(message.get("id") or "")
        for message in _message_items(data)
        if str(message.get("id") or "").startswith(candidate)
    ]
    matches = [match for match in matches if match]

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        typer.echo(
            f"Error: message ID prefix '{candidate}' is ambiguous. Use the full ID from --json.",
            err=True,
        )
        raise typer.Exit(1)
    return candidate


def _target_mention(to: str) -> str:
    return to if to.startswith("@") else f"@{to}"


def _starts_with_mention(content: str, mention: str) -> bool:
    return content.lstrip().lower().startswith(mention.lower())


def _sender_label(message: dict) -> str | None:
    display_name = str(message.get("display_name") or "").strip()
    sender_type = str(message.get("sender_type") or "").strip()
    if display_name:
        if sender_type == "agent":
            return f"@{display_name.lstrip('@')}"
        return display_name
    if sender_type:
        return sender_type
    return None


def _extract_delivery_context(data: dict | None) -> dict | None:
    """Pull AVAIL-CONTRACT-001 ``delivery_context`` out of a send response.

    Looks in three places per the spec — top-level, metadata, and nested
    message.metadata — so the CLI doesn't care which envelope wrapping the
    backend uses. Returns the dict or ``None`` if not present.
    """
    if not isinstance(data, dict):
        return None
    direct = data.get("delivery_context")
    if isinstance(direct, dict):
        return direct
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        nested = metadata.get("delivery_context")
        if isinstance(nested, dict):
            return nested
    msg = data.get("message")
    if isinstance(msg, dict):
        msg_meta = msg.get("metadata")
        if isinstance(msg_meta, dict):
            inner = msg_meta.get("delivery_context")
            if isinstance(inner, dict):
                return inner
    return None


_DELIVERY_PATH_LABEL = {
    "live_session": "delivered live",
    "warm_wake": "warming target",
    "inbox_queue": "queued",
    "blocked_unroutable": "blocked (unroutable)",
    "failed_no_route": "failed (no route)",
}

_EXPECTED_RESPONSE_LABEL = {
    "immediate": "Immediate",
    "warming": "Warming",
    "dispatch_delayed": "Dispatch",
    "queued": "Queued",
    "unlikely": "Unlikely",
    "unavailable": "Unavailable",
    "unknown": "Unknown",
}


def _delivery_context_chip(ctx: dict) -> str | None:
    """Format delivery_context as a one-line chip for human output.

    Returns ``None`` when the context is empty / has no useful fields.
    Renders disagreement signal when ``delivery_path`` doesn't match the
    ``expected_response_at_send`` prediction (per the spec's "predicted
    warming, actually live" debugging gold).
    """
    if not isinstance(ctx, dict):
        return None
    delivery_path = str(ctx.get("delivery_path") or "").strip()
    expected = str(ctx.get("expected_response_at_send") or "").strip()
    warning = str(ctx.get("warning") or "").strip()

    if not delivery_path and not expected and not warning:
        return None

    parts: list[str] = []
    if delivery_path:
        label = _DELIVERY_PATH_LABEL.get(delivery_path, delivery_path)
        parts.append(label)

    # Disagreement signal: predicted X, actually Y
    if delivery_path and expected and not _delivery_matches_expectation(delivery_path, expected):
        expected_label = _EXPECTED_RESPONSE_LABEL.get(expected, expected)
        parts.append(f"predicted {expected_label}, actually {_DELIVERY_PATH_LABEL.get(delivery_path, delivery_path)}")
    elif expected and not delivery_path:
        # No actual path yet (offline send?), show the prediction alone
        parts.append(_EXPECTED_RESPONSE_LABEL.get(expected, expected))

    if warning:
        parts.append(f"warning: {warning}")

    return " · ".join(parts) if parts else None


def _delivery_matches_expectation(delivery_path: str, expected: str) -> bool:
    """Whether the actual delivery_path agrees with expected_response_at_send.

    Mapping per AVAIL-CONTRACT-001 §"Pre-send / Post-send UX":
    - immediate ↔ live_session
    - warming ↔ warm_wake
    - dispatch_delayed ↔ warm_wake (cloud-agent dispatch is also a "warm wake" path)
    - queued ↔ inbox_queue
    - unlikely / unavailable ↔ blocked_unroutable / failed_no_route
    """
    pairs = {
        "immediate": {"live_session"},
        "warming": {"warm_wake"},
        "dispatch_delayed": {"warm_wake"},
        "queued": {"inbox_queue"},
        "unlikely": {"blocked_unroutable", "failed_no_route", "live_session"},
        "unavailable": {"blocked_unroutable", "failed_no_route"},
        "unknown": {"live_session", "warm_wake", "inbox_queue", "blocked_unroutable", "failed_no_route"},
    }
    return delivery_path in pairs.get(expected, set())


def _gateway_reply_note(message: dict) -> str | None:
    metadata = message.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("control_plane") != "gateway":
        return None
    gateway = metadata.get("gateway")
    if not isinstance(gateway, dict):
        return None

    parts = ["via Gateway"]
    gateway_id = str(gateway.get("gateway_id") or "").strip()
    if gateway_id:
        parts[0] = f"{parts[0]} {gateway_id[:8]}"

    agent_name = str(gateway.get("agent_name") or "").strip()
    if agent_name:
        parts.append(f"agent=@{agent_name.lstrip('@')}")

    runtime_type = str(gateway.get("runtime_type") or "").strip()
    if runtime_type:
        parts.append(f"runtime={runtime_type}")

    transport = str(gateway.get("transport") or "").strip()
    if transport:
        parts.append(f"transport={transport}")

    return " · ".join(parts)


def _attachment_ref(
    *,
    attachment_id: str,
    content_type: str,
    filename: str,
    size: int,
    url: str,
    context_key: str | None,
) -> dict:
    ref = {
        "id": attachment_id,
        "filename": filename,
        "content_type": content_type,
        "size": size,
        "size_bytes": size,
        "url": url,
        "kind": "file",
    }
    if context_key:
        ref["context_key"] = context_key
    return ref


def _context_upload_value(
    *,
    attachment_id: str,
    context_key: str,
    filename: str,
    content_type: str,
    size: int,
    url: str,
    local_path: Path,
) -> dict:
    value = {
        "type": "file_upload",
        "attachment_id": attachment_id,
        "context_key": context_key,
        "filename": filename,
        "content_type": content_type,
        "size": size,
        "url": url,
        "source": "message_attachment",
    }

    if size <= 50_000 and (
        content_type.startswith("text/") or content_type in {"application/json", "application/xml", "application/yaml"}
    ):
        try:
            value["content"] = local_path.read_text(errors="replace")
        except Exception:
            pass

    return value


@app.command("send")
def send(
    content: str = typer.Argument(..., help="Message content"),
    wait: bool = typer.Option(
        True,
        "--wait/--no-wait",
        "-w",
        help="Wait for a reply after sending. Use --no-wait for intentional notify-only sends.",
    ),
    skip_ax: bool = typer.Option(False, "--skip-ax", help="Deprecated alias for --no-wait.", hidden=True),
    timeout: int = typer.Option(60, "--timeout", "-t", help="Max seconds to wait for reply"),
    to: Optional[str] = typer.Option(
        None, "--to", help="@mention another agent by name (prepends @name to your message)"
    ),
    ask_ax: bool = typer.Option(False, "--ask-ax", help="Route this message to aX by prepending @aX"),
    act_as: Optional[str] = typer.Option(
        None, "--act-as", help="Impersonate: send as a different agent identity. Requires a token scoped to that agent."
    ),
    files: Optional[list[str]] = typer.Option(
        None,
        "--file",
        "-f",
        help="Attach a local file to this message; creates a transcript preview backed by context metadata (repeatable)",
    ),
    channel: str = typer.Option("main", "--channel", help="Channel name"),
    parent: Optional[str] = typer.Option(None, "--parent", "--reply-to", "-r", help="Parent message ID (thread reply)"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    as_json: bool = JSON_OPTION,
):
    """Send a message and wait for a reply by default.

    Use --to to get an agent's attention by mention. Use --no-wait to send only.
    For delegated agent work that needs ownership and a reply, use `ax handoff`
    instead; it creates/tracks the task, sends the message, watches for the
    agent response, and returns structured evidence.

    Attach files with --file when the primary intent is a chat message with a
    polished transcript preview. The attachment metadata includes the context
    key so agents can load the file later:
        ax send "here's the diagram" --file ./arch.png
        ax send "two files" -f report.md -f data.csv
    """
    if skip_ax:
        wait = False
    if ask_ax and to:
        typer.echo("Error: use either --ask-ax or --to, not both.", err=True)
        raise typer.Exit(1)

    final_content = content
    if ask_ax:
        mention = _target_mention("aX")
        if not _starts_with_mention(content, mention):
            final_content = f"{mention} {content}"
    elif to:
        mention = _target_mention(to)
        if not _starts_with_mention(content, mention):
            final_content = f"{mention} {content}"

    gateway_cfg = resolve_gateway_config()
    if gateway_cfg:
        if act_as:
            typer.echo("Error: --act-as is not supported with Gateway-native local identity.", err=True)
            raise typer.Exit(1)
        if files:
            typer.echo("Error: --file is not supported with Gateway-native local identity yet.", err=True)
            raise typer.Exit(1)
        if channel != "main":
            typer.echo("Error: custom --channel is not supported with Gateway-native local identity yet.", err=True)
            raise typer.Exit(1)
        data = _gateway_local_send(
            gateway_cfg=gateway_cfg,
            content=final_content,
            space_id=space_id,
            parent_id=parent,
        )
        msg = data.get("message", data)
        msg_id = msg.get("id") or msg.get("message_id") or data.get("id")
        if as_json:
            print_json(data)
        else:
            sent_line = f"[green]Sent through Gateway.[/green] id={msg_id}"
            sender = _sender_label(msg)
            if sender:
                sent_line += f" as {sender}"
            console.print(sent_line)
            if wait:
                console.print("[dim]Gateway-native send accepted; reply waiting for this path is not wired yet.[/dim]")
        return

    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)

    # --act-as: override sender identity (requires scoped token)
    if act_as:
        # Validate scope before sending — fail fast with a clear message
        try:
            me = client.whoami()
            scope = me.get("credential_scope", {})
            agent_scope = scope.get("agent_scope", "all")
            allowed_ids = scope.get("allowed_agent_ids", [])

            if agent_scope == "user":
                typer.echo(
                    "Error: --act-as rejected. Your token has agent_scope='user' — it cannot send as any agent.",
                    err=True,
                )
                raise typer.Exit(1)

            if agent_scope == "agents" and allowed_ids:
                # Resolve the agent name to an ID to check scope
                try:
                    agents_data = client.list_agents()
                    agents = agents_data if isinstance(agents_data, list) else agents_data.get("agents", [])
                    match = next((a for a in agents if a.get("name") == act_as), None)
                    if match and str(match.get("id")) not in allowed_ids:
                        allowed_names = []
                        for a in agents:
                            if str(a.get("id")) in allowed_ids:
                                allowed_names.append(a.get("name", str(a.get("id"))))
                        typer.echo(
                            f"Error: --act-as '{act_as}' rejected. "
                            f"Your token is only scoped to: {', '.join(allowed_names)}",
                            err=True,
                        )
                        raise typer.Exit(1)
                except httpx.HTTPStatusError:
                    pass  # Let the server enforce if we can't check client-side
        except httpx.HTTPStatusError:
            pass  # Let the server enforce

        client._base_headers["X-Agent-Name"] = act_as
    else:
        # Default: resolve agent from env/config (normal identity)
        resolved_agent = resolve_agent_name(client=client)
        if resolved_agent:
            client._base_headers["X-Agent-Name"] = resolved_agent

    # --file: upload files and collect attachment metadata
    attachments = []
    for file_path in files or []:
        local_path = Path(file_path).expanduser().resolve()
        try:
            upload_data = client.upload_file(str(local_path), space_id=sid)
        except FileNotFoundError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        except httpx.HTTPStatusError as exc:
            handle_error(exc)
        # Normalize upload response into attachment reference
        raw_attachment = upload_data.get("attachment", upload_data)
        attachment_id = (
            raw_attachment.get("id")
            or raw_attachment.get("attachment_id")
            or raw_attachment.get("file_id")
            or upload_data.get("id")
            or ""
        )
        filename = (
            raw_attachment.get("original_filename")
            or raw_attachment.get("filename")
            or raw_attachment.get("name")
            or local_path.name
        )
        content_type = raw_attachment.get("content_type") or "application/octet-stream"
        size = int(raw_attachment.get("size_bytes") or raw_attachment.get("size") or 0)
        url = raw_attachment.get("url") or ""
        context_key = build_upload_context_key(filename, attachment_id)

        try:
            client.set_context(
                sid,
                context_key,
                json.dumps(
                    _context_upload_value(
                        attachment_id=attachment_id,
                        context_key=context_key,
                        filename=filename,
                        content_type=content_type,
                        size=size,
                        url=url,
                        local_path=local_path,
                    )
                ),
            )
        except httpx.HTTPStatusError:
            context_key = None
            console.print(f"  [yellow]Warning: uploaded {filename}, but context storage failed[/yellow]")

        attachments.append(
            _attachment_ref(
                attachment_id=attachment_id,
                filename=filename,
                content_type=content_type,
                size=size,
                url=url,
                context_key=context_key,
            )
        )
        console.print(f"  [dim]Uploaded: {attachments[-1]['filename']}[/dim]")

    processing_watcher = None
    if wait and to:
        processing_watcher = _ProcessingStatusWatcher(client, space_id=sid, timeout=timeout + 5)
        processing_watcher.start()
        processing_watcher.wait_ready()

    try:
        parent_id = _resolve_message_id(client, parent, space_id=sid) if parent else None
        data = client.send_message(
            sid,
            final_content,
            channel=channel,
            parent_id=parent_id,
            attachments=attachments or None,
        )
    except httpx.HTTPStatusError as e:
        handle_error(e)

    msg = data.get("message", data)
    msg_id = msg.get("id") or msg.get("message_id") or data.get("id")
    if processing_watcher and msg_id:
        processing_watcher.set_message_id(str(msg_id))

    delivery_chip = _delivery_context_chip(_extract_delivery_context(data) or {})

    if not wait or not msg_id:
        if processing_watcher:
            processing_watcher.close()
        if as_json:
            print_json(data)
        else:
            sent_line = f"[green]Sent.[/green] id={msg_id}"
            sender = _sender_label(msg)
            if sender:
                sent_line += f" as {sender}"
            console.print(sent_line)
            if delivery_chip:
                console.print(f"[dim]{delivery_chip}[/dim]")
        return

    sent_line = f"[green]Sent.[/green] id={msg_id}"
    sender = _sender_label(msg)
    if sender:
        sent_line += f" as {sender}"
    console.print(sent_line)
    if delivery_chip:
        console.print(f"[dim]{delivery_chip}[/dim]")
    wait_label = _target_mention("aX") if ask_ax else (_target_mention(to) if to else "reply")
    reply = _wait_for_reply(
        client,
        msg_id,
        timeout=timeout,
        wait_label=wait_label,
        processing_watcher=processing_watcher,
    )
    processing_statuses = processing_watcher.events if processing_watcher else []
    if processing_watcher:
        processing_watcher.close()

    if reply:
        if as_json:
            print_json({"sent": data, "reply": reply, "processing_statuses": processing_statuses})
        else:
            console.print(f"\n[bold cyan]aX:[/bold cyan] {reply.get('content', '')}")
            gateway_note = _gateway_reply_note(reply)
            if gateway_note:
                console.print(f"[dim]{gateway_note}[/dim]")
    else:
        if as_json:
            print_json(
                {
                    "sent": data,
                    "reply": None,
                    "timeout": True,
                    "processing_statuses": processing_statuses,
                }
            )
        else:
            if processing_statuses:
                last_status = processing_statuses[-1].get("status")
                console.print(
                    f"\n[yellow]No final reply within {timeout}s, "
                    f"but target emitted processing status: {last_status}.[/yellow]"
                )
            else:
                console.print(f"\n[yellow]No reply within {timeout}s. Check later: ax messages list[/yellow]")


@app.command("list")
def list_messages(
    limit: int = typer.Option(20, "--limit", help="Max messages to return"),
    channel: str = typer.Option("main", "--channel", help="Channel name"),
    unread: bool = typer.Option(False, "--unread", help="Show only unread messages for the current user"),
    mark_read: bool = typer.Option(False, "--mark-read", help="Mark returned unread messages as read"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    as_json: bool = JSON_OPTION,
):
    """List recent messages."""
    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)
    try:
        kwargs = {"limit": limit, "channel": channel, "space_id": sid}
        if unread:
            kwargs["unread_only"] = True
        if mark_read:
            kwargs["mark_read"] = True
        data = client.list_messages(**kwargs)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    messages = _message_items(data)
    if as_json:
        print_json(messages)
    else:
        for m in messages:
            c = str(m.get("content", ""))
            m["content_short"] = c[:60] + "..." if len(c) > 60 else c
            m["sender"] = m.get("display_name") or m.get("sender_handle") or m.get("sender_type", "")
            full_id = str(m.get("id", ""))
            m["short_id"] = full_id[:8] if full_id else ""
        print_table(
            ["ID", "Sender", "Content", "Created At"],
            messages,
            keys=["short_id", "sender", "content_short", "created_at"],
        )
        if isinstance(data, dict):
            unread_count = data.get("unread_count")
            marked_read_count = data.get("marked_read_count")
            if unread_count is not None:
                console.print(f"[dim]Unread: {unread_count}[/dim]")
            if marked_read_count:
                console.print(f"[green]Marked read: {marked_read_count}[/green]")


@app.command("read")
def mark_read(
    message_id: Optional[str] = typer.Argument(None, help="Message ID to mark read"),
    all_messages: bool = typer.Option(False, "--all", help="Mark all messages in the current space as read"),
    as_json: bool = JSON_OPTION,
):
    """Mark one message, or all current-space messages, as read."""
    if not all_messages and not message_id:
        typer.echo("Error: provide a message ID or --all.", err=True)
        raise typer.Exit(1)
    if all_messages and message_id:
        typer.echo("Error: use either a message ID or --all, not both.", err=True)
        raise typer.Exit(1)

    client = get_client()
    try:
        if all_messages:
            data = client.mark_all_messages_read()
        else:
            data = client.mark_message_read(_resolve_message_id(client, message_id or ""))
    except httpx.HTTPStatusError as e:
        handle_error(e)
    if as_json:
        print_json(data)
    else:
        print_kv(data)


@app.command("get")
def get(
    message_id: str = typer.Argument(..., help="Message ID"),
    as_json: bool = JSON_OPTION,
):
    """Get a single message."""
    client = get_client()
    try:
        data = client.get_message(_resolve_message_id(client, message_id))
    except httpx.HTTPStatusError as e:
        handle_error(e)
    if as_json:
        print_json(data)
    else:
        print_kv(data)


@app.command("edit")
def edit(
    message_id: str = typer.Argument(..., help="Message ID"),
    content: str = typer.Argument(..., help="New content"),
    as_json: bool = JSON_OPTION,
):
    """Edit a message."""
    client = get_client()
    try:
        data = client.edit_message(_resolve_message_id(client, message_id), content)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    if as_json:
        print_json(data)
    else:
        print_kv(data)


@app.command("delete")
def delete(
    message_id: str = typer.Argument(..., help="Message ID"),
    as_json: bool = JSON_OPTION,
):
    """Delete a message."""
    client = get_client()
    try:
        resolved_message_id = _resolve_message_id(client, message_id)
        client.delete_message(resolved_message_id)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    if as_json:
        print_json({"status": "deleted", "message_id": resolved_message_id})
    else:
        typer.echo("Deleted.")


@app.command("search")
def search(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(20, "--limit", help="Max results"),
    as_json: bool = JSON_OPTION,
):
    """Search messages."""
    client = get_client()
    try:
        data = client.search_messages(query, limit=limit)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    results = data if isinstance(data, list) else data.get("results", data.get("messages", []))
    if as_json:
        print_json(results)
    else:
        for m in results:
            c = str(m.get("content", ""))
            m["content_short"] = c[:60] + "..." if len(c) > 60 else c
            m["sender"] = m.get("display_name") or m.get("sender_handle") or m.get("sender_type", "")
        print_table(
            ["ID", "Sender", "Content", "Created At"],
            results,
            keys=["id", "sender", "content_short", "created_at"],
        )
