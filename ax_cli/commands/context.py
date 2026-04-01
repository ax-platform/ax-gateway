"""ax context — shared context and file upload operations."""
import tempfile
from pathlib import Path
from typing import Optional

import httpx
import typer

from ..config import get_client, resolve_space_id
from ..output import JSON_OPTION, print_json, print_kv, print_table, handle_error

app = typer.Typer(name="context", help="Context & file operations", no_args_is_help=True)


def _normalize_upload(payload: dict) -> dict:
    """Normalize varying upload response shapes into a consistent dict."""
    attachment = payload.get("attachment") if isinstance(payload, dict) else None
    if isinstance(attachment, dict):
        return attachment
    return {
        "filename": payload.get("original_filename") or payload.get("filename"),
        "content_type": payload.get("content_type"),
        "size": payload.get("size_bytes") or payload.get("size"),
        "url": payload.get("url"),
    }


@app.command("upload-file")
def upload_file(
    file_path: str = typer.Argument(..., help="Local file to upload"),
    key: Optional[str] = typer.Option(None, "--key", "-k", help="Context key (default: filename)"),
    vault: bool = typer.Option(False, "--vault", help="Store permanently in the intelligence vault (default: ephemeral)"),
    ttl: Optional[int] = typer.Option(None, "--ttl", help="Ephemeral TTL in seconds (default: 86400 = 24h)"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    as_json: bool = JSON_OPTION,
):
    """Upload a local file and store a reference in shared context.

    By default, the reference is stored ephemerally (24h TTL in Redis).
    Use --vault to promote it to the permanent intelligence vault.

    Examples:
        ax context upload-file ./report.md
        ax context upload-file ./arch.png --key infra-diagram --vault
        ax context upload-file ./data.csv --ttl 3600
    """
    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)

    # Upload the file
    try:
        upload_data = client.upload_file(file_path)
    except FileNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except httpx.HTTPStatusError as exc:
        handle_error(exc)

    info = _normalize_upload(upload_data)
    context_key = key or Path(file_path).name

    # Store reference in context — inline text content so agents can read it
    content_type = info.get("content_type", "")
    is_text = content_type and (
        content_type.startswith("text/")
        or content_type in ("application/json", "application/xml", "application/yaml")
    )

    text_content = None
    if is_text:
        try:
            text_content = Path(file_path).read_text(errors="replace")
        except Exception:
            pass

    context_value = {
        "type": "file_upload",
        "filename": info.get("filename"),
        "content_type": content_type,
        "size": info.get("size"),
        "url": info.get("url"),
        "source": "local",
        "original_path": file_path,
    }
    if text_content is not None:
        context_value["content"] = text_content

    import json
    try:
        if vault:
            # Promote to permanent vault storage
            r = client._http.post(
                f"/api/v1/spaces/{sid}/intelligence/promote",
                json={
                    "key": context_key,
                    "payload": context_value,
                    "summary_snippet": f"Uploaded file: {info.get('filename')}",
                    "artifact_type": "RESEARCH",
                },
            )
            r.raise_for_status()
            context_value["storage"] = "vault"
        else:
            # Ephemeral context (Redis)
            client.set_context(sid, context_key, json.dumps(context_value), ttl=ttl)
            context_value["storage"] = "ephemeral"
            context_value["ttl"] = ttl or 86400
    except httpx.HTTPStatusError as exc:
        # Upload succeeded but context store failed — still show the upload
        typer.echo(f"Warning: file uploaded but context store failed: {exc}", err=True)

    context_value["key"] = context_key

    if as_json:
        print_json(context_value)
    else:
        print_kv(context_value)


@app.command("fetch-url")
def fetch_url(
    url: str = typer.Argument(..., help="URL to fetch and store"),
    key: Optional[str] = typer.Option(None, "--key", "-k", help="Context key (default: derived from URL)"),
    vault: bool = typer.Option(False, "--vault", help="Store permanently in the intelligence vault"),
    ttl: Optional[int] = typer.Option(None, "--ttl", help="Ephemeral TTL in seconds (default: 86400)"),
    upload: bool = typer.Option(False, "--upload", help="Upload the fetched content as a file (not just store the text)"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    as_json: bool = JSON_OPTION,
):
    """Fetch a URL and store its content in shared context.

    By default, stores the text content directly in context.
    Use --upload to download and upload the file (for images, PDFs, etc).

    Examples:
        ax context fetch-url https://example.com/api-docs.md
        ax context fetch-url https://example.com/diagram.png --upload --vault
        ax context fetch-url https://example.com/data.json --key api-schema --ttl 7200
    """
    import json
    from urllib.parse import urlparse

    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)

    # Derive a default key from the URL
    parsed = urlparse(url)
    default_key = Path(parsed.path).name or parsed.netloc
    context_key = key or default_key

    typer.echo(f"Fetching {url} ...")

    try:
        resp = httpx.get(url, follow_redirects=True, timeout=30.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        typer.echo(f"Error fetching URL: {exc}", err=True)
        raise typer.Exit(1) from exc

    content_type = resp.headers.get("content-type", "").split(";")[0].strip()
    is_text = content_type.startswith("text/") or content_type in (
        "application/json", "application/xml", "application/javascript",
    )

    if upload or not is_text:
        # Download to temp file, then upload
        suffix = Path(parsed.path).suffix or ".bin"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(resp.content)
            tmp_path = tmp.name

        try:
            upload_data = client.upload_file(tmp_path)
        except httpx.HTTPStatusError as exc:
            handle_error(exc)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        info = _normalize_upload(upload_data)
        context_value = {
            "type": "url_fetch_upload",
            "filename": info.get("filename"),
            "content_type": info.get("content_type") or content_type,
            "size": info.get("size"),
            "url": info.get("url"),
            "source_url": url,
        }
    else:
        # Store text content directly in context
        text_content = resp.text
        context_value = {
            "type": "url_fetch_text",
            "content_type": content_type,
            "size": len(resp.content),
            "source_url": url,
            "content_preview": text_content[:200] + ("..." if len(text_content) > 200 else ""),
        }

    try:
        if vault:
            r = client._http.post(
                f"/api/v1/spaces/{sid}/intelligence/promote",
                json={
                    "key": context_key,
                    "payload": {**context_value, "content": resp.text if is_text and not upload else None},
                    "summary_snippet": f"Fetched from {url}",
                    "artifact_type": "RESEARCH",
                },
            )
            r.raise_for_status()
            context_value["storage"] = "vault"
        else:
            store_value = json.dumps(context_value) if upload or not is_text else resp.text
            client.set_context(sid, context_key, store_value, ttl=ttl)
            context_value["storage"] = "ephemeral"
            context_value["ttl"] = ttl or 86400
    except httpx.HTTPStatusError as exc:
        typer.echo(f"Warning: fetch succeeded but context store failed: {exc}", err=True)

    context_value["key"] = context_key

    if as_json:
        print_json(context_value)
    else:
        print_kv(context_value)


@app.command("set")
def set_ctx(
    key: str = typer.Argument(..., help="Context key"),
    value: str = typer.Argument(..., help="Context value"),
    ttl: Optional[int] = typer.Option(None, "--ttl", help="TTL in seconds"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    as_json: bool = JSON_OPTION,
):
    """Set a key-value pair in ephemeral context."""
    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)
    try:
        data = client.set_context(sid, key, value, ttl=ttl)
    except httpx.HTTPStatusError as exc:
        handle_error(exc)
    if as_json:
        print_json(data)
    else:
        typer.echo(f"Set: {key}")


@app.command("get")
def get_ctx(
    key: str = typer.Argument(..., help="Context key"),
    as_json: bool = JSON_OPTION,
):
    """Get a context value by key."""
    client = get_client()
    try:
        data = client.get_context(key)
    except httpx.HTTPStatusError as exc:
        handle_error(exc)
    if as_json:
        print_json(data)
    else:
        print_kv(data)


@app.command("list")
def list_ctx(
    prefix: Optional[str] = typer.Option(None, "--prefix", help="Filter by key prefix"),
    as_json: bool = JSON_OPTION,
):
    """List context entries."""
    client = get_client()
    try:
        data = client.list_context(prefix=prefix)
    except httpx.HTTPStatusError as exc:
        handle_error(exc)
    items = data if isinstance(data, list) else data.get("items", data.get("context", []))
    if as_json:
        print_json(items)
    else:
        print_table(
            ["Key", "Value Preview", "TTL"],
            items,
            keys=["key", "value", "ttl"],
        )


@app.command("delete")
def delete_ctx(
    key: str = typer.Argument(..., help="Context key to delete"),
):
    """Delete a context entry."""
    client = get_client()
    try:
        client.delete_context(key)
    except httpx.HTTPStatusError as exc:
        handle_error(exc)
    typer.echo(f"Deleted: {key}")


@app.command("download")
def download_file(
    key: str = typer.Argument(..., help="Context key to download"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path (default: original filename)"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
):
    """Download a file from context to local disk."""
    import json as _json
    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)

    try:
        data = client.get_context(key)
    except httpx.HTTPStatusError as e:
        handle_error(e)

    # Parse the value to find URL and filename
    raw = data.get("value", data)
    if isinstance(raw, dict) and "value" in raw:
        raw = raw["value"]
    if isinstance(raw, str):
        try:
            raw = _json.loads(raw)
        except Exception:
            console.print("[red]Context value is not a file upload[/red]")
            raise typer.Exit(1)

    if not isinstance(raw, dict) or raw.get("type") != "file_upload":
        console.print("[red]Context key is not a file upload[/red]")
        raise typer.Exit(1)

    url = raw.get("url", "")
    filename = output or raw.get("filename", key)

    if not url:
        console.print("[red]No URL in file upload[/red]")
        raise typer.Exit(1)

    # Download
    try:
        headers = {k: v for k, v in client._headers.items() if k != "Content-Type"}
        with httpx.Client(headers=headers, timeout=60.0, follow_redirects=True) as http:
            r = http.get(url)
            r.raise_for_status()
            from pathlib import Path
            Path(filename).write_bytes(r.content)
            console.print(f"[green]Downloaded:[/green] {filename} ({len(r.content)} bytes)")
    except httpx.HTTPStatusError as e:
        handle_error(e)
