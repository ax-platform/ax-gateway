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
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _mime_from_ext(ext: str) -> str | None:
    return _EXT_MIME.get(ext)


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


class AxClient:
    def __init__(self, base_url: str, token: str, *, agent_name: str | None = None, agent_id: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.agent_id = agent_id  # Used for exchange parameters, NOT headers (§13)

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
        - axp_a_ (agent-bound PAT) + agent_id → agent_access
        - axp_u_ (user PAT) → user_access always, even if agent_id is set
          (user PATs cannot exchange for agent_access — server returns 422)
        """
        is_agent_pat = self.token.startswith("axp_a_")
        if self.agent_id and is_agent_pat:
            return self._exchanger.get_token(
                "agent_access",
                agent_id=self.agent_id,
                scope="messages tasks context agents spaces search",
                force_refresh=force_refresh,
            )
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

    def _parse_json(self, r: httpx.Response) -> dict:
        """Parse JSON response, raising a clear error if HTML is returned."""
        content_type = r.headers.get("content-type", "")
        if "text/html" in content_type or r.text.lstrip().startswith("<!"):
            raise httpx.HTTPStatusError(
                f"Expected JSON but got HTML from {r.url} — the frontend may be catching this API route",
                request=r.request,
                response=r,
            )
        return r.json()

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

    def send_message(
        self,
        space_id: str,
        content: str,
        *,
        agent_id: str | None = None,
        channel: str = "main",
        parent_id: str | None = None,
        attachments: list[dict] | None = None,
    ) -> dict:
        """POST /api/v1/messages — explicit space_id required."""
        body: dict = {"content": content, "space_id": space_id, "channel": channel, "message_type": "text"}
        if parent_id:
            body["parent_id"] = parent_id
        if attachments:
            body["attachments"] = attachments
            body["metadata"] = {"accepted_attachments": attachments}
        r = self._http.post("/api/v1/messages", json=body, headers=self._with_agent(agent_id))
        r.raise_for_status()
        return self._parse_json(r)

    def upload_file(self, file_path: str) -> dict:
        """POST /api/v1/uploads — upload a local file.

        Uses a separate httpx client to avoid sending Content-Type: application/json
        on the multipart request.
        """
        path = Path(file_path).expanduser().resolve()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")

        content_type = (
            _mime_from_ext(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
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
                )
        r.raise_for_status()
        return self._parse_json(r)

    def list_messages(self, limit: int = 20, channel: str = "main", *, agent_id: str | None = None) -> dict:
        r = self._http.get(
            "/api/v1/messages", params={"limit": limit, "channel": channel}, headers=self._with_agent(agent_id)
        )
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
        agent_id: str | None = None,
    ) -> dict:
        """POST /api/v1/tasks — explicit space_id required."""
        body = {"title": title, "space_id": space_id, "priority": priority}
        if description:
            body["description"] = description
        r = self._http.post("/api/v1/tasks", json=body, headers=self._with_agent(agent_id))
        r.raise_for_status()
        return self._parse_json(r)

    def list_tasks(self, limit: int = 20, *, agent_id: str | None = None) -> dict:
        r = self._http.get("/api/v1/tasks", params={"limit": limit}, headers=self._with_agent(agent_id))
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

    def list_agents(self) -> dict:
        r = self._http.get("/api/v1/agents")
        r.raise_for_status()
        return self._parse_json(r)

    def get_agents_presence(self) -> dict:
        """GET /api/v1/agents/presence — bulk presence for all agents."""
        r = self._http.get("/api/v1/agents/presence")
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

    def create_key(self, name: str, *, allowed_agent_ids: list[str] | None = None) -> dict:
        body: dict = {"name": name}
        if allowed_agent_ids:
            body["agent_scope"] = "agents"
            body["allowed_agent_ids"] = allowed_agent_ids
        r = self._http.post("/api/v1/keys", json=body)
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
        """POST /agents/manage/create — requires user_admin + agents.create."""
        body: dict = {"name": name}
        for k in ("description", "system_prompt", "model", "space_id"):
            if k in kwargs and kwargs[k] is not None:
                body[k] = kwargs[k]
        r = self._http.post("/agents/manage/create", json=body, headers=self._admin_headers("agents.create"))
        r.raise_for_status()
        return self._parse_json(r)

    def mgmt_list_agents(self) -> list[dict]:
        """GET /agents/manage/list — requires user_admin + agents.create."""
        r = self._http.get("/agents/manage/list", headers=self._admin_headers("agents.create"))
        r.raise_for_status()
        return self._parse_json(r)

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
        """GET /api/sse/messages — returns streaming response.

        Usage:
            with client.connect_sse() as resp:
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
            "/api/sse/messages",
            params=params,
            timeout=timeout or httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0),
        )

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
