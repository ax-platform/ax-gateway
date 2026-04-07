"""ax assign — assign work to an agent and track until complete."""

import time
from typing import Optional

import httpx
import typer

from ..config import get_client, resolve_space_id
from ..output import JSON_OPTION, console, handle_error, print_json

app = typer.Typer(name="assign", help="Assign work to agents and track completion")

# Prompt templates by verb — each alias has its own tone
_PROMPTS = {
    "assign": "@{agent} Assignment: {instructions}\n\nTask ID: `{tid}…`\n\n@mention @orion when done.",
    "ship": "@{agent} Ship this: {instructions}\n\nThis needs to land. Branch from dev/staging, push clean code, open PR. Task: `{tid}…`\n\n@mention @orion when shipped.",
    "manage": "@{agent} Managed task: {instructions}\n\nPlease work through this methodically. Update status as you go. Task: `{tid}…`\n\n@mention @orion with progress or when complete.",
    "boss": "@{agent} Get this done: {instructions}\n\nNo excuses, no investigations, no reports. Ship code. Task: `{tid}…`\n\n@mention @orion when it's done. Don't come back without a branch.",
}

_NUDGES = {
    "assign": "@{agent} Status check — are you still working on: {instructions}? Task: `{tid}…`",
    "ship": "@{agent} This needs to ship. Where are we on: {instructions}? Task: `{tid}…`",
    "manage": "@{agent} Checking in — any blockers on: {instructions}? Task: `{tid}…`",
    "boss": "@{agent} Still waiting. What's the holdup on: {instructions}? Task: `{tid}…`",
}


def _detect_verb() -> str:
    """Detect which alias invoked us (assign/ship/manage/boss)."""
    import sys

    args = sys.argv[1:2]
    if args and args[0] in _PROMPTS:
        return args[0]
    return "assign"


@app.command()
def run(
    agent: str = typer.Argument(..., help="Agent to assign (@name or name)"),
    instructions: str = typer.Argument(..., help="What the agent should do"),
    watch: bool = typer.Option(True, "--watch/--no-watch", help="Watch for completion (default: yes)"),
    timeout: int = typer.Option(300, "--timeout", "-t", help="Seconds to wait per check cycle"),
    max_cycles: int = typer.Option(5, "--max-cycles", help="Max nudge cycles before giving up"),
    priority: str = typer.Option("high", "--priority", help="Task priority"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    json_output: bool = JSON_OPTION,
):
    """Assign work to an agent: create task, send instructions, track until done.

    Creates a task, sends @mention with instructions, then watches for completion.
    If the agent doesn't respond, nudges them. Repeats until done or max cycles.

    Examples:
        ax assign mcp_sentinel "Redesign context-explorer per design brief"
        ax assign backend_sentinel "Fix the auth bug" --timeout 600
        ax assign frontend_sentinel "Add upload button" --no-watch
    """
    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)

    # Normalize agent name and detect verb
    agent_name = agent.lstrip("@")
    verb = _detect_verb()

    # Step 1: Create task
    console.print(f"[cyan]Creating task for @{agent_name}...[/cyan]")
    try:
        task_data = client.create_task(
            sid,
            instructions[:120],
            description=instructions,
            priority=priority,
        )
    except httpx.HTTPStatusError as e:
        handle_error(e)
        raise typer.Exit(1)

    task = task_data.get("task", task_data)
    task_id = str(task.get("id", ""))
    tid_short = task_id[:8]
    console.print(f"[green]Task created:[/green] {tid_short}… ({priority})")

    # Step 2: Send assignment message (tone varies by verb)
    prompt_template = _PROMPTS.get(verb, _PROMPTS["assign"])
    content = prompt_template.format(agent=agent_name, instructions=instructions, tid=tid_short)
    try:
        msg = client.send_message(sid, content)
        msg_id = msg.get("id", msg.get("message", {}).get("id", ""))
        console.print(f"[green]Assignment sent:[/green] {msg_id}")
    except httpx.HTTPStatusError as e:
        handle_error(e)
        raise typer.Exit(1)

    if not watch:
        if json_output:
            print_json({"task_id": task_id, "message_id": msg_id, "agent": agent_name, "status": "assigned"})
        return

    # Step 3: Watch loop — track until agent confirms done
    console.print(f"\n[dim]Watching @{agent_name} (timeout: {timeout}s, max cycles: {max_cycles})...[/dim]")

    for cycle in range(1, max_cycles + 1):
        console.print(f"\n[cyan]── Cycle {cycle}/{max_cycles} ──[/cyan]")

        # Watch for messages from the agent (shows them live, returns on completion signal or timeout)
        result = _watch_for_agent(client, agent_name, timeout=timeout)

        if result is None:
            # No messages at all from the agent
            console.print(f"[yellow]No response from @{agent_name} in {timeout}s. Nudging...[/yellow]")
            try:
                nudge_template = _NUDGES.get(verb, _NUDGES["assign"])
                client.send_message(
                    sid,
                    nudge_template.format(agent=agent_name, instructions=instructions[:80], tid=tid_short),
                )
            except Exception:
                pass
            continue

        # Check if the result contains a completion signal
        response_text = result.lower()
        completion_signals = ["done", "pushed", "merged", "completed", "finished", "pr ", "pull request", "branch "]

        if any(signal in response_text for signal in completion_signals):
            console.print(f"\n[green]@{agent_name} signals completion![/green]")

            try:
                client.update_task(task_id, status="completed")
                console.print(f"[green]Task {tid_short}… marked complete.[/green]")
            except Exception:
                pass

            if json_output:
                print_json(
                    {
                        "task_id": task_id,
                        "message_id": msg_id,
                        "agent": agent_name,
                        "status": "completed",
                        "response": result[:500],
                        "cycles": cycle,
                    }
                )
            return

        # Agent responded but no completion signal — nudge for completion
        console.print(f"\n[yellow]@{agent_name} responded but no completion signal. Nudging...[/yellow]")
        try:
            nudge_template = _NUDGES.get(verb, _NUDGES["assign"])
            client.send_message(
                sid,
                nudge_template.format(agent=agent_name, instructions=instructions[:80], tid=tid_short),
            )
        except Exception:
            pass

    # Exhausted cycles
    console.print(f"\n[yellow]Max cycles reached ({max_cycles}). @{agent_name} has not confirmed completion.[/yellow]")
    console.print("[dim]Check messages manually or run again.[/dim]")

    if json_output:
        print_json(
            {
                "task_id": task_id,
                "message_id": msg_id,
                "agent": agent_name,
                "status": "timeout",
                "cycles": max_cycles,
            }
        )


def _watch_for_agent(client, agent_name: str, *, timeout: int = 300) -> str | None:
    """Watch SSE + poll messages for responses from the agent.

    Returns the latest message content from the agent, or None on timeout.
    Collects ALL messages from the agent during the window — doesn't stop
    at the first "On it." Only returns once the timeout expires or a
    completion signal is found.
    """
    deadline = time.time() + timeout
    seen_ids: set[str] = set()
    poll_interval = 3.0  # Check every 3 seconds — agents respond fast
    latest_content: str | None = None
    completion_signals = [
        "done",
        "pushed",
        "merged",
        "completed",
        "finished",
        "pr ",
        "pull request",
        "branch ",
        "shipped",
        "implemented",
        "opened pr",
    ]

    # Snapshot current message IDs so we only see NEW messages
    try:
        data = client.list_messages(limit=15)
        messages = data if isinstance(data, list) else data.get("messages", data.get("items", []))
        for msg in messages:
            seen_ids.add(msg.get("id", ""))
    except Exception:
        pass

    while time.time() < deadline:
        remaining = int(deadline - time.time())
        if remaining <= 0:
            break

        try:
            data = client.list_messages(limit=15)
            messages = data if isinstance(data, list) else data.get("messages", data.get("items", []))

            for msg in messages:
                msg_id = msg.get("id", "")
                if msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)

                # Check if this message is from our target agent
                # Messages use display_name for sender identity
                sender_candidates = [
                    msg.get("display_name", ""),
                    msg.get("agent_name", ""),
                    msg.get("sender", ""),
                ]
                author = msg.get("author", {})
                if isinstance(author, dict):
                    sender_candidates.extend(
                        [
                            author.get("username", ""),
                            author.get("name", ""),
                            author.get("agent_name", ""),
                        ]
                    )
                elif isinstance(author, str):
                    sender_candidates.append(author)

                sender_str = " ".join(c for c in sender_candidates if c).lower()
                if agent_name.lower() in sender_str:
                    content = msg.get("content", "(no content)")
                    latest_content = content
                    console.print(f"  [dim]@{agent_name}: {content[:120]}[/dim]")

                    # Early exit on completion signal
                    if any(s in content.lower() for s in completion_signals):
                        return content

        except Exception:
            pass

        # Heartbeat
        if remaining % 30 < poll_interval + 1:
            console.print(f"  [dim]... waiting ({remaining}s remaining)[/dim]", end="  \r")

        time.sleep(poll_interval)

    return latest_content
