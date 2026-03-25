"""Shared output helpers: --json flag, tables, error handling."""
import json

import httpx
import typer
from rich.console import Console
from rich.table import Table

console = Console()

JSON_OPTION = typer.Option(False, "--json", help="Output as JSON")
SPACE_OPTION = typer.Option(None, "--space-id", help="Override default space")
AGENT_OPTION = typer.Option(None, "--agent-id", help="Target agent")


def print_json(data):
    console.print_json(json.dumps(data, default=str))


def print_table(columns: list[str], rows: list[dict], *, keys: list[str] | None = None):
    if keys is None:
        keys = [c.lower().replace(" ", "_") for c in columns]
    table = Table()
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*[str(row.get(k, "")) for k in keys])
    console.print(table)


def print_kv(data: dict):
    for k, v in data.items():
        console.print(f"[bold]{k}[/bold]: {v}")


def is_stale_agent_binding_error(e: httpx.HTTPStatusError) -> bool:
    """Return True when the API rejected a saved agent binding for this token."""
    try:
        detail = str(e.response.json().get("detail", ""))
    except Exception:
        detail = e.response.text or ""
    lowered = detail.lower()
    return e.response.status_code == 403 and "allowed_agent_ids" in lowered and "not permitted" in lowered


def handle_error(e: httpx.HTTPStatusError):
    content_type = e.response.headers.get("content-type", "")
    if "json" in content_type:
        try:
            detail = e.response.json().get("detail", e.response.text)
        except Exception:
            detail = e.response.text
    elif "html" in content_type:
        detail = (
            f"Server returned HTML instead of JSON (content-type: {content_type}). "
            "This usually means the request hit the frontend instead of the API "
            "— check base_url and agent config."
        )
    else:
        detail = e.response.text[:200] if e.response.text else str(e)
    if is_stale_agent_binding_error(e):
        detail = (
            f"{detail}\n"
            "Hint: the saved agent binding is not valid for this credential. "
            "Rebind with 'ax auth bind --agent <name>' or 'ax auth bind --agent-id <uuid>', "
            "or clear it with 'ax auth unbind'. Use '--as-user' only for user-scoped admin "
            "operations like key management."
        )
    typer.echo(f"Error {e.response.status_code}: {detail}", err=True)
    raise typer.Exit(1)
