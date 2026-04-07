"""ax events — SSE event streaming."""

import json
import sys
from typing import Optional

import httpx
import typer

from ..config import get_client
from ..output import JSON_OPTION, console

app = typer.Typer(name="events", help="Event streaming", no_args_is_help=True)

ROUTING_EVENT_TYPES = {"routing_status", "dispatch_progress", "agent_processing"}


@app.command("stream")
def stream(
    max_events: int = typer.Option(0, "--max-events", help="Stop after N events (0=unlimited)"),
    filter: Optional[str] = typer.Option(None, "--filter", help="Filter: 'routing', 'messages', or event type"),
    as_json: bool = JSON_OPTION,
):
    """Stream SSE events in real-time. Use --filter routing to see only routing events."""
    client = get_client()
    url = f"{client.base_url}/api/v1/sse/messages"
    params = {"token": client.token}
    headers = {}

    filter_types: set[str] | None = None
    if filter == "routing":
        filter_types = ROUTING_EVENT_TYPES
    elif filter == "messages":
        filter_types = {"message", "mention"}
    elif filter:
        filter_types = {filter}

    typer.echo(f"Connecting to {url} ...", err=True)
    if filter_types:
        typer.echo(f"Filtering: {', '.join(sorted(filter_types))}", err=True)
    count = 0
    try:
        with httpx.stream("GET", url, params=params, headers=headers, timeout=None) as resp:
            event_type = None
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    if filter_types and event_type not in filter_types:
                        continue

                    data_str = line[5:].strip()
                    try:
                        parsed = json.loads(data_str)
                    except json.JSONDecodeError:
                        parsed = data_str

                    if as_json:
                        print(json.dumps({"event": event_type, "data": parsed}, default=str))
                        sys.stdout.flush()
                    else:
                        preview = data_str[:120] + "..." if len(data_str) > 120 else data_str
                        console.print(f"[bold cyan][{event_type}][/bold cyan] {preview}")

                    count += 1
                    if max_events and count >= max_events:
                        typer.echo(f"\nReached {max_events} events, stopping.", err=True)
                        return
    except KeyboardInterrupt:
        typer.echo(f"\nStopped after {count} events.", err=True)
    except httpx.HTTPStatusError as e:
        typer.echo(f"Error {e.response.status_code}: {e.response.text}", err=True)
        raise typer.Exit(1)
