"""aX Platform API Client.

API is source of truth. Every write operation requires explicit space_id.

Usage:
    client = AxClient("http://localhost:8001", "axp_u_...")
    me = client.whoami()
    space_id = me["space_id"]  # or from client.list_spaces()
    msg = client.send_message(space_id, "hello")
    client.send_message(space_id, "do this", agent_id="<uuid>")
"""

import hashlib
import mimetypes
import os
import platform
from pathlib import Path
from urllib.parse import quote

import httpx

_EXT_MIME: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".txt": "text/plain",
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".jsx": "text/javascript",
    ".ts": "text/typescript",
    ".tsx": "text/typescript",
    # The upload API intentionally keeps a narrow MIME allowlist. Normalize
    # common source/config artifacts to inert text so CLI uploads work without
    # expanding backend policy for every language-specific MIME type.
    ".java": "text/plain",
    ".go": "text/plain",
    ".rs": "text/plain",
    ".kt": "text/plain",
    ".kts": "text/plain",
    ".c": "text/plain",
    ".cc": "text/plain",
    ".cpp": "text/plain",
    ".cs": "text/plain",
    ".rb": "text/plain",
    ".sh": "text/plain",
    ".css": "text/plain",
    ".xml": "text/plain",
    ".yaml": "text/plain",
    ".yml": "text/plain",
    ".sql": "text/plain",
    ".svg": "image/svg+xml",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_FILENAME_MIME: dict[str, str] = {
    "dockerfile": "text/plain",
    "makefile": "text/plain",
}


def _mime_from_ext(ext: str) -> str | None:
    return _EXT_MIME.get(ext)


def _mime_from_filename(filename: str) -> str | None:
    return _FILENAME_MIME.get(filename.lower())


def _build_fingerprint(token: str) -> dict[str, str]:
    """Build credential fingerprint headers sent on every request.

    These allow the server to detect when a credential is used from
    an unexpected location (copied config, stolen token, etc.).
    All sensitive values are hashed — only non-sensitive metadata is sent in plain text.
    """
    cwd = str(Path.cwd().resolve())
    hostname = platform.node()
    username = os.getenv("USER", os.getenv("USERNAME", ""))

    # Composite identity hash — changes if any of dir/host/user change
    identity = f"{cwd}:{hostname}:{username}"

    return {
        "X-AX-FP": hashlib.sha256(identity.encode()).hexdigest()[:24],
        "X-AX-FP-Token": hashlib.sha256(token.encode()).hexdigest()[:16],
        "X-AX-FP-OS": f"{platform.system()}/{platform.release()}",
        "X-AX-FP-Arch": platform.machine(),
    }


# Honeypot key prefixes — these look like real credentials from other
# platforms but are actually traps. If anyone uses one, the CLI alerts
# the aX platform immediately with full fingerprint data.
HONEYPOT_PREFIXES = {
    "AKIA": "aws-iam",  # AWS IAM access key
    "ASIA": "aws-sts",  # AWS STS temporary key
    "ghp_": "github-pat",  # GitHub personal access token
    "gho_": "github-oauth",  # GitHub OAuth token
    "ghs_": "github-app",  # GitHub App token
    "sk-": "openai",  # OpenAI API key
    "sk-ant-": "anthropic",  # Anthropic API key
    "xoxb-": "slack-bot",  # Slack bot token
    "xoxp-": "slack-user",  # Slack user token
    "SG.": "sendgrid",  # SendGrid API key
    "key-": "generic",  # Generic API key
}


def _check_honeypot(token: str, base_url: str) -> None:
    """Check if a token matches a honeypot pattern and alert the platform.

    Honeypot keys look like real credentials from AWS, GitHub, etc.
    They can be planted in repos, .env files, or config to detect
    unauthorized access. When someone tries to use one, we fire an
    alert to aX with the fingerprint of whoever triggered it.
    """
    for prefix, provider in HONEYPOT_PREFIXES.items():
        if token.startswith(prefix):
            fp = _build_fingerprint(token)
            alert = {
                "event": "honeypot_triggered",
                "provider_pattern": provider,
                "prefix": prefix,
                "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "fingerprint": fp,
            }
            try:
                httpx.post(
                    f"{base_url}/api/v1/security/honeypot",
                    json=alert,
                    timeout=5.0,
                )
            except Exception:
                pass  # Best-effort — don't block the caller
            return


class _RetryOnAuthClient:
    """Wraps httpx.Client to retry on 401 with fresh JWT + exponential backoff.

    Intercepts all HTTP methods. On 401:
    1. Clear cached JWT, force re-exchange
    2. Retry with backoff (0.5s, 1s, 2s)
    3. Give up after 3 retries
    """

    _MAX_RETRIES = 3
    _BACKOFF_BASE = 1.0  # seconds — retries at 1s, 2s, 4s

    def __init__(self, inner: httpx.Client, get_fresh_jwt):
        self._inner = inner
        self._get_fresh_jwt = get_fresh_jwt

    def _retry(self, method: str, *args, **kwargs) -> httpx.Response:
        r = getattr(self._inner, method)(*args, **kwargs)
        if r.status_code != 401 or not self._get_fresh_jwt:
            return r

        import time as _time

        for attempt in range(self._MAX_RETRIES):
            backoff = self._BACKOFF_BASE * (2**attempt)
            _time.sleep(backoff)
            fresh_jwt = self._get_fresh_jwt()
            headers = kwargs.get("headers") or {}
            headers["Authorization"] = f"Bearer {fresh_jwt}"
            kwargs["headers"] = headers
            r = getattr(self._inner, method)(*args, **kwargs)
            if r.status_code != 401:
                return r
        return r  # final 401 after all retries

    def get(self, *args, **kwargs):
        return self._retry("get", *args, **kwargs)

    def post(self, *args, **kwargs):
        return self._retry("post", *args, **kwargs)

    def put(self, *args, **kwargs):
        return self._retry("put", *args, **kwargs)

    def patch(self, *args, **kwargs):
        return self._retry("patch", *args, **kwargs)

    def delete(self, *args, **kwargs):
        return self._retry("delete", *args, **kwargs)

    def stream(self, *args, **kwargs):
        return self._inner.stream(*args, **kwargs)

    def close(self):
        self._inner.close()


def _block_user_token(context: str) -> None:
    """Hard-block user tokens when they are mixed with agent identity config.

    User PATs exchange to user JWTs and can operate as the user. They must not
    be used from an agent profile, because that makes an agent appear to act
    with the user's identity.
    """
    import sys

    sys.stderr.write(
        f"\n\033[31m✗  Blocked: user token (axp_u_) cannot be used for: {context}\033[0m\n"
        "   User PATs exchange to user JWTs and act as the user.\n"
        "   Agent profiles need an agent PAT so actions are attributed to the agent.\n"
        "   Get an agent token first:\n"
        "     ax token mint <agent-name> --create      # mint agent PAT (requires user PAT)\n"
        "\n"
    )
    sys.stderr.flush()
    raise SystemExit(1)


class AxClient:
    def __init__(self, base_url: str, token: str, *, agent_name: str | None = None, agent_id: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.agent_id = agent_id  # Used for exchange parameters, NOT headers (§13)
        self.agent_name = agent_name

        # Check for honeypot keys before doing anything else
        _check_honeypot(token, self.base_url)

        # AUTH-SPEC-001 §13: PAT → exchange → JWT for all business calls
        # PAT is ONLY sent to /auth/exchange, never to business endpoints
        self._exchanger = None
        self._use_exchange = token.startswith("axp_")

        if self._use_exchange:
            from .token_cache import TokenExchanger

            self._exchanger = TokenExchanger(base_url, token)

        self._base_headers = {
            "Content-Type": "application/json",
        }
        self._base_headers.update(_build_fingerprint(token))
        # Legacy: X-Agent-Name only for non-exchange path (Cognito JWTs)
        if agent_name and not self._use_exchange:
            self._base_headers["X-Agent-Name"] = agent_name

        # For non-PAT tokens (Cognito), use directly
        if not self._use_exchange:
            self._base_headers["Authorization"] = f"Bearer {token}"

        inner = httpx.Client(
            base_url=self.base_url,
            headers=self._base_headers,
            timeout=30.0,
            event_hooks={"request": [self._inject_auth]} if self._use_exchange else {},
        )
        # Wrap with 401 retry — on auth failure, force re-exchange and retry with backoff
        get_fresh = (lambda: self._get_jwt(force_refresh=True)) if self._use_exchange else None
        self._http = _RetryOnAuthClient(inner, get_fresh)

    def _get_jwt(self, *, force_refresh: bool = False) -> str:
        """Get a JWT from the exchanger with appropriate token class.

        Token class selection:
        - axp_a_ (agent-bound PAT) + configured agent_id → agent_access
        - axp_a_ without configured agent_id → user_access fallback
        - axp_u_ (user PAT) → user_access always, even if agent_id is set
          (user PATs cannot exchange for agent_access — server returns 422)

        User PATs are valid for explicit user-authored CLI work and bootstrap,
        but they must not be combined with agent identity config. That mix is
        how an agent accidentally speaks as the user.
        """
        is_agent_pat = self.token.startswith("axp_a_")
        if self.agent_id and is_agent_pat:
            return self._exchanger.get_token(
                "agent_access",
                agent_id=self.agent_id,
                scope="messages tasks context agents spaces search",
                force_refresh=force_refresh,
            )
        if self.token.startswith("axp_u_") and (self.agent_id or self.agent_name):
            _block_user_token("user PAT with agent identity configured")
        return self._exchanger.get_token(
            "user_access",
            scope="messages tasks context agents spaces search",
            force_refresh=force_refresh,
        )

    def _inject_auth(self, request: httpx.Request) -> None:
        """httpx event hook: inject fresh JWT on every request.

        AUTH-SPEC-001 §13: PAT never sent to business endpoints.
        The exchanger handles caching — this just sets the header.
        """
        if self._exchanger and "Authorization" not in request.headers:
            request.headers["Authorization"] = f"Bearer {self._get_jwt()}"

    def _auth_headers(self, *, for_agent: bool = False) -> dict:
        """Get headers with a fresh JWT from exchange, or static token.

        AUTH-SPEC-001 §13: --agent affects exchange parameters only.
        No X-Agent-Id/X-Agent-Name headers with exchange auth.
        Uses agent_access when agent_id is set (server determines PAT class
        from credential binding, not prefix).
        """
        if self._exchanger:
            jwt = self._get_jwt()
            return {**self._base_headers, "Authorization": f"Bearer {jwt}"}
        return {**self._base_headers, "Authorization": f"Bearer {self.token}"}

    def _with_agent(self, agent_id: str | None) -> dict:
        """Get auth headers, targeting agent if specified.

        With exchange auth: agent_id is used in exchange parameters (frozen in JWT).
        Without exchange auth: falls back to X-Agent-Id header (legacy).
        """
        headers = self._auth_headers(for_agent=bool(agent_id or self.agent_id))
        # Legacy path only: add X-Agent-Id header for non-exchange auth
        if agent_id and not self._use_exchange:
            headers["X-Agent-Id"] = agent_id
        return headers

    def _is_html_response(self, r: httpx.Response) -> bool:
        content_type = r.headers.get("content-type", "")
        return "text/html" in content_type or r.text.lstrip().startswith("<!")

    def _parse_json(self, r: httpx.Response) -> dict | list[dict]:
        """Parse JSON response, raising a clear error if HTML is returned."""
        if self._is_html_response(r):
            path = r.request.url.path if r.request else str(r.url)
            method = r.request.method.upper() if r.request else ""
            if method == "POST" and path == "/api/v1/agents":
                detail = (
                    "Agent create returned HTML instead of JSON. The hosted API must return a JSON 4xx "
                    "with an explicit reason such as quota, rate limit, name conflict, or feature flag; "
                    "the CLI cannot safely infer the denied create reason from the SPA shell."
                )
            else:
                detail = f"Expected JSON but got HTML from {r.url} — the frontend may be catching this API route"
            raise httpx.HTTPStatusError(
                detail,
                request=r.request,
                response=r,
            )
        return r.json()

    def _is_management_route_miss(self, r: httpx.Response) -> bool:
        """Return true when a deploy/proxy missed a management route.

        Some environments expose agent management at /api/v1/agents/manage/*,
        while older/local mounts expose /agents/manage/*. Fall back only for
        route-shape misses; authz/authn failures must remain visible.
        """
        if self._is_html_response(r):
            return True
        return r.status_code in {404, 405}

    def _management_json_with_fallback(self, method: str, paths: list[str], **kwargs) -> dict | list[dict]:
        """Request JSON from the first live management route in paths."""
        last_response: httpx.Response | None = None
        for path in paths:
            r = getattr(self._http, method)(path, **kwargs)
            if self._is_management_route_miss(r):
                last_response = r
                continue
            r.raise_for_status()
            return self._parse_json(r)

        if last_response is None:
            raise RuntimeError("No management route paths provided")
        last_response.raise_for_status()
        return self._parse_json(last_response)

    # --- Identity ---

    def whoami(self) -> dict:
        """GET /auth/me — returns user identity."""
        r = self._http.get("/auth/me")
        r.raise_for_status()
        return self._parse_json(r)

    # --- Spaces ---

    def list_spaces(self) -> list[dict]:
        r = self._http.get("/api/v1/spaces")
        r.raise_for_status()
        return self._parse_json(r)

    def get_space(self, space_id: str) -> dict:
        r = self._http.get(f"/api/v1/spaces/{space_id}")
        r.raise_for_status()
        return self._parse_json(r)

    def create_space(self, name: str, *, description: str | None = None, visibility: str = "private") -> dict:
        """POST /api/spaces/create — create a new space."""
        body = {"name": name, "visibility": visibility}
        if description:
            body["description"] = description
        r = self._http.post("/api/spaces/create", json=body)
        r.raise_for_status()
        return self._parse_json(r)

    def list_space_members(self, space_id: str) -> list[dict]:
        r = self._http.get(f"/api/v1/spaces/{space_id}/members")
        r.raise_for_status()
        return self._parse_json(r)

    # --- Messages ---

    def send_heartbeat(
        self,
        *,
        agent_id: str | None = None,
        status: str | None = None,
        note: str | None = None,
        cadence_seconds: int | None = None,
    ) -> dict:
        """POST /api/v1/agents/heartbeat — refresh agent presence in Redis.

        Backend currently treats this as a presence ping (no body required).
        ``status`` / ``note`` / ``cadence_seconds`` are forward-compatible —
        sent in the body so backend can adopt them when the richer heartbeat
        protocol lands. Backend extras are ignored gracefully.
        """
        body: dict = {}
        if status is not None:
            body["status"] = status
        if note is not None:
            body["note"] = note
        if cadence_seconds is not None:
            body["cadence_seconds"] = cadence_seconds
        r = self._http.post(
            "/api/v1/agents/heartbeat",
            json=body,
            headers=self._with_agent(agent_id),
        )
        r.raise_for_status()
        return self._parse_json(r)

    def send_message(
        self,
        space_id: str,
        content: str,
        *,
        agent_id: str | None = None,
        channel: str = "main",
        parent_id: str | None = None,
        attachments: list[dict] | None = None,
        metadata: dict | None = None,
        message_type: str = "text",
    ) -> dict:
        """POST /api/v1/messages — explicit space_id required."""
        body: dict = {"content": content, "space_id": space_id, "channel": channel, "message_type": message_type}
        if parent_id:
            body["parent_id"] = parent_id
        if metadata:
            body["metadata"] = metadata
        if attachments:
            body["attachments"] = attachments
            merged_metadata = dict(metadata or {})
            merged_metadata["accepted_attachments"] = attachments
            body["metadata"] = merged_metadata
        r = self._http.post("/api/v1/messages", json=body, headers=self._with_agent(agent_id))
        r.raise_for_status()
        return self._parse_json(r)

    def set_agent_processing_status(
        self,
        message_id: str,
        status: str,
        *,
        agent_name: str | None = None,
        space_id: str | None = None,
        activity: str | None = None,
        tool_name: str | None = None,
        progress: dict | None = None,
        detail: dict | None = None,
        reason: str | None = None,
        error_message: str | None = None,
        retry_after_seconds: int | None = None,
        parent_message_id: str | None = None,
    ) -> dict:
        """POST /api/v1/agents/processing-status.

        Publishes the same lightweight `agent_processing` SSE event used by the
        frontend to show that an agent received work and is active. This is
        best-effort presence/progress, not durable task state.
        """
        body: dict = {"message_id": message_id, "status": status}
        if agent_name:
            body["agent_name"] = agent_name
        optional_fields = {
            "activity": activity,
            "tool_name": tool_name,
            "progress": progress,
            "detail": detail,
            "reason": reason,
            "error_message": error_message,
            "retry_after_seconds": retry_after_seconds,
            "parent_message_id": parent_message_id,
        }
        for key, value in optional_fields.items():
            if value is not None:
                body[key] = value
        headers = self._with_agent(self.agent_id)
        if space_id:
            headers["X-Space-Id"] = space_id
        r = self._http.post("/api/v1/agents/processing-status", json=body, headers=headers)
        r.raise_for_status()
        return self._parse_json(r)

    def record_tool_call(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        space_id: str | None = None,
        tool_action: str | None = None,
        resource_uri: str | None = None,
        arguments_hash: str | None = None,
        kind: str | None = None,
        arguments: dict | None = None,
        initial_data: dict | None = None,
        status: str = "success",
        duration_ms: int | None = None,
        agent_name: str | None = None,
        agent_id: str | None = None,
        message_id: str | None = None,
        correlation_id: str | None = None,
    ) -> dict:
        """POST /api/v1/tool-calls.

        Records a tool-call audit event from an authenticated agent runtime.
        The backend stores it durably and fans out progress/tool-call SSE so
        the operator UI can show richer in-flight activity.
        """
        body: dict = {
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "status": status,
        }
        optional_fields = {
            "space_id": space_id,
            "tool_action": tool_action,
            "resource_uri": resource_uri,
            "arguments_hash": arguments_hash,
            "kind": kind,
            "arguments": arguments,
            "initial_data": initial_data,
            "duration_ms": duration_ms,
            "agent_name": agent_name,
            "agent_id": agent_id,
            "message_id": message_id,
            "correlation_id": correlation_id,
        }
        for key, value in optional_fields.items():
            if value is not None:
                body[key] = value
        headers = self._with_agent(agent_id)
        if space_id:
            headers["X-Space-Id"] = space_id
        r = self._http.post("/api/v1/tool-calls", json=body, headers=headers)
        r.raise_for_status()
        return self._parse_json(r)

    def upload_file(self, file_path: str, *, space_id: str | None = None) -> dict:
        """POST /api/v1/uploads — upload a local file.

        Uses a separate httpx client to avoid sending Content-Type: application/json
        on the multipart request.
        """
        path = Path(file_path).expanduser().resolve()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")

        content_type = (
            _mime_from_filename(path.name)
            or _mime_from_ext(path.suffix.lower())
            or mimetypes.guess_type(path.name)[0]
            or "application/octet-stream"
        )
        headers = {k: v for k, v in self._auth_headers().items() if k != "Content-Type"}

        with path.open("rb") as fh:
            with httpx.Client(
                base_url=self.base_url,
                headers=headers,
                timeout=60.0,
                follow_redirects=True,
            ) as upload_http:
                r = upload_http.post(
                    "/api/v1/uploads/",
                    files={"file": (path.name, fh, content_type)},
                    data={"space_id": space_id} if space_id else None,
                )
        r.raise_for_status()
        return self._parse_json(r)

    def list_messages(
        self,
        limit: int = 20,
        channel: str = "main",
        *,
        space_id: str | None = None,
        agent_id: str | None = None,
        unread_only: bool = False,
        mark_read: bool = False,
    ) -> dict:
        params: dict[str, str | int] = {"limit": limit, "channel": channel}
        if space_id:
            params["space_id"] = space_id
        if unread_only:
            params["unread_only"] = "true"
        if mark_read:
            params["mark_read"] = "true"
        r = self._http.get("/api/v1/messages", params=params, headers=self._with_agent(agent_id))
        r.raise_for_status()
        return self._parse_json(r)

    def mark_message_read(self, message_id: str) -> dict:
        r = self._http.post(f"/api/v1/messages/{message_id}/read")
        r.raise_for_status()
        return self._parse_json(r)

    def mark_all_messages_read(self) -> dict:
        r = self._http.post("/api/v1/messages/mark-all-read")
        r.raise_for_status()
        return self._parse_json(r)

    def get_message(self, message_id: str) -> dict:
        r = self._http.get(f"/api/v1/messages/{message_id}")
        r.raise_for_status()
        return self._parse_json(r)

    def edit_message(self, message_id: str, content: str) -> dict:
        r = self._http.patch(f"/api/v1/messages/{message_id}", json={"content": content})
        r.raise_for_status()
        return self._parse_json(r)

    def delete_message(self, message_id: str) -> int:
        r = self._http.delete(f"/api/v1/messages/{message_id}")
        r.raise_for_status()
        return r.status_code

    def add_reaction(self, message_id: str, emoji: str) -> dict:
        r = self._http.post(f"/api/v1/messages/{message_id}/reactions", json={"emoji": emoji})
        r.raise_for_status()
        return self._parse_json(r)

    def list_replies(self, message_id: str) -> dict:
        r = self._http.get(f"/api/v1/messages/{message_id}/replies")
        r.raise_for_status()
        return self._parse_json(r)

    # --- Tasks ---

    def create_task(
        self,
        space_id: str,
        title: str,
        *,
        description: str | None = None,
        priority: str = "medium",
        assignee_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict:
        """POST /api/v1/tasks — explicit space_id required."""
        body = {"title": title, "space_id": space_id, "priority": priority}
        if description:
            body["description"] = description
        if assignee_id:
            body["assignee_id"] = assignee_id
        headers = self._with_agent(agent_id)
        r = self._http.post("/api/v1/tasks", json=body, headers=headers)
        if r.status_code < 400 and self._is_html_response(r):
            # Hosted frontend deployments may catch POST /api/v1/tasks and
            # return the app shell. Fall back to the frontend write endpoint,
            # but only when the current session is already scoped to the
            # requested space: /api/tasks ignores explicit space_id.
            self._verify_session_space_for_task_fallback(space_id)
            legacy_r = self._http.post(
                "/api/tasks",
                json={
                    "title": title,
                    "description": description,
                    "priority": priority,
                    "requirements": {},
                },
                headers=headers,
            )
            legacy_r.raise_for_status()
            data = self._parse_json(legacy_r)
            self._verify_created_task_space(data, space_id)
            return data

        r.raise_for_status()
        data = self._parse_json(r)
        self._verify_created_task_space(data, space_id)
        return data

    def _task_from_create_response(self, data: object) -> dict:
        if isinstance(data, dict) and isinstance(data.get("task"), dict):
            return data["task"]
        if isinstance(data, dict):
            return data
        return {}

    def _whoami_space_id(self, data: dict) -> str | None:
        for key in ("resolved_space_id", "space_id"):
            value = data.get(key)
            if value:
                return str(value)

        bound_agent = data.get("bound_agent")
        if isinstance(bound_agent, dict):
            for key in ("default_space_id", "space_id"):
                value = bound_agent.get(key)
                if value:
                    return str(value)
        return None

    def _verify_session_space_for_task_fallback(self, expected_space_id: str) -> None:
        try:
            data = self.whoami()
        except httpx.HTTPStatusError:
            raise
        except Exception as exc:
            raise RuntimeError(
                "Hosted task create route returned HTML and axctl could not verify the active session space "
                "before using the fallback write route."
            ) from exc

        actual_space_id = self._whoami_space_id(data)
        if actual_space_id != str(expected_space_id):
            raise RuntimeError(
                "Hosted task create route returned HTML, and the fallback write route uses the credential's "
                "active space instead of --space-id. Refusing to create the task because the active session "
                f"space is {actual_space_id or 'unknown'}, not requested space {expected_space_id}."
            )

    def _verify_created_task_space(self, data: object, expected_space_id: str) -> None:
        task = self._task_from_create_response(data)
        actual_space_id = task.get("space_id")
        if actual_space_id:
            if str(actual_space_id) != str(expected_space_id):
                raise RuntimeError(
                    f"Task was created in the wrong space: expected {expected_space_id}, got {actual_space_id}."
                )
            return

        task_id = task.get("id")
        if not task_id:
            raise RuntimeError(
                "Task create response did not include an id or space_id, so axctl cannot prove the target space."
            )

        listed = self.list_tasks(limit=100, space_id=expected_space_id)
        tasks = listed if isinstance(listed, list) else listed.get("tasks", [])
        if any(str(item.get("id")) == str(task_id) for item in tasks if isinstance(item, dict)):
            return

        raise RuntimeError(
            "Task create response did not include space_id and the new task was not visible in "
            f"requested space {expected_space_id}. It may have landed in the credential's default space."
        )

    def list_tasks(
        self,
        limit: int = 20,
        *,
        agent_id: str | None = None,
        space_id: str | None = None,
    ) -> dict:
        params: dict[str, str | int] = {"limit": limit}
        if space_id:
            params["space_id"] = space_id
        r = self._http.get("/api/v1/tasks", params=params, headers=self._with_agent(agent_id))
        r.raise_for_status()
        return self._parse_json(r)

    def get_task(self, task_id: str) -> dict:
        r = self._http.get(f"/api/v1/tasks/{task_id}")
        r.raise_for_status()
        return self._parse_json(r)

    def update_task(self, task_id: str, **fields) -> dict:
        r = self._http.patch(f"/api/v1/tasks/{task_id}", json=fields)
        r.raise_for_status()
        return self._parse_json(r)

    # --- Agents ---

    def list_agents(self, *, space_id: str | None = None, limit: int | None = None) -> dict:
        params: dict[str, str | int] = {}
        if space_id:
            params["space_id"] = space_id
        if limit:
            params["limit"] = limit
        r = self._http.get("/api/v1/agents", params=params or None)
        r.raise_for_status()
        return self._parse_json(r)

    def get_agents_presence(self) -> dict:
        """GET /api/v1/agents/presence — bulk presence for all agents."""
        r = self._http.get("/api/v1/agents/presence")
        r.raise_for_status()
        return self._parse_json(r)

    def list_agents_availability(
        self,
        *,
        space_id: str | None = None,
        connection_path: str | None = None,
        badge_state: str | None = None,
        filter_: str | None = None,
    ) -> dict | list:
        """GET /api/v1/agents/availability — bulk resolved DTO list.

        Per AVAIL-CONTRACT-001 spec: optimized for picker/widget rendering.
        Each row is the resolved ``agent_state`` envelope. Optional query
        filters: ``connection_path=gateway_managed|mcp_only|...``,
        ``badge_state=live|routable_delayed|...``, ``filter=available_now|
        gateway_connected|cloud_agent|disabled|recently_active``.
        """
        params: dict[str, str] = {}
        if space_id:
            params["space_id"] = space_id
        if connection_path:
            params["connection_path"] = connection_path
        if badge_state:
            params["badge_state"] = badge_state
        if filter_:
            params["filter"] = filter_
        r = self._http.get("/api/v1/agents/availability", params=params or None)
        r.raise_for_status()
        return self._parse_json(r)

    def get_agent_placement(self, agent_id_or_name: str) -> dict:
        """GET agent record + extract placement-relevant fields.

        No dedicated GET /placement endpoint today (per backend's
        agents_unified.py). Reads the agent record and surfaces
        ``space_id`` / ``pinned`` / ``allowed_spaces`` (when present)
        in a stable shape, plus passes through any ``placement`` /
        ``placement_state`` sub-objects backend emits later.
        """
        # Resolve UUID directly; otherwise look up via /manage/{name}
        if len(agent_id_or_name) == 36 and agent_id_or_name.count("-") == 4:
            agent = self.get_agent(agent_id_or_name)
        else:
            agent = self.get_agent(agent_id_or_name)
        record = agent.get("agent", agent) if isinstance(agent, dict) else {}
        return {
            "agent_id": record.get("id"),
            "name": record.get("name"),
            "space_id": record.get("space_id"),
            "pinned": record.get("pinned"),
            "allowed_spaces": record.get("allowed_spaces"),
            "placement": record.get("placement"),  # forward-compat — backend may emit later
            "placement_state": record.get("placement_state"),
            "_record": record,  # full record for diagnostics
        }

    def set_agent_placement(self, agent_id_or_name: str, *, space_id: str, pinned: bool = False) -> dict:
        """POST /api/v1/agents/{id}/placement — set default space + pinned.

        Per ax-backend agents_unified.py, body is ``{space_id, pinned}``.
        Future-compat: when backend implements the full
        ``GATEWAY-PLACEMENT-POLICY-001`` machinery (policy_kind /
        allowed_spaces / placement_state envelope), this method's
        signature can extend; the existing call site stays compatible.
        """
        body = {"space_id": space_id, "pinned": pinned}
        r = self._http.post(
            f"/api/v1/agents/{agent_id_or_name}/placement",
            json=body,
        )
        r.raise_for_status()
        return self._parse_json(r)

    def get_agent_presence(self, agent_id_or_name: str, *, space_id: str | None = None) -> dict:
        """GET single-agent availability record.

        Tries the AVAIL-CONTRACT-001 ``/state`` endpoint first (returns an
        envelope ``{agent_state, raw_presence, control}``). Falls back to
        the legacy ``/presence`` endpoint (basic flat shape) on 404.

        Returns a flat dict — when ``/state`` succeeds, the ``agent_state``
        sub-object is unwrapped so callers see the resolved DTO directly,
        with envelope siblings (``raw_presence``, ``control``) preserved
        alongside for diagnostic access.

        Accepts either an agent UUID directly (single round trip) or a name
        which is resolved via the agents list first.
        """
        identifier = agent_id_or_name
        # Resolve name → id if we got a name (the /state endpoint accepts both,
        # but the /presence fallback only takes UUIDs).
        if not (len(identifier) == 36 and identifier.count("-") == 4):
            agents = self.list_agents()
            items = agents if isinstance(agents, list) else agents.get("agents", [])
            match = None
            for a in items:
                if isinstance(a, dict) and a.get("name") == agent_id_or_name:
                    match = a
                    break
            if not match:
                raise RuntimeError(f"agent not found: {agent_id_or_name}")
            agent_id = match.get("id") or match.get("agent_id")
            if not agent_id:
                raise RuntimeError(f"agent has no id field: {agent_id_or_name}")
            identifier = agent_id

        params = {"space_id": space_id} if space_id else None

        # Try /state first (AVAIL-CONTRACT-001). If 404, fall back to /presence.
        try:
            r = self._http.get(f"/api/v1/agents/{identifier}/state", params=params)
            r.raise_for_status()
            envelope = self._parse_json(r)
            # Unwrap the envelope so the resolved DTO is at the top level.
            if isinstance(envelope, dict) and "agent_state" in envelope:
                resolved = dict(envelope.get("agent_state") or {})
                # Preserve envelope siblings under reserved keys for diagnostics.
                if "raw_presence" in envelope:
                    resolved["_raw_presence"] = envelope["raw_presence"]
                if "control" in envelope:
                    resolved["_control"] = envelope["control"]
                return resolved
            return envelope
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            # /state not available yet — fall back to /presence.
        r = self._http.get(f"/api/v1/agents/{identifier}/presence", params=params)
        r.raise_for_status()
        return self._parse_json(r)

    def create_agent(self, name: str, **kwargs) -> dict:
        """POST /api/v1/agents — create a new agent."""
        body: dict = {"name": name}
        for key in ("description", "system_prompt", "model", "space_id", "enable_cloud_agent", "can_manage_agents"):
            if key in kwargs and kwargs[key] is not None:
                body[key] = kwargs[key]
        r = self._http.post("/api/v1/agents", json=body)
        r.raise_for_status()
        return self._parse_json(r)

    def get_agent(self, identifier: str) -> dict:
        """GET /api/v1/agents/manage/{identifier} — get by name or UUID."""
        r = self._http.get(f"/api/v1/agents/manage/{identifier}")
        r.raise_for_status()
        return self._parse_json(r)

    def update_agent(self, identifier: str, **fields) -> dict:
        """PUT /api/v1/agents/manage/{identifier} — update agent."""
        r = self._http.put(f"/api/v1/agents/manage/{identifier}", json=fields)
        r.raise_for_status()
        return self._parse_json(r)

    def delete_agent(self, identifier: str) -> dict:
        """DELETE /api/v1/agents/manage/{identifier} — delete agent."""
        r = self._http.delete(f"/api/v1/agents/manage/{identifier}")
        r.raise_for_status()
        return self._parse_json(r)

    def get_agent_tools(self, space_id: str, agent_id: str) -> dict:
        """GET /{space_id}/roster filtered to one agent — returns enabled_tools."""
        r = self._http.get(
            f"/api/v1/organizations/{space_id}/roster",
            params={"entry_type": "agent"},
        )
        r.raise_for_status()
        roster = self._parse_json(r)
        entries = roster.get("entries", roster) if isinstance(roster, dict) else roster
        for entry in entries if isinstance(entries, list) else []:
            if str(entry.get("id")) == agent_id:
                return {
                    "agent_id": agent_id,
                    "name": entry.get("name"),
                    "enabled_tools": entry.get("enabled_tools"),
                    "capabilities": entry.get("capabilities_list"),
                }
        return {"agent_id": agent_id, "enabled_tools": None, "error": "not_found"}

    # --- Context ---

    def set_context(self, space_id: str, key: str, value: str, *, ttl: int | None = None) -> dict:
        """POST /api/v1/context — explicit space_id required."""
        body = {"key": key, "value": value, "space_id": space_id}
        if ttl:
            body["ttl"] = ttl
        r = self._http.post("/api/v1/context", json=body)
        r.raise_for_status()
        return self._parse_json(r)

    def promote_context(
        self,
        space_id: str,
        key: str,
        *,
        artifact_type: str = "RESEARCH",
        agent_id: str | None = None,
    ) -> dict:
        """POST /api/v1/spaces/{space_id}/intelligence/promote for an existing context key."""
        body = {"key": key, "artifact_type": artifact_type}
        if agent_id:
            body["agent_id"] = agent_id
        r = self._http.post(f"/api/v1/spaces/{space_id}/intelligence/promote", json=body)
        r.raise_for_status()
        return self._parse_json(r)

    def get_context(self, key: str, *, space_id: str | None = None) -> dict:
        params = {"space_id": space_id} if space_id else None
        r = self._http.get(f"/api/v1/context/{quote(key, safe='')}", params=params)
        r.raise_for_status()
        return self._parse_json(r)

    def list_context(self, prefix: str | None = None, *, space_id: str | None = None) -> dict:
        params = {}
        if prefix:
            params["prefix"] = prefix
        if space_id:
            params["space_id"] = space_id
        r = self._http.get("/api/v1/context", params=params)
        r.raise_for_status()
        return self._parse_json(r)

    def delete_context(self, key: str, *, space_id: str | None = None) -> int:
        params = {"space_id": space_id} if space_id else None
        r = self._http.delete(f"/api/v1/context/{quote(key, safe='')}", params=params)
        r.raise_for_status()
        return r.status_code

    # --- Search ---

    def search_messages(self, query: str, limit: int = 20, *, agent_id: str | None = None) -> dict:
        r = self._http.post(
            "/api/v1/search/messages", json={"query": query, "limit": limit}, headers=self._with_agent(agent_id)
        )
        r.raise_for_status()
        return self._parse_json(r)

    # --- Keys (PAT management) ---

    def create_key(
        self,
        name: str,
        *,
        allowed_agent_ids: list[str] | None = None,
        bound_agent_id: str | None = None,
        audience: str | None = None,
        scopes: list[str] | None = None,
        space_id: str | None = None,
    ) -> dict:
        """POST /api/v1/keys — mint a user PAT, optionally bound to an agent.

        When ``bound_agent_id`` is set, the resulting PAT inherits the agent's
        allowed-spaces policy and can only be used to send as that agent. This
        is the prod-friendly alternative to ``/credentials/agent-pat`` when the
        latter isn't routed (see axctl-friction-2026-04-17 §3).
        """
        body: dict = {"name": name}
        if allowed_agent_ids:
            body["agent_scope"] = "agents"
            body["allowed_agent_ids"] = allowed_agent_ids
        if bound_agent_id:
            body["bound_agent_id"] = bound_agent_id
        if audience:
            body["audience"] = audience
        if scopes:
            body["scopes"] = scopes
        headers = dict(self._base_headers)
        if space_id:
            headers["X-Space-Id"] = space_id
        r = self._http.post("/api/v1/keys", json=body, headers=headers)
        r.raise_for_status()
        return self._parse_json(r)

    def list_keys(self) -> list[dict]:
        r = self._http.get("/api/v1/keys")
        r.raise_for_status()
        return self._parse_json(r)

    def revoke_key(self, credential_id: str) -> int:
        r = self._http.delete(f"/api/v1/keys/{credential_id}")
        return r.status_code

    def rotate_key(self, credential_id: str) -> dict:
        r = self._http.post(f"/api/v1/keys/{credential_id}/rotate")
        r.raise_for_status()
        return self._parse_json(r)

    # --- SSE ---

    # --- Management API (user_admin JWT) ---

    def _admin_headers(self, scope: str) -> dict:
        """Get headers with a user_admin JWT for management operations."""
        if not self._exchanger:
            return self._base_headers
        jwt = self._exchanger.get_token("user_admin", scope=scope)
        return {**self._base_headers, "Authorization": f"Bearer {jwt}"}

    def mgmt_create_agent(self, name: str, **kwargs) -> dict:
        """Create an agent — requires user_admin + agents.create."""
        body: dict = {"name": name}
        for k in ("description", "system_prompt", "model", "space_id"):
            if k in kwargs and kwargs[k] is not None:
                body[k] = kwargs[k]
        return self._management_json_with_fallback(
            "post",
            ["/api/v1/agents/manage/create", "/agents/manage/create"],
            json=body,
            headers=self._admin_headers("agents.create"),
        )

    def mgmt_list_agents(self) -> list[dict]:
        """List manageable agents — requires user_admin + agents.create."""
        return self._management_json_with_fallback(
            "get",
            ["/api/v1/agents/manage/list", "/agents/manage/list"],
            headers=self._admin_headers("agents.create"),
        )

    def mgmt_update_agent(self, agent_id: str, **fields) -> dict:
        """PATCH /agents/manage/{id} — requires user_admin + agents.create."""
        r = self._http.patch(f"/agents/manage/{agent_id}", json=fields, headers=self._admin_headers("agents.create"))
        r.raise_for_status()
        return self._parse_json(r)

    def mgmt_issue_agent_pat(
        self,
        agent_id: str,
        *,
        name: str | None = None,
        expires_in_days: int = 90,
        audience: str = "cli",
    ) -> dict:
        """POST /credentials/agent-pat — requires user_admin + credentials.issue.agent."""
        body = {"agent_id": agent_id, "expires_in_days": expires_in_days, "audience": audience}
        if name:
            body["name"] = name
        r = self._http.post(
            "/credentials/agent-pat", json=body, headers=self._admin_headers("agents.create credentials.issue.agent")
        )
        r.raise_for_status()
        return self._parse_json(r)

    def mgmt_issue_enrollment(
        self,
        *,
        name: str | None = None,
        expires_in_hours: int = 1,
        audience: str = "cli",
    ) -> dict:
        """POST /credentials/enrollment — requires user_admin + credentials.issue.agent."""
        body = {"expires_in_hours": expires_in_hours, "audience": audience}
        if name:
            body["name"] = name
        r = self._http.post(
            "/credentials/enrollment", json=body, headers=self._admin_headers("agents.create credentials.issue.agent")
        )
        r.raise_for_status()
        return self._parse_json(r)

    def mgmt_revoke_credential(self, credential_id: str) -> dict:
        """DELETE /credentials/{id} — requires user_admin + credentials.revoke."""
        r = self._http.delete(f"/credentials/{credential_id}", headers=self._admin_headers("credentials.revoke"))
        r.raise_for_status()
        return self._parse_json(r)

    def mgmt_list_credentials(self) -> list[dict]:
        """GET /credentials — requires user_admin + credentials.issue.agent."""
        r = self._http.get("/credentials", headers=self._admin_headers("agents.create credentials.issue.agent"))
        r.raise_for_status()
        return self._parse_json(r)

    # --- SSE ---

    def connect_sse(
        self,
        *,
        space_id: str | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> httpx.Response:
        """GET /api/v1/sse/messages — returns streaming response.

        Usage:
            with client.connect_sse(space_id=space_id) as resp:
                for line in resp.iter_lines():
                    if line.startswith("data:"):
                        event = json.loads(line[5:])
        """
        # Use JWT for SSE token param when exchange auth is available
        sse_token = self._get_jwt() if self._exchanger else self.token
        params = {"token": sse_token}
        if space_id:
            params["space_id"] = space_id
        return self._http.stream(
            "GET",
            "/api/v1/sse/messages",
            params=params,
            timeout=timeout or httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0),
        )

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
