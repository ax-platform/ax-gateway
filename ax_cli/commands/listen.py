"""ax listen — SSE agent listener.

Connect any agent to the aX platform via SSE. Listens for @mentions
and runs a handler command for each one.

Usage:
    ax listen                              # Echo bot
    ax listen --exec "python my_agent.py"  # Custom handler
    ax listen --dry-run                    # Watch only
    ax listen --agent mybot --exec ./bot   # Named agent
"""

import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
import typer

from ..config import get_client, resolve_agent_name, resolve_space_id
from ..output import console

app = typer.Typer(name="listen", help="Listen for @mentions via SSE", no_args_is_help=False)


# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------


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


def _should_respond(data: dict, agent_name: str, agent_id: str | None) -> bool:
    """Return True if this message is an @mention for our agent."""
    if not isinstance(data, dict):
        return False
    content = data.get("content", "")

    # SSE events use different field names depending on event type:
    # 'message' events have author as dict {"id": ..., "name": ..., "type": ...}
    # 'mention' events have author as string, plus sender_name
    author = data.get("author")
    if isinstance(author, dict):
        sender = author.get("name", "")
        sender_id = author.get("id", "")
    else:
        sender = (
            data.get("display_name")
            or data.get("username")
            or data.get("sender_name")
            or (author if isinstance(author, str) else "")
        )
        sender_id = data.get("agent_id") or ""

    if sender.lower() == agent_name.lower():
        return False
    if agent_id and sender_id == agent_id:
        return False

    return f"@{agent_name}" in content


def _strip_mention(content: str, agent_name: str) -> str:
    """Remove the @mention prefix from content."""
    return re.sub(rf"@{re.escape(agent_name)}\b\s*[-—]?\s*", "", content, count=1).strip()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _run_handler(command: str, prompt: str, *, workdir: str | None = None) -> str:
    """Run an external command with the mention content.

    The command receives the mention text via:
      - AX_MENTION_CONTENT environment variable
      - Last positional argument (when no shell operators present)

    Whatever the command prints to stdout becomes the reply.
    """
    env = {**os.environ, "AX_MENTION_CONTENT": prompt}
    argv = [*shlex.split(command), prompt]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=workdir,
            env=env,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            output += f"\n(stderr: {result.stderr.strip()[:200]})"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "(handler timed out after 300s)"
    except FileNotFoundError:
        return f"(handler not found: {argv[0]})"


def _echo_handler(prompt: str) -> str:
    """Default echo handler — reflects the mention back."""
    return f"Echo: {prompt}"


# ---------------------------------------------------------------------------
# Pause gate (file-based, shared with killswitch.sh)
# ---------------------------------------------------------------------------


def _is_paused(agent_name: str) -> bool:
    pause_all = Path.home() / ".ax" / "sentinel_pause"
    pause_one = Path.home() / ".ax" / f"sentinel_pause_{agent_name}"
    return pause_all.exists() or pause_one.exists()


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------


def _worker(
    mention_queue: queue.Queue,
    client_holder: list,
    agent_name: str,
    agent_id: str | None,
    space_id: str,
    handler,
    dry_run: bool,
):
    """Process mentions sequentially from the queue."""
    while True:
        try:
            data = mention_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        if data is None:
            break

        # Pause gate
        was_paused = False
        while _is_paused(agent_name):
            if not was_paused:
                console.print(f"[yellow]PAUSED[/yellow] — holding {mention_queue.qsize() + 1} messages")
                was_paused = True
            time.sleep(2.0)
        if was_paused:
            console.print("[green]RESUMED[/green]")

        author = data.get("display_name") or data.get("username") or "?"
        content = data.get("content", "")
        msg_id = data.get("id", "")
        prompt = _strip_mention(content, agent_name)

        if not prompt:
            mention_queue.task_done()
            continue

        console.print(
            f"[bold cyan]@{author}[/bold cyan] → [dim]{prompt[:100]}{'...' if len(prompt) > 100 else ''}[/dim]"
        )

        if dry_run:
            console.print("[dim]  (dry run — not responding)[/dim]")
            mention_queue.task_done()
            continue

        try:
            response_text = handler(prompt)
            if response_text:
                client = client_holder[0]
                client.send_message(
                    space_id,
                    response_text,
                    agent_id=agent_id,
                    parent_id=msg_id,
                )
                console.print(f"[green]  replied[/green] ({len(response_text)} chars)")
        except Exception as e:
            console.print(f"[red]  error: {e}[/red]")
        finally:
            mention_queue.task_done()


# ---------------------------------------------------------------------------
# Main command
# ---------------------------------------------------------------------------


@app.callback(invoke_without_command=True)
def listen(
    exec_cmd: Optional[str] = typer.Option(
        None,
        "--exec",
        "-e",
        help="Command to run for each mention. Gets content as last arg + AX_MENTION_CONTENT env var.",
    ),
    agent: Optional[str] = typer.Option(
        None,
        "--agent",
        "-a",
        help="Agent name to listen as (default: from config)",
    ),
    space_id: Optional[str] = typer.Option(
        None,
        "--space-id",
        "-s",
        help="Space to listen in (default: from config)",
    ),
    workdir: Optional[str] = typer.Option(
        None,
        "--workdir",
        "-w",
        help="Working directory for handler command",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Watch mentions without responding",
    ),
    queue_size: int = typer.Option(
        50,
        "--queue-size",
        help="Max queued mentions before dropping",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Output events as JSON lines",
    ),
):
    """Listen for @mentions via SSE and respond.

    \b
    With no --exec, runs as an echo bot (great for testing).
    With --exec, runs your command for each mention.
    \b
    Your handler receives the mention content as:
      - Last positional argument
      - AX_MENTION_CONTENT environment variable
    \b
    Whatever your handler prints to stdout becomes the reply.

    \b
    Examples:
      ax listen                              # Echo bot
      ax listen --exec "python agent.py"     # Custom handler
      ax listen --dry-run                    # Watch only
      ax listen --agent mybot --exec ./bot   # Named agent
    """
    client = get_client()
    agent_name = agent or resolve_agent_name(client=client)
    if not agent_name:
        console.print(
            "[red]Error: No agent name.[/red] Use --agent or set agent_name in "
            ".ax/config.toml or AX_AGENT_NAME env var."
        )
        raise typer.Exit(1)

    sid = resolve_space_id(client, explicit=space_id)

    # Resolve agent_id for proper identity headers
    agent_id = None
    try:
        agents_data = client.list_agents()
        agents_list = agents_data if isinstance(agents_data, list) else agents_data.get("agents", [])
        for a in agents_list:
            if a.get("name", "").lower() == agent_name.lower():
                agent_id = a["id"]
                break
    except Exception:
        pass

    # Build handler function
    if exec_cmd:

        def handler(prompt):
            return _run_handler(exec_cmd, prompt, workdir=workdir)
    else:
        handler = _echo_handler

    # Print banner
    console.print("[bold]ax listen[/bold] — SSE agent listener")
    console.print(f"  Agent:   @{agent_name}" + (f" ({agent_id[:12]}...)" if agent_id else ""))
    console.print(f"  Space:   {sid[:12]}...")
    console.print(f"  API:     {client.base_url}")
    console.print(f"  Handler: {exec_cmd or 'echo (built-in)'}")
    console.print(f"  Mode:    {'DRY RUN' if dry_run else 'LIVE'}")
    console.print()

    # Start worker thread
    mention_q: queue.Queue = queue.Queue(maxsize=queue_size)
    client_holder = [client]

    worker_thread = threading.Thread(
        target=_worker,
        args=(mention_q, client_holder, agent_name, agent_id, sid, handler, dry_run),
        daemon=True,
    )
    worker_thread.start()

    seen_ids: set = set()
    SEEN_MAX = 500
    backoff = 1

    console.print(f"[green]Listening for @{agent_name} mentions...[/green]  (Ctrl+C to stop)\n")

    # SSE loop with auto-reconnect
    while True:
        try:
            with client.connect_sse() as resp:
                if resp.status_code != 200:
                    console.print(f"[red]SSE failed: {resp.status_code}[/red]")
                    raise ConnectionError()

                for event_type, data in _iter_sse(resp):
                    backoff = 1

                    if event_type in ("bootstrap", "heartbeat", "ping", "identity_bootstrap"):
                        continue

                    if event_type == "connected":
                        if as_json:
                            print(json.dumps({"event": "connected", "agent": agent_name}))
                            sys.stdout.flush()
                        continue

                    if event_type in ("message", "mention"):
                        if not isinstance(data, dict):
                            continue
                        msg_id = data.get("id", "")
                        if msg_id in seen_ids:
                            continue

                        if _should_respond(data, agent_name, agent_id):
                            seen_ids.add(msg_id)
                            if len(seen_ids) > SEEN_MAX:
                                seen_ids = set(list(seen_ids)[-SEEN_MAX // 2 :])

                            if as_json:
                                print(
                                    json.dumps(
                                        {
                                            "event": "mention",
                                            "from": data.get("display_name"),
                                            "content": data.get("content", "")[:200],
                                            "id": msg_id,
                                        }
                                    )
                                )
                                sys.stdout.flush()

                            try:
                                mention_q.put_nowait(data)
                            except queue.Full:
                                console.print("[yellow]Queue full — dropping mention[/yellow]")

        except (httpx.ConnectError, httpx.ReadTimeout):
            console.print(f"[yellow]Connection lost. Reconnecting in {backoff}s...[/yellow]")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except KeyboardInterrupt:
            console.print("\n[dim]Shutting down...[/dim]")
            mention_q.put(None)
            worker_thread.join(timeout=5)
            break
        except Exception as e:
            console.print(f"[red]Error: {e}. Reconnecting in {backoff}s...[/red]")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
