"""aX Platform API Client.

API is source of truth. Every write operation requires explicit space_id.

Usage:
    client = AxClient("http://localhost:8001", "axp_u_...")
    me = client.whoami()
    space_id = me["space_id"]  # or from client.list_spaces()
    msg = client.send_message(space_id, "hello")
    client.send_message(space_id, "do this", agent_id="<uuid>")
"""
import mimetypes
from pathlib import Path

import httpx


class AxClient:
    def __init__(self, base_url: str, token: str, *, agent_name: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if agent_name:
            self._headers["X-Agent-Name"] = agent_name
        self._http = httpx.Client(
            base_url=self.base_url, headers=self._headers, timeout=30.0,
        )

    def _with_agent(self, agent_id: str | None) -> dict:
        """Add X-Agent-Id header if targeting an agent."""
        if agent_id:
            return {**self._headers, "X-Agent-Id": agent_id}
        return self._headers

    # --- Identity ---

    def whoami(self) -> dict:
        """GET /auth/me — returns user identity."""
        r = self._http.get("/auth/me")
        r.raise_for_status()
        return r.json()

    # --- Spaces ---

    def list_spaces(self) -> list[dict]:
        r = self._http.get("/api/v1/spaces")
        r.raise_for_status()
        return r.json()

    def get_space(self, space_id: str) -> dict:
        r = self._http.get(f"/api/v1/spaces/{space_id}")
        r.raise_for_status()
        return r.json()

    def list_space_members(self, space_id: str) -> list[dict]:
        r = self._http.get(f"/api/v1/spaces/{space_id}/members")
        r.raise_for_status()
        return r.json()

    # --- Messages ---

    def send_message(self, space_id: str, content: str, *,
                     agent_id: str | None = None,
                     channel: str = "main",
                     parent_id: str | None = None,
                     attachments: list[dict] | None = None) -> dict:
        """POST /api/v1/messages — explicit space_id required."""
        body = {"content": content, "space_id": space_id,
                "channel": channel, "message_type": "text"}
        if parent_id:
            body["parent_id"] = parent_id
        if attachments:
            body["attachments"] = attachments
        r = self._http.post("/api/v1/messages", json=body,
                            headers=self._with_agent(agent_id))
        r.raise_for_status()
        return r.json()

    def upload_file(self, file_path: str) -> dict:
        """POST /api/v1/uploads — upload a local file.

        Uses a separate httpx client to avoid sending Content-Type: application/json
        on the multipart request.
        """
        path = Path(file_path).expanduser().resolve()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        headers = {k: v for k, v in self._headers.items() if k != "Content-Type"}

        with path.open("rb") as fh:
            with httpx.Client(
                base_url=self.base_url, headers=headers, timeout=60.0,
                follow_redirects=True,
            ) as upload_http:
                r = upload_http.post(
                    "/api/v1/uploads/",
                    files={"file": (path.name, fh, content_type)},
                )
        r.raise_for_status()
        return r.json()

    def list_messages(self, limit: int = 20, channel: str = "main", *,
                      agent_id: str | None = None) -> dict:
        r = self._http.get("/api/v1/messages",
                           params={"limit": limit, "channel": channel},
                           headers=self._with_agent(agent_id))
        r.raise_for_status()
        return r.json()

    def get_message(self, message_id: str) -> dict:
        r = self._http.get(f"/api/v1/messages/{message_id}")
        r.raise_for_status()
        return r.json()

    def edit_message(self, message_id: str, content: str) -> dict:
        r = self._http.patch(f"/api/v1/messages/{message_id}",
                             json={"content": content})
        r.raise_for_status()
        return r.json()

    def delete_message(self, message_id: str) -> int:
        r = self._http.delete(f"/api/v1/messages/{message_id}")
        r.raise_for_status()
        return r.status_code

    def add_reaction(self, message_id: str, emoji: str) -> dict:
        r = self._http.post(f"/api/v1/messages/{message_id}/reactions",
                            json={"emoji": emoji})
        r.raise_for_status()
        return r.json()

    def list_replies(self, message_id: str) -> dict:
        r = self._http.get(f"/api/v1/messages/{message_id}/replies")
        r.raise_for_status()
        return r.json()

    # --- Tasks ---

    def create_task(self, space_id: str, title: str, *,
                    description: str | None = None,
                    priority: str = "medium",
                    agent_id: str | None = None) -> dict:
        """POST /api/v1/tasks — explicit space_id required."""
        body = {"title": title, "space_id": space_id, "priority": priority}
        if description:
            body["description"] = description
        r = self._http.post("/api/v1/tasks", json=body,
                            headers=self._with_agent(agent_id))
        r.raise_for_status()
        return r.json()

    def list_tasks(self, limit: int = 20, *, agent_id: str | None = None) -> dict:
        r = self._http.get("/api/v1/tasks", params={"limit": limit},
                           headers=self._with_agent(agent_id))
        r.raise_for_status()
        return r.json()

    def get_task(self, task_id: str) -> dict:
        r = self._http.get(f"/api/v1/tasks/{task_id}")
        r.raise_for_status()
        return r.json()

    def update_task(self, task_id: str, **fields) -> dict:
        r = self._http.patch(f"/api/v1/tasks/{task_id}", json=fields)
        r.raise_for_status()
        return r.json()

    # --- Agents ---

    def list_agents(self) -> dict:
        r = self._http.get("/api/v1/agents")
        r.raise_for_status()
        return r.json()

    def get_agents_presence(self) -> dict:
        """GET /api/v1/agents/presence — bulk presence for all agents."""
        r = self._http.get("/api/v1/agents/presence")
        r.raise_for_status()
        return r.json()

    def create_agent(self, name: str, **kwargs) -> dict:
        """POST /api/v1/agents — create a new agent."""
        body: dict = {"name": name}
        for key in ("description", "system_prompt", "model", "space_id",
                     "enable_cloud_agent", "can_manage_agents"):
            if key in kwargs and kwargs[key] is not None:
                body[key] = kwargs[key]
        r = self._http.post("/api/v1/agents", json=body)
        r.raise_for_status()
        return r.json()

    def get_agent(self, identifier: str) -> dict:
        """GET /api/v1/agents/manage/{identifier} — get by name or UUID."""
        r = self._http.get(f"/api/v1/agents/manage/{identifier}")
        r.raise_for_status()
        return r.json()

    def update_agent(self, identifier: str, **fields) -> dict:
        """PUT /api/v1/agents/manage/{identifier} — update agent."""
        r = self._http.put(f"/api/v1/agents/manage/{identifier}", json=fields)
        r.raise_for_status()
        return r.json()

    def delete_agent(self, identifier: str) -> dict:
        """DELETE /api/v1/agents/manage/{identifier} — delete agent."""
        r = self._http.delete(f"/api/v1/agents/manage/{identifier}")
        r.raise_for_status()
        return r.json()

    def get_agent_tools(self, space_id: str, agent_id: str) -> dict:
        """GET /{space_id}/roster filtered to one agent — returns enabled_tools."""
        r = self._http.get(
            f"/api/v1/organizations/{space_id}/roster",
            params={"entry_type": "agent"},
        )
        r.raise_for_status()
        roster = r.json()
        entries = roster.get("entries", roster) if isinstance(roster, dict) else roster
        for entry in (entries if isinstance(entries, list) else []):
            if str(entry.get("id")) == agent_id:
                return {
                    "agent_id": agent_id,
                    "name": entry.get("name"),
                    "enabled_tools": entry.get("enabled_tools"),
                    "capabilities": entry.get("capabilities_list"),
                }
        return {"agent_id": agent_id, "enabled_tools": None, "error": "not_found"}

    # --- Context ---

    def set_context(self, space_id: str, key: str, value: str, *,
                    ttl: int | None = None) -> dict:
        """POST /api/v1/context — explicit space_id required."""
        body = {"key": key, "value": value, "space_id": space_id}
        if ttl:
            body["ttl"] = ttl
        r = self._http.post("/api/v1/context", json=body)
        r.raise_for_status()
        return r.json()

    def get_context(self, key: str) -> dict:
        r = self._http.get(f"/api/v1/context/{key}")
        r.raise_for_status()
        return r.json()

    def list_context(self, prefix: str | None = None) -> dict:
        params = {}
        if prefix:
            params["prefix"] = prefix
        r = self._http.get("/api/v1/context", params=params)
        r.raise_for_status()
        return r.json()

    def delete_context(self, key: str) -> int:
        r = self._http.delete(f"/api/v1/context/{key}")
        r.raise_for_status()
        return r.status_code

    # --- Search ---

    def search_messages(self, query: str, limit: int = 20, *,
                        agent_id: str | None = None) -> dict:
        r = self._http.post("/api/v1/search/messages",
                            json={"query": query, "limit": limit},
                            headers=self._with_agent(agent_id))
        r.raise_for_status()
        return r.json()

    # --- Keys (PAT management) ---

    def create_key(self, name: str, *,
                   allowed_agent_ids: list[str] | None = None) -> dict:
        body: dict = {"name": name}
        if allowed_agent_ids:
            body["allowed_agent_ids"] = allowed_agent_ids
        r = self._http.post("/api/v1/keys", json=body)
        r.raise_for_status()
        return r.json()

    def list_keys(self) -> list[dict]:
        r = self._http.get("/api/v1/keys")
        r.raise_for_status()
        return r.json()

    def revoke_key(self, credential_id: str) -> int:
        r = self._http.delete(f"/api/v1/keys/{credential_id}")
        return r.status_code

    def rotate_key(self, credential_id: str) -> dict:
        r = self._http.post(f"/api/v1/keys/{credential_id}/rotate")
        r.raise_for_status()
        return r.json()

    # --- SSE ---

    def connect_sse(self) -> httpx.Response:
        """GET /api/v1/sse/messages — returns streaming response.

        Usage:
            with client.connect_sse() as resp:
                for line in resp.iter_lines():
                    if line.startswith("data:"):
                        event = json.loads(line[5:])
        """
        return self._http.stream(
            "GET", "/api/v1/sse/messages",
            params={"token": self.token},
            timeout=httpx.Timeout(connect=10.0, read=90.0, write=10.0, pool=10.0),
        )

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
