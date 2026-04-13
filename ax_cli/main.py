"""aX Platform CLI — Typer app with subcommand registration."""

import sys
from typing import Optional

import httpx
import typer

from .commands import (
    agents,
    auth,
    channel,
    context,
    credentials,
    events,
    handoff,
    keys,
    listen,
    messages,
    mint,
    profile,
    spaces,
    tasks,
    upload,
    watch,
)

app = typer.Typer(name="ax", help="aX Platform CLI", no_args_is_help=True)
app.add_typer(auth.app, name="auth")
app.add_typer(keys.app, name="keys")
app.add_typer(credentials.app, name="credentials")
app.add_typer(agents.app, name="agents")
app.add_typer(messages.app, name="messages")
app.add_typer(tasks.app, name="tasks")
app.add_typer(events.app, name="events")
app.add_typer(listen.app, name="listen")
app.add_typer(context.app, name="context")
app.add_typer(watch.app, name="watch")
app.add_typer(upload.app, name="upload")
app.add_typer(profile.app, name="profile")
app.add_typer(spaces.app, name="spaces")
app.add_typer(channel.app, name="channel")
app.add_typer(mint.app, name="token")
app.command("handoff")(handoff.run)


@app.command("login")
def login(
    token: str = typer.Option(None, "--token", "-t", help="PAT token (axp_u_... or axp_a_...)"),
    base_url: str = typer.Option("http://localhost:8002", "--url", "-u", help="API base URL"),
    agent: str = typer.Option(None, "--agent", "-a", help="Agent name or ID (auto-detected if not set)"),
    space_id: str = typer.Option(None, "--space-id", "-s", help="Space ID (auto-detected if not set)"),
):
    """Alias for `ax auth init` using more familiar CLI vocabulary."""
    auth.init(token=token, base_url=base_url, agent=agent, space_id=space_id)


@app.command("send")
def send_shortcut(
    content: str = typer.Argument(..., help="Message to send"),
    wait: bool = typer.Option(True, "--wait/--skip-ax", "-w", help="Wait for aX response (default: yes)"),
    timeout: int = typer.Option(60, "--timeout", "-t", help="Max seconds to wait"),
    reply_to: Optional[str] = typer.Option(None, "--reply-to", "--parent", "-r", help="Reply to message ID (thread)"),
    to: Optional[str] = typer.Option(None, "--to", help="@mention another agent by name"),
    act_as: Optional[str] = typer.Option(
        None, "--act-as", help="Impersonate: send as a different agent. Requires scoped token."
    ),
    files: Optional[list[str]] = typer.Option(None, "--file", "-f", help="Attach a local file (repeatable)"),
    space_id: Optional[str] = typer.Option(None, "--space-id", "-s", help="Override default space"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Send a message and wait for aX's response by default. Use --skip-ax to send only."""
    messages.send(
        content=content,
        wait=wait,
        timeout=timeout,
        to=to,
        act_as=act_as,
        files=files,
        channel="main",
        parent=reply_to,
        space_id=space_id,
        as_json=as_json,
    )


def main():
    """Entry point with global error handling."""
    try:
        app()
    except httpx.ConnectError:
        typer.echo("Error: cannot reach aX API. Is the server running?", err=True)
        sys.exit(1)
