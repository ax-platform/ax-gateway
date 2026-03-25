"""aX Platform API Client.

API is source of truth. Every write operation requires explicit space_id.

Usage:
    client = AxClient("http://localhost:8001", "axp_u_...")
    me = client.whoami()
    space_id = me["space_id"]  # or from client.list_spaces()
    msg = client.send_message(space_id, "hello")
    client.send_message(space_id, "do this", agent_id="<uuid>")
"""
import json

import httpx


class AxClient:
    def __init__(self, base_url: str, token: str, *, agent_name: str | None = None, agent_id: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._headers.update(self._agent_headers(agent_name=agent_name, agent_id=agent_id))
        self._http = httpx.Client(
            base_url=self.base_url, headers=self._headers, timeout=30.0,
        )

    @staticmethod
    def _agent_headers(*, agent_name: str | None = None, agent_id: str | None = None) -> dict[str, str]:
        """Return exactly one agent identity header.

        ID is canonical after bind and wins over name.
        Name is only used during bootstrap (no ID yet) or explicit interactive targeting.
        """
        if agent_id:
            return {"X-Agent-Id": agent_id}
        if agent_name:
            return {"X-Agent-Name": agent_name}
        return {}

    def set_default_agent(self, *, agent_name: str | None = None, agent_id: str | None = None) -> None:
        """Update default agent identity headers for subsequent requests."""
        self._headers.pop("X-Agent-Name", None)
        self._headers.pop("X-Agent-Id", None)
        self._headers.update(self._agent_headers(agent_name=agent_name, agent_id=agent_id))
        self._http.headers.clear()
        self._http.headers.update(self._headers)

    def _with_agent(self, agent_id: str | None = None, *, agent_name: str | None = None) -> dict:
        """Return request headers with an optional explicit agent override."""
        if agent_name or agent_id:
            return {
                **{
                    k: v
                    for k, v in self._headers.items()
                    if k not in {"X-Agent-Name", "X-Agent-Id"}
                },
                **self._agent_headers(agent_name=agent_name, agent_id=agent_id),
            }
        return self._headers

    def _parse_json(self, r: httpx.Response):
        """Raise-for-status then safely parse JSON, with a clear error for non-JSON responses."""
        r.raise_for_status()
        content_type = r.headers.get("content-type", "")
        if "json" not in content_type:
            raise httpx.HTTPStatusError(
                f"Expected JSON but got '{content_type}' from {r.url} "
                "— check your base_url and agent config",
                request=r.request,
                response=r,
            )
        return r.json()

    # --- Identity ---

    def whoami(self) -> dict:
        """GET /auth/me — returns user identity."""
        r = self._http.get("/auth/me")
        return self._parse_json(r)

    # --- Spaces ---

    def list_spaces(self) -> list[dict]:
        r = self._http.get("/api/v1/spaces")
        return self._parse_json(r)

    def get_space(self, space_id: str) -> dict:
        r = self._http.get(f"/api/v1/spaces/{space_id}")
        return self._parse_json(r)

    def list_space_members(self, space_id: str) -> list[dict]:
        r = self._http.get(f"/api/v1/spaces/{space_id}/members")
        return self._parse_json(r)

    # --- Messages ---

    def send_message(self, space_id: str, content: str, *,
                     agent_id: str | None = None,
                     channel: str = "main",
                     parent_id: str | None = None) -> dict:
        """POST /api/v1/messages — explicit space_id required."""
        body = {"content": content, "space_id": space_id,
                "channel": channel, "message_type": "text"}
        if parent_id:
            body["parent_id"] = parent_id
        r = self._http.post("/api/v1/messages", json=body,
                            headers=self._with_agent(agent_id))
        return self._parse_json(r)

    def list_messages(self, limit: int = 20, channel: str = "main", *,
                      agent_id: str | None = None) -> dict:
        r = self._http.get("/api/v1/messages",
                           params={"limit": limit, "channel": channel},
                           headers=self._with_agent(agent_id))
        return self._parse_json(r)

    def get_message(self, message_id: str) -> dict:
        r = self._http.get(f"/api/v1/messages/{message_id}")
        return self._parse_json(r)

    def edit_message(self, message_id: str, content: str) -> dict:
        r = self._http.patch(f"/api/v1/messages/{message_id}",
                             json={"content": content})
        return self._parse_json(r)

    def delete_message(self, message_id: str) -> int:
        r = self._http.delete(f"/api/v1/messages/{message_id}")
        r.raise_for_status()
        return r.status_code

    def add_reaction(self, message_id: str, emoji: str) -> dict:
        r = self._http.post(f"/api/v1/messages/{message_id}/reactions",
                            json={"emoji": emoji})
        return self._parse_json(r)

    def list_replies(self, message_id: str) -> dict:
        r = self._http.get(f"/api/v1/messages/{message_id}/replies")
        return self._parse_json(r)

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
        return self._parse_json(r)

    def list_tasks(self, limit: int = 20, *, agent_id: str | None = None) -> dict:
        r = self._http.get("/api/v1/tasks", params={"limit": limit},
                           headers=self._with_agent(agent_id))
        return self._parse_json(r)

    def get_task(self, task_id: str) -> dict:
        r = self._http.get(f"/api/v1/tasks/{task_id}")
        return self._parse_json(r)

    def update_task(self, task_id: str, **fields) -> dict:
        r = self._http.patch(f"/api/v1/tasks/{task_id}", json=fields)
        return self._parse_json(r)

    # --- Agents ---

    def list_agents(self) -> dict:
        r = self._http.get("/api/v1/agents")
        return self._parse_json(r)

    def get_agents_presence(self) -> dict:
        """GET /api/v1/agents/presence — bulk presence for all agents."""
        r = self._http.get("/api/v1/agents/presence")
        return self._parse_json(r)

    def create_agent(self, name: str, **kwargs) -> dict:
        """POST /api/v1/agents — create a new agent."""
        body: dict = {"name": name}
        for key in ("description", "system_prompt", "model", "space_id",
                     "enable_cloud_agent", "can_manage_agents"):
            if key in kwargs and kwargs[key] is not None:
                body[key] = kwargs[key]
        r = self._http.post("/api/v1/agents", json=body)
        return self._parse_json(r)

    def get_agent(self, identifier: str) -> dict:
        """GET /api/v1/agents/manage/{identifier} — get by name or UUID."""
        r = self._http.get(f"/api/v1/agents/manage/{identifier}")
        return self._parse_json(r)

    def update_agent(self, identifier: str, **fields) -> dict:
        """PUT /api/v1/agents/manage/{identifier} — update agent."""
        r = self._http.put(f"/api/v1/agents/manage/{identifier}", json=fields)
        return self._parse_json(r)

    def delete_agent(self, identifier: str) -> dict:
        """DELETE /api/v1/agents/manage/{identifier} — delete agent."""
        r = self._http.delete(f"/api/v1/agents/manage/{identifier}")
        return self._parse_json(r)

    def get_agent_tools(self, space_id: str, agent_id: str) -> dict:
        """GET /{space_id}/roster filtered to one agent — returns enabled_tools."""
        r = self._http.get(
            f"/api/v1/organizations/{space_id}/roster",
            params={"entry_type": "agent"},
        )
        roster = self._parse_json(r)
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
        return self._parse_json(r)

    def get_context(self, key: str) -> dict:
        r = self._http.get(f"/api/v1/context/{key}")
        return self._parse_json(r)

    def list_context(self, prefix: str | None = None) -> dict:
        params = {}
        if prefix:
            params["prefix"] = prefix
        r = self._http.get("/api/v1/context", params=params)
        return self._parse_json(r)

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
        return self._parse_json(r)

    # --- Keys (PAT / delegated agent token management) ---

    def create_key(self, name: str, *,
                   allowed_agent_ids: list[str] | None = None,
                   agent_scope: str | None = None,
                   agent_id: str | None = None) -> dict:
        body: dict = {"name": name}
        if agent_scope:
            body["agent_scope"] = agent_scope
        if allowed_agent_ids:
            body["allowed_agent_ids"] = allowed_agent_ids
        if agent_id:
            body["agent_id"] = agent_id
        r = self._http.post("/api/v1/keys", json=body)
        return self._parse_json(r)

    def list_keys(self) -> list[dict]:
        r = self._http.get("/api/v1/keys")
        return self._parse_json(r)

    def revoke_key(self, credential_id: str) -> int:
        r = self._http.delete(f"/api/v1/keys/{credential_id}")
        return r.status_code

    def rotate_key(self, credential_id: str) -> dict:
        r = self._http.post(f"/api/v1/keys/{credential_id}/rotate")
        return self._parse_json(r)

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
            timeout=None,
        )

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
