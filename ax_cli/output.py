"""Shared output helpers: --json flag, tables, error handling."""

import json

import httpx
import typer
from rich.console import Console
from rich.table import Table

console = Console()

JSON_OPTION = typer.Option(False, "--json", help="Output as JSON")
SPACE_OPTION = typer.Option(None, "--space-id", help="Override default space")


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
