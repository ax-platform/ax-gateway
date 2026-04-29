"""Shared output helpers: --json flag, tables, error handling."""

import json

import httpx
import typer
from rich.console import Console
from rich.table import Table

console = Console()
# Dedicated stderr console for status/log lines that mustn't pollute stdout
# (e.g. when the caller parses --json output or pipes the command).
err_console = Console(stderr=True)

JSON_OPTION = typer.Option(False, "--json", help="Output as JSON")
SPACE_OPTION = typer.Option(None, "--space-id", help="Override default space")
EXIT_NOT_OK = 2
EXIT_SKIPPED = 3


def apply_envelope(
    data: dict, *, summary: dict | None = None, details: list | None = None, skipped: bool = False
) -> dict:
    """Add the stable QA/diagnostic envelope without removing legacy fields."""
    data["version"] = 1
    data["skipped"] = skipped
    data["summary"] = summary or {}
    data["details"] = details or []
    return data


def mention_prefix(mention: str | None) -> str:
    """Normalize an optional agent/user mention to the @handle form."""
    if not mention:
        return ""
    value = mention.strip()
    if not value:
        return ""
    return value if value.startswith("@") else f"@{value}"


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


def handle_error(e: httpx.HTTPStatusError):
    url = str(e.request.url) if e.request else "unknown"
    try:
        detail = e.response.json().get("detail", e.response.text[:200])
    except Exception:
        body = e.response.text[:200]
        if "<html" in body.lower():
            detail = "Got HTML instead of JSON (frontend may be catching this route)"
        else:
            detail = body
    typer.echo(f"Error {e.response.status_code}: {detail}", err=True)
    typer.echo(f"  URL: {url}", err=True)
    raise typer.Exit(1)
