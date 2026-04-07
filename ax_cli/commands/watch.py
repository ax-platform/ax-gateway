"""ax watch — block until a condition is met on the message stream.

Connects via SSE, watches for matching messages, exits when found.
Designed for automation loops: send a command, watch for the response.

Usage:
    ax watch                                    # Wait for any message (30s default)
    ax watch --mention                          # Wait for someone to @mention you
    ax watch --from backend_sentinel            # Wait for a message from specific agent
    ax watch --contains "merged"                # Wait for message containing text
    ax watch --from backend_sentinel --timeout 300  # 5 minute timeout
    ax watch --event tool_call_completed        # Wait for specific SSE event
    ax watch --count 3                          # Wait for 3 matching messages

Examples in scripts:
    ax send "@backend_sentinel implement uploads" --skip-ax
    RESULT=$(ax watch --from backend_sentinel --timeout 600 --json)
    echo "Agent responded: $RESULT"
"""

import json
import time
from typing import Optional

import httpx
import typer

from ..config import get_client, resolve_agent_name, resolve_space_id
from ..output import console

app = typer.Typer(name="watch", help="Wait for messages matching a condition", no_args_is_help=False)


def _iter_sse(response: httpx.Response):
    """Yield (event_type, parsed_data) from an SSE stream."""
    event_type = None
    data_lines: list[str] = []
    for line in response.iter_lines():
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


def _matches(
    event_type: str,
    data: dict,
    *,
    mention: bool = False,
    from_agent: str | None = None,
    contains: str | None = None,
    event_filter: str | None = None,
    agent_name: str = "",
    started_at: float = 0,
) -> bool:
    """Check if an SSE event matches the watch condition."""
    if not isinstance(data, dict):
        return False

    # Only match messages AFTER we started watching
    if started_at > 0:
        msg_time = data.get("timestamp") or data.get("created_at") or data.get("server_time") or ""
        if msg_time:
            try:
                from datetime import datetime

                if msg_time.endswith("Z"):
                    msg_time = msg_time[:-1] + "+00:00"
                msg_ts = datetime.fromisoformat(msg_time).timestamp()
                if msg_ts < started_at:
                    return False  # Message is from before we started watching
            except (ValueError, TypeError):
                pass

    # Event type filter
    if event_filter:
        return event_type == event_filter

    # Only look at message events
    if event_type not in ("message", "mention"):
        return False

    content = data.get("content", "")
    author = data.get("author")
    author_name = author.get("name", "") if isinstance(author, dict) else ""
    sender = data.get("display_name") or data.get("username") or author_name

    # Don't match our own messages
    if sender.lower() == agent_name.lower():
        return False

    # From specific agent
    if from_agent:
        if sender.lower() != from_agent.lower():
            return False

    # Mention filter
    if mention:
        if f"@{agent_name}" not in content:
            return False

    # Contains text
    if contains:
        if contains.lower() not in content.lower():
            return False

    return True


def _watch_poll(
    client,
    *,
    from_agent: str | None = None,
    contains: str | None = None,
    timeout: int = 300,
    interval: int = 5,
    output_json: bool = False,
    quiet: bool = False,
):
    """Poll messages list, show 'Working...' progress, return final response.

    This catches the tool-progress updates that SSE doesn't emit.
    Watches the newest message from `from_agent` (or any non-self sender).
    Exits when the message is no longer 'Working...' status.
    """
    agent_name = resolve_agent_name()
    start_time = time.time()
    last_status = ""

    if not quiet:
        conditions = []
        if from_agent:
            conditions.append(f"@{from_agent}")
        else:
            conditions.append("any agent")
        console.print(
            f"[dim]Polling for response from {', '.join(conditions)} (timeout: {timeout}s, interval: {interval}s)[/dim]"
        )

    while True:
        elapsed = time.time() - start_time
        if timeout > 0 and elapsed > timeout:
            if not quiet:
                console.print(f"\n[yellow]Timeout — no final response in {timeout}s[/yellow]")
            raise typer.Exit(1)

        try:
            data = client.list_messages(limit=10)
        except (httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadError):
            time.sleep(interval)
            continue

        messages = data if isinstance(data, list) else data.get("messages", [])

        # Find the most recent message from the target agent (or any non-self)
        for msg in messages:
            sender = msg.get("display_name") or msg.get("sender_handle") or ""
            if agent_name and sender.lower() == agent_name.lower():
                continue
            if from_agent and sender.lower() != from_agent.lower():
                continue

            content = msg.get("content", "")

            if content.startswith("Working"):
                # Show progress update
                status_line = content.split("\n")[0][:120]
                if status_line != last_status:
                    last_status = status_line
                    if not quiet:
                        console.print(f"  [dim]{status_line}[/dim]", end="\r")
                        # Also show tool lines
                        lines = content.strip().split("\n")
                        for tl in lines[1:4]:
                            console.print(f"  [dim]{tl.strip()[:100]}[/dim]")
                break
            else:
                # Final response — not "Working..." anymore
                if not quiet and not output_json:
                    console.print(f"\n[bold cyan]{sender}:[/bold cyan] {content[:3000]}")
                    if len(content) > 3000:
                        console.print(f"[dim]  ... ({len(content)} chars total)[/dim]")
                if output_json:
                    print(json.dumps(msg, indent=2, default=str))
                if not quiet:
                    console.print(f"\n[dim]Completed in {int(elapsed)}s[/dim]")
                raise typer.Exit(0)

        time.sleep(interval)


@app.callback(invoke_without_command=True)
def watch(
    mention: bool = typer.Option(False, "--mention", "-m", help="Wait for @mention of your agent"),
    from_agent: Optional[str] = typer.Option(None, "--from", "-f", help="Wait for message from specific agent/user"),
    contains: Optional[str] = typer.Option(None, "--contains", "-c", help="Wait for message containing text"),
    event: Optional[str] = typer.Option(None, "--event", "-e", help="Wait for specific SSE event type"),
    timeout: int = typer.Option(30, "--timeout", "-t", help="Timeout in seconds (0 = wait forever)"),
    count: int = typer.Option(1, "--count", "-n", help="Number of matching messages to collect"),
    output_json: bool = typer.Option(False, "--json", help="Output matching messages as JSON"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="No progress output, just the result"),
    poll: bool = typer.Option(False, "--poll", "-p", help="Use polling instead of SSE (shows Working... progress)"),
    interval: int = typer.Option(5, "--interval", "-i", help="Poll interval in seconds (with --poll)"),
):
    """Wait for messages matching a condition.

    By default uses SSE. With --poll, polls messages list instead
    (shows Working... tool progress that SSE doesn't emit).

    Exit codes: 0 = condition met, 1 = timeout, 2 = error
    """
    client = get_client()
    agent_name = resolve_agent_name()

    if poll:
        _watch_poll(
            client,
            from_agent=from_agent,
            contains=contains,
            timeout=timeout,
            interval=interval,
            output_json=output_json,
            quiet=quiet,
        )
        return
    space_id = resolve_space_id(client)

    token = client.token
    base_url = client.base_url

    sse_url = f"{base_url}/api/sse/messages?token={token}"
    if space_id:
        sse_url += f"&space_id={space_id}"

    start_time = time.time()
    matched: list[dict] = []

    if not quiet:
        conditions = []
        if mention:
            conditions.append(f"@{agent_name} mention")
        if from_agent:
            conditions.append(f"from @{from_agent}")
        if contains:
            conditions.append(f"contains '{contains}'")
        if event:
            conditions.append(f"event={event}")
        if not conditions:
            conditions.append("any message")
        console.print(f"[dim]Watching for: {', '.join(conditions)} (timeout: {timeout}s)[/dim]")

    try:
        with httpx.stream(
            "GET",
            sse_url,
            timeout=httpx.Timeout(connect=10, read=float(timeout) if timeout else None, write=10, pool=10),
            follow_redirects=True,
        ) as response:
            if response.status_code != 200:
                console.print(f"[red]SSE connection failed: {response.status_code}[/red]")
                raise typer.Exit(2)

            last_heartbeat = time.time()
            heartbeat_interval = 30  # Print heartbeat every 30s

            for event_type, data in _iter_sse(response):
                # Check timeout
                elapsed = time.time() - start_time
                if timeout > 0 and elapsed > timeout:
                    break

                # Heartbeat — show we're still connected
                now = time.time()
                if not quiet and (now - last_heartbeat) >= heartbeat_interval:
                    remaining = max(0, timeout - elapsed) if timeout > 0 else 0
                    console.print(f"[dim]  ... listening ({int(elapsed)}s elapsed, {int(remaining)}s remaining)[/dim]")
                    last_heartbeat = now

                # Skip non-dict events
                if not isinstance(data, dict):
                    continue

                # SSE keepalive events — confirm connection is alive
                if event_type in ("connected", "bootstrap", "heartbeat", "identity_bootstrap", "ping"):
                    if event_type == "connected" and not quiet:
                        console.print("[dim]  SSE connected[/dim]")
                    continue

                if _matches(
                    event_type,
                    data,
                    mention=mention,
                    from_agent=from_agent,
                    contains=contains,
                    event_filter=event,
                    agent_name=agent_name,
                    started_at=start_time,
                ):
                    matched.append(data)
                    if not quiet and not output_json:
                        sender = data.get("display_name", "?")
                        content = data.get("content", "")[:200]
                        console.print(f"[green]Match:[/green] @{sender}: {content}")

                    if len(matched) >= count:
                        break

    except httpx.ReadTimeout:
        pass  # Timeout is expected
    except KeyboardInterrupt:
        if not quiet:
            console.print("\n[dim]Cancelled[/dim]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(2)

    if matched:
        if output_json:
            if count == 1:
                print(json.dumps(matched[0], indent=2, default=str))
            else:
                print(json.dumps(matched, indent=2, default=str))
        if not quiet and not output_json:
            elapsed = int(time.time() - start_time)
            console.print(f"[dim]{len(matched)} match(es) in {elapsed}s[/dim]")
        raise typer.Exit(0)
    else:
        if not quiet:
            console.print(f"[yellow]Timeout — no matching messages in {timeout}s[/yellow]")
        raise typer.Exit(1)
