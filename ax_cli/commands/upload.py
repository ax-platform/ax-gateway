"""ax upload — upload files to context, optionally notify agents."""

import json
from pathlib import Path
from typing import Optional

import httpx
import typer

from ..config import get_client, resolve_space_id
from ..output import JSON_OPTION, console, handle_error, print_json

app = typer.Typer(name="upload", help="Upload files to context", no_args_is_help=True)


@app.command("file")
def upload_file(
    file_path: str = typer.Argument(..., help="Path to the file to upload"),
    message: Optional[str] = typer.Option(None, "--message", "-m", help="Message to send referencing the upload"),
    key: Optional[str] = typer.Option(None, "--key", "-k", help="Context key (default: filename)"),
    vault: bool = typer.Option(False, "--vault", help="Store permanently in vault (default: ephemeral 24h)"),
    skip_ax: bool = typer.Option(False, "--skip-ax", help="Send message without waiting for aX reply"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only output the attachment ID"),
    json_output: bool = JSON_OPTION,
):
    """Upload a file to context and optionally send a message about it.

    Pattern: file → upload API → context vault → message notifies agents.
    Agents access the file through context, not inline in chat.

    Examples:
        ax upload file screenshot.png -m "check this screenshot"
        ax upload file report.pdf --vault --message "aX review this report"
        ax upload file data.csv --key "sales-q1" --vault
        ax upload file arch.png --quiet   # just get the ID
    """
    client = get_client()
    space_id = resolve_space_id(client)
    path = Path(file_path).expanduser().resolve()

    if not path.exists():
        console.print(f"[red]File not found: {file_path}[/red]")
        raise typer.Exit(1)

    # Step 1: Upload the file
    try:
        result = client.upload_file(str(path))
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except httpx.HTTPStatusError as exc:
        handle_error(exc)
        raise typer.Exit(1)

    attachment_id = result.get("attachment_id", result.get("file_id", ""))
    url = result.get("url", "")
    content_type = result.get("content_type", "")
    size = result.get("size", 0)
    original_name = result.get("original_filename", path.name)
    context_key = key or original_name

    if quiet:
        typer.echo(attachment_id)
        return

    if not json_output:
        console.print(f"[green]Uploaded:[/green] {original_name} ({content_type}, {size} bytes)")

    # Step 2: Store reference in context
    # For text-based files under 50KB, inline the content so agents can read it
    TEXT_TYPES = ("text/", "application/json", "application/xml", "application/yaml")
    MAX_INLINE_SIZE = 50_000
    inline_content = None
    if size <= MAX_INLINE_SIZE and any(content_type.startswith(t) for t in TEXT_TYPES):
        try:
            inline_content = Path(file_path).expanduser().resolve().read_text(errors="replace")
        except Exception:
            pass

    context_value = {
        "type": "file_upload",
        "attachment_id": attachment_id,
        "filename": original_name,
        "content_type": content_type,
        "size": size,
        "url": url,
    }
    if inline_content is not None:
        context_value["content"] = inline_content

    try:
        if vault:
            r = client._http.post(
                f"/api/v1/spaces/{space_id}/intelligence/promote",
                json={
                    "key": context_key,
                    "payload": context_value,
                    "summary_snippet": f"Uploaded file: {original_name}",
                    "artifact_type": "RESEARCH",
                },
            )
            r.raise_for_status()
            storage_type = "vault"
        else:
            client.set_context(space_id, context_key, json.dumps(context_value))
            storage_type = "ephemeral (24h)"

        if not json_output:
            console.print(f"[green]Context:[/green] key={context_key} ({storage_type})")
    except httpx.HTTPStatusError:
        if not json_output:
            console.print("[yellow]Warning: upload succeeded but context store failed[/yellow]")
        storage_type = "failed"

    # Step 3: Send message referencing the upload
    # Default: always notify. Use --quiet to skip message.
    msg_id = None
    if not quiet:
        if message is not None:
            content = f"{message}\n\n📎 Uploaded `{original_name}` to context (key: `{context_key}`)"
        else:
            content = f"📎 Uploaded `{original_name}` to context (key: `{context_key}`)"
        attachments = [
            {
                "id": attachment_id,
                "content_type": content_type,
                "filename": original_name,
                "size_bytes": size,
            }
        ]

        try:
            msg = client.send_message(space_id, content, attachments=attachments)
            msg_id = msg.get("id", msg.get("message", {}).get("id", ""))
            if not json_output:
                console.print(f"[green]Message sent:[/green] {msg_id}")
        except httpx.HTTPStatusError as exc:
            handle_error(exc)
            raise typer.Exit(1)

        # Wait for aX reply
        if not skip_ax and msg_id:
            from .messages import _wait_for_reply

            _wait_for_reply(client, msg_id, timeout=60)

    if json_output:
        print_json(
            {
                "attachment_id": attachment_id,
                "url": url,
                "filename": original_name,
                "content_type": content_type,
                "size": size,
                "context_key": context_key,
                "context_storage": storage_type,
                "message_id": msg_id,
            }
        )
