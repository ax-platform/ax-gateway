"""ax context — shared context and file upload operations."""

import os
import tempfile
import webbrowser
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import httpx
import typer

from ..config import get_client, resolve_space_id
from ..context_keys import build_upload_context_key
from ..output import JSON_OPTION, handle_error, mention_prefix, print_json, print_kv, print_table

app = typer.Typer(name="context", help="Context & file operations", no_args_is_help=True)

TEXT_CONTENT_TYPES = {
    "application/json",
    "application/javascript",
    "application/xml",
    "application/yaml",
    "image/svg+xml",
    "text/x-python",
    "text/typescript",
}

TEXT_SUFFIXES = {
    ".css",
    ".csv",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".py",
    ".svg",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


_mention_prefix = mention_prefix


def _send_context_mention(client, sid: str, mention: str | None, message: str) -> str | None:
    prefix = _mention_prefix(mention)
    if not prefix:
        return None
    try:
        sent = client.send_message(sid, f"{prefix} {message}")
    except httpx.HTTPStatusError as exc:
        typer.echo(f"Warning: context updated but mention failed: {exc}", err=True)
        return None
    return sent.get("id", sent.get("message", {}).get("id", ""))


def _normalize_upload(payload: dict) -> dict:
    """Normalize varying upload response shapes into a consistent dict."""
    attachment = payload.get("attachment") if isinstance(payload, dict) else None
    if isinstance(attachment, dict):
        return attachment
    return {
        "attachment_id": payload.get("attachment_id") or payload.get("id") or payload.get("file_id"),
        "filename": payload.get("original_filename") or payload.get("filename"),
        "content_type": payload.get("content_type"),
        "size": payload.get("size_bytes") or payload.get("size"),
        "url": payload.get("url"),
    }


def _optional_space_id(client, explicit: str | None) -> str | None:
    if explicit:
        return resolve_space_id(client, explicit=explicit)
    if os.environ.get("AX_SPACE_ID"):
        return resolve_space_id(client)
    return None


def _safe_filename(name: str) -> str:
    candidate = Path(name).name.strip()
    return candidate or "context-preview.bin"


def _context_file_payload(data: dict, key: str) -> dict:
    """Extract a file reference from a context get response."""
    import json as _json

    raw = data.get("value", data)
    if isinstance(raw, dict) and "value" in raw:
        raw = raw["value"]
    if isinstance(raw, str):
        try:
            raw = _json.loads(raw)
        except Exception as exc:
            raise ValueError("Context value is not a file upload") from exc

    if not isinstance(raw, dict) or not raw.get("url"):
        raise ValueError("Context key is not a file upload")

    filename = raw.get("filename") or raw.get("name") or key
    return {
        **raw,
        "filename": _safe_filename(str(filename)),
    }


def _fetch_context_file(client, sid: str | None, payload: dict) -> bytes:
    url = payload.get("url", "")
    if not url:
        raise ValueError("No URL in file upload")

    download_url = urljoin(f"{client.base_url}/", url)
    headers = {k: v for k, v in client._auth_headers().items() if k != "Content-Type"}
    with httpx.Client(headers=headers, timeout=60.0, follow_redirects=True) as http:
        response = http.get(download_url, params={"space_id": sid} if sid else None)
        response.raise_for_status()
        return response.content


def _preview_cache_dir(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    if os.environ.get("AX_PREVIEW_CACHE_DIR"):
        return Path(os.environ["AX_PREVIEW_CACHE_DIR"]).expanduser()
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
    return base / "axctl" / "previews"


def _is_text_like(payload: dict) -> bool:
    content_type = str(payload.get("content_type") or "").split(";")[0].strip().lower()
    filename = str(payload.get("filename") or "")
    return (
        content_type.startswith("text/") or content_type in TEXT_CONTENT_TYPES or Path(filename).suffix in TEXT_SUFFIXES
    )


def _load_context_artifact(
    *,
    key: str,
    cache_dir: str | None,
    open_file: bool,
    space_id: str | None,
    include_content: bool,
    max_content_bytes: int,
) -> dict:
    import hashlib

    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)

    data = client.get_context(key, space_id=sid)
    payload = _context_file_payload(data, key)
    content = _fetch_context_file(client, sid, payload)

    key_hash = hashlib.sha256(key.encode()).hexdigest()[:16]
    target_dir = _preview_cache_dir(cache_dir) / key_hash
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / payload["filename"]
    target_path.write_bytes(content)

    result = {
        "key": key,
        "filename": payload["filename"],
        "content_type": payload.get("content_type"),
        "size": len(content),
        "path": str(target_path),
        "source_url": payload.get("url"),
        "cached": True,
        "text_like": _is_text_like(payload),
    }

    if include_content and result["text_like"]:
        trimmed = content[:max_content_bytes]
        result["content"] = trimmed.decode("utf-8", errors="replace")
        result["content_truncated"] = len(content) > max_content_bytes

    if open_file:
        result["opened"] = webbrowser.open(target_path.as_uri())

    return result


@app.command("upload-file")
def upload_file(
    file_path: str = typer.Argument(..., help="Local file to upload into context storage"),
    key: Optional[str] = typer.Option(None, "--key", "-k", help="Context key (default: unique upload key)"),
    vault: bool = typer.Option(
        False, "--vault", help="Store permanently in the intelligence vault (default: ephemeral)"
    ),
    ttl: Optional[int] = typer.Option(None, "--ttl", help="Ephemeral TTL in seconds (default: 86400 = 24h)"),
    mention: Optional[str] = typer.Option(None, "--mention", help="@mention a user or agent after storing context"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    as_json: bool = JSON_OPTION,
):
    """Upload a local file and store a reference in shared context.

    By default, the reference is stored ephemerally (24h TTL in Redis).
    Use --vault to promote it to the permanent intelligence vault.
    This is the lower-level storage primitive. Use `ax send --file` for a
    polished message attachment preview, or `ax upload file` when collaborators
    should see a context upload signal in the transcript by default.

    Examples:
        ax context upload-file ./report.md
        ax context upload-file ./arch.png --key infra-diagram --vault
        ax context upload-file ./data.csv --ttl 3600 --mention @orion
    """
    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)

    # Upload the file
    try:
        upload_data = client.upload_file(file_path, space_id=sid)
    except FileNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except httpx.HTTPStatusError as exc:
        handle_error(exc)

    info = _normalize_upload(upload_data)
    context_key = key or build_upload_context_key(
        info.get("filename") or Path(file_path).name,
        info.get("attachment_id"),
    )

    # Store reference in context — inline text content so agents can read it
    content_type = info.get("content_type", "")
    is_text = content_type and (
        content_type.startswith("text/") or content_type in ("application/json", "application/xml", "application/yaml")
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
            # Vault promotion is Redis -> Postgres. Store the context entry
            # first, then promote that key into durable intelligence storage.
            client.set_context(sid, context_key, json.dumps(context_value), ttl=ttl)
            client.promote_context(sid, context_key, artifact_type="RESEARCH")
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
    msg_id = _send_context_mention(
        client,
        sid,
        mention,
        f"Context uploaded: `{context_key}` ({context_value.get('filename') or Path(file_path).name})",
    )
    if msg_id:
        context_value["message_id"] = msg_id

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
    upload: bool = typer.Option(
        False, "--upload", help="Upload the fetched content as a file (not just store the text)"
    ),
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
        "application/json",
        "application/xml",
        "application/javascript",
    )

    if upload or not is_text:
        # Download to temp file, then upload
        safe_name = _safe_filename(default_key)
        if "." not in safe_name:
            safe_name = f"{safe_name}.bin"
        tmp_dir = Path(tempfile.mkdtemp(prefix="ax-fetch-url-"))
        tmp_path = tmp_dir / safe_name
        tmp_path.write_bytes(resp.content)

        try:
            upload_data = client.upload_file(str(tmp_path), space_id=sid)
        except httpx.HTTPStatusError as exc:
            handle_error(exc)
        finally:
            tmp_path.unlink(missing_ok=True)
            tmp_dir.rmdir()

        info = _normalize_upload(upload_data)
        context_value = {
            "type": "file_upload",
            "filename": info.get("filename"),
            "content_type": info.get("content_type") or content_type,
            "size": info.get("size"),
            "url": info.get("url"),
            "source": "url_fetch",
            "source_url": url,
        }
        if is_text:
            context_value["content"] = resp.text
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
            store_value = json.dumps(
                {**context_value, "content": resp.text if is_text and not upload else context_value.get("content")}
            )
            client.set_context(sid, context_key, store_value, ttl=ttl)
            client.promote_context(sid, context_key, artifact_type="RESEARCH")
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
    mention: Optional[str] = typer.Option(None, "--mention", help="@mention a user or agent after setting context"),
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
    msg_id = _send_context_mention(client, sid, mention, f"Context updated: `{key}`")
    if msg_id and isinstance(data, dict):
        data = {**data, "message_id": msg_id}
    if as_json:
        print_json(data)
    else:
        typer.echo(f"Set: {key}")


@app.command("get")
def get_ctx(
    key: str = typer.Argument(..., help="Context key"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    as_json: bool = JSON_OPTION,
):
    """Get a context value by key."""
    client = get_client()
    sid = _optional_space_id(client, space_id)
    try:
        data = client.get_context(key, space_id=sid)
    except httpx.HTTPStatusError as exc:
        handle_error(exc)
    if as_json:
        print_json(data)
    else:
        print_kv(data)


@app.command("list")
def list_ctx(
    prefix: Optional[str] = typer.Option(None, "--prefix", help="Filter by key prefix"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    as_json: bool = JSON_OPTION,
):
    """List context entries."""
    client = get_client()
    sid = _optional_space_id(client, space_id)
    try:
        data = client.list_context(prefix=prefix, space_id=sid)
    except httpx.HTTPStatusError as exc:
        handle_error(exc)
    # API returns dict of {key: {value, ttl, ...}} — normalize to list of rows
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and not data.get("items") and not data.get("context"):
        # Dict of key→metadata pairs (prod API format)
        items = []
        for k, v in data.items():
            entry = {"key": k}
            if isinstance(v, dict):
                val = v.get("value", str(v))
                entry["value"] = str(val)[:80] if len(str(val)) > 80 else str(val)
                entry["ttl"] = v.get("ttl")
            else:
                entry["value"] = str(v)[:80]
            items.append(entry)
    else:
        items = data.get("items", data.get("context", []))
    if as_json:
        print_json(data)
    else:
        print_table(
            ["Key", "Value Preview", "TTL"],
            items,
            keys=["key", "value", "ttl"],
        )


@app.command("delete")
def delete_ctx(
    key: str = typer.Argument(..., help="Context key to delete"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
):
    """Delete a context entry."""
    client = get_client()
    sid = _optional_space_id(client, space_id)
    try:
        client.delete_context(key, space_id=sid)
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
    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)

    try:
        data = client.get_context(key, space_id=sid)
    except httpx.HTTPStatusError as e:
        handle_error(e)

    try:
        payload = _context_file_payload(data, key)
        content = _fetch_context_file(client, sid, payload)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    except httpx.HTTPStatusError as e:
        handle_error(e)

    filename = output or payload.get("filename", key)
    Path(filename).write_bytes(content)
    typer.echo(f"Downloaded: {filename} ({len(content)} bytes)")


@app.command("load")
def load_file(
    key: str = typer.Argument(..., help="Context key to load"),
    cache_dir: Optional[str] = typer.Option(
        None, "--cache-dir", help="Preview cache directory (default: ~/.cache/axctl/previews)"
    ),
    open_file: bool = typer.Option(False, "--open", help="Open with the system default viewer"),
    include_content: bool = typer.Option(False, "--content", help="Include decoded text for text-like files"),
    max_content_bytes: int = typer.Option(20000, "--max-content-bytes", help="Max decoded content bytes"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    as_json: bool = JSON_OPTION,
):
    """Load a context artifact into a private local cache for agent inspection.

    This keeps context as the source of truth while avoiding manual downloads
    into the current working directory.
    """
    try:
        result = _load_context_artifact(
            key=key,
            cache_dir=cache_dir,
            open_file=open_file,
            space_id=space_id,
            include_content=include_content,
            max_content_bytes=max_content_bytes,
        )
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    except httpx.HTTPStatusError as exc:
        handle_error(exc)

    if as_json:
        print_json(result)
    else:
        print_kv(result)


@app.command("preview")
def preview_file(
    key: str = typer.Argument(..., help="Context key to preview"),
    cache_dir: Optional[str] = typer.Option(
        None, "--cache-dir", help="Preview cache directory (default: ~/.cache/axctl/previews)"
    ),
    open_file: bool = typer.Option(False, "--open", help="Open with the system default viewer"),
    include_content: bool = typer.Option(False, "--content", help="Include decoded text for text-like files"),
    max_content_bytes: int = typer.Option(20000, "--max-content-bytes", help="Max decoded content bytes"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    as_json: bool = JSON_OPTION,
):
    """Preview a context artifact from the private local cache.

    This is an agent-friendly alias for `context load`: it resolves protected
    upload URLs with the active profile, writes the artifact under the preview
    cache, and returns the local path.
    """
    load_file(
        key=key,
        cache_dir=cache_dir,
        open_file=open_file,
        include_content=include_content,
        max_content_bytes=max_content_bytes,
        space_id=space_id,
        as_json=as_json,
    )
