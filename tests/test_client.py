"""Tests for AxClient auth and token class selection."""

from unittest.mock import MagicMock

import httpx
import pytest

from ax_cli.client import AxClient, _mime_from_ext, _mime_from_filename


class TestTokenClassSelection:
    """Verify correct token class is requested based on PAT prefix + agent_id."""

    def test_user_pat_with_agent_id_is_blocked(self, tmp_path, monkeypatch, mock_exchange):
        """User PATs exchange to user JWTs, so an agent-bound profile must not use one."""
        mock_post = mock_exchange()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        client = AxClient(
            "https://example.com",
            "axp_u_UserKey.UserSecret",
            agent_id="some-agent-uuid",
        )
        with pytest.raises(SystemExit):
            client._get_jwt()

        mock_post.assert_not_called()

    def test_user_pat_with_agent_name_is_blocked(self, tmp_path, monkeypatch, mock_exchange):
        """Agent-name config plus user PAT is also an attribution boundary violation."""
        mock_post = mock_exchange()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        client = AxClient(
            "https://example.com",
            "axp_u_UserKey.UserSecret",
            agent_name="some-agent",
        )
        with pytest.raises(SystemExit):
            client._get_jwt()

        mock_post.assert_not_called()

    def test_agent_pat_with_agent_id_uses_agent_access(self, tmp_path, monkeypatch, mock_exchange):
        """Agent-bound PATs (axp_a_) with agent_id should use agent_access."""
        mock_post = mock_exchange()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        client = AxClient(
            "https://example.com",
            "axp_a_AgentKey.AgentSecret",
            agent_id="some-agent-uuid",
        )
        client._get_jwt()

        call_body = mock_post.call_args[1]["json"]
        assert call_body["requested_token_class"] == "agent_access"
        assert call_body["agent_id"] == "some-agent-uuid"

    def test_agent_pat_without_agent_id_falls_back_to_user_access(self, tmp_path, monkeypatch, mock_exchange):
        """Agent-bound PATs need configured agent_id before requesting agent_access."""
        mock_post = mock_exchange()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        client = AxClient(
            "https://example.com",
            "axp_a_AgentKey.AgentSecret",
        )
        client._get_jwt()

        call_body = mock_post.call_args[1]["json"]
        assert call_body["requested_token_class"] == "user_access"
        assert "agent_id" not in call_body

    def test_user_pat_without_agent_id_uses_user_access(self, tmp_path, monkeypatch, mock_exchange):
        """User PAT without agent_id → user_access."""
        mock_post = mock_exchange()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        client = AxClient(
            "https://example.com",
            "axp_u_UserKey.UserSecret",
        )
        client._get_jwt()

        call_body = mock_post.call_args[1]["json"]
        assert call_body["requested_token_class"] == "user_access"


def test_cli_mime_overrides_normalize_common_source_artifacts_to_safe_text():
    assert _mime_from_ext(".java") == "text/plain"
    assert _mime_from_ext(".go") == "text/plain"
    assert _mime_from_ext(".rs") == "text/plain"
    assert _mime_from_ext(".yaml") == "text/plain"
    assert _mime_from_ext(".sh") == "text/plain"
    assert _mime_from_filename("Dockerfile") == "text/plain"
    assert _mime_from_filename("Makefile") == "text/plain"


def test_connect_sse_uses_v1_route_and_explicit_space_id():
    client = AxClient("https://example.com", "legacy-token")
    client._http.stream = MagicMock(return_value="stream-response")

    result = client.connect_sse(space_id="space-123")

    assert result == "stream-response"
    call = client._http.stream.call_args
    assert call.args[:2] == ("GET", "/api/v1/sse/messages")
    assert call.kwargs["params"] == {"token": "legacy-token", "space_id": "space-123"}


def test_list_messages_passes_explicit_space_id():
    client = AxClient("https://example.com", "legacy-token")
    response = httpx.Response(
        200,
        json={"messages": []},
        request=httpx.Request("GET", "https://example.com/api/v1/messages"),
    )
    client._http.get = MagicMock(return_value=response)

    client.list_messages(limit=5, channel="main", space_id="space-123")

    assert client._http.get.call_args.args[0] == "/api/v1/messages"
    assert client._http.get.call_args.kwargs["params"] == {
        "limit": 5,
        "channel": "main",
        "space_id": "space-123",
    }


def test_list_messages_can_request_unread_and_mark_read():
    client = AxClient("https://example.com", "legacy-token")
    response = httpx.Response(
        200,
        json={"messages": [], "unread_count": 0},
        request=httpx.Request("GET", "https://example.com/api/v1/messages"),
    )
    client._http.get = MagicMock(return_value=response)

    client.list_messages(
        limit=5,
        channel="main",
        space_id="space-123",
        unread_only=True,
        mark_read=True,
    )

    assert client._http.get.call_args.kwargs["params"] == {
        "limit": 5,
        "channel": "main",
        "space_id": "space-123",
        "unread_only": "true",
        "mark_read": "true",
    }


def test_send_message_allows_metadata_and_message_type():
    client = AxClient("https://example.com", "legacy-token")
    response = httpx.Response(
        200,
        json={"id": "msg-1"},
        request=httpx.Request("POST", "https://example.com/api/v1/messages"),
    )
    client._http.post = MagicMock(return_value=response)

    client.send_message(
        "space-123",
        "context signal",
        channel="automation-alerts",
        metadata={"ui": {"widget": {"resource_uri": "ui://context/explorer"}}},
        message_type="system",
    )

    assert client._http.post.call_args.args[0] == "/api/v1/messages"
    assert client._http.post.call_args.kwargs["json"] == {
        "content": "context signal",
        "space_id": "space-123",
        "channel": "automation-alerts",
        "message_type": "system",
        "metadata": {"ui": {"widget": {"resource_uri": "ui://context/explorer"}}},
    }


def test_mark_message_read_calls_backend_read_endpoint():
    client = AxClient("https://example.com", "legacy-token")
    response = httpx.Response(
        200,
        json={"status": "success", "message_id": "msg-1"},
        request=httpx.Request("POST", "https://example.com/api/v1/messages/msg-1/read"),
    )
    client._http.post = MagicMock(return_value=response)

    assert client.mark_message_read("msg-1")["status"] == "success"
    assert client._http.post.call_args.args[0] == "/api/v1/messages/msg-1/read"


def test_mark_all_messages_read_calls_backend_endpoint():
    client = AxClient("https://example.com", "legacy-token")
    response = httpx.Response(
        200,
        json={"status": "success", "marked_read": 2},
        request=httpx.Request("POST", "https://example.com/api/v1/messages/mark-all-read"),
    )
    client._http.post = MagicMock(return_value=response)

    assert client.mark_all_messages_read()["marked_read"] == 2
    assert client._http.post.call_args.args[0] == "/api/v1/messages/mark-all-read"


def test_record_tool_call_posts_audit_payload():
    client = AxClient("https://example.com", "legacy-token", agent_id="agent-123", agent_name="codex")
    response = httpx.Response(
        202,
        json={"ok": True, "tool_call_id": "tool-1"},
        request=httpx.Request("POST", "https://example.com/api/v1/tool-calls"),
    )
    client._http.post = MagicMock(return_value=response)

    result = client.record_tool_call(
        tool_name="shell",
        tool_call_id="tool-1",
        space_id="space-123",
        tool_action="wc -c README.md",
        arguments={"command": "wc -c README.md"},
        initial_data={"output": "28358 README.md"},
        status="success",
        message_id="msg-1",
        correlation_id="msg-1",
    )

    assert result["tool_call_id"] == "tool-1"
    assert client._http.post.call_args.args[0] == "/api/v1/tool-calls"
    assert client._http.post.call_args.kwargs["json"] == {
        "tool_name": "shell",
        "tool_call_id": "tool-1",
        "status": "success",
        "space_id": "space-123",
        "tool_action": "wc -c README.md",
        "arguments": {"command": "wc -c README.md"},
        "initial_data": {"output": "28358 README.md"},
        "message_id": "msg-1",
        "correlation_id": "msg-1",
    }


def test_set_agent_processing_status_includes_optional_fields():
    client = AxClient("https://example.com", "legacy-token", agent_id="agent-123", agent_name="codex")
    response = httpx.Response(
        200,
        json={"ok": True, "event": "agent_processing", "status": "processing"},
        request=httpx.Request("POST", "https://example.com/api/v1/agents/processing-status"),
    )
    client._http.post = MagicMock(return_value=response)

    result = client.set_agent_processing_status(
        "msg-1",
        "processing",
        agent_name="codex",
        space_id="space-123",
        activity="Running command",
        tool_name="shell",
        progress={"current": 1, "total": 3, "unit": "steps"},
        detail={"command": "pwd"},
        reason="gateway_runtime",
        error_message=None,
        retry_after_seconds=5,
        parent_message_id="parent-1",
    )

    assert result["status"] == "processing"
    assert client._http.post.call_args.args[0] == "/api/v1/agents/processing-status"
    assert client._http.post.call_args.kwargs["json"] == {
        "message_id": "msg-1",
        "status": "processing",
        "agent_name": "codex",
        "activity": "Running command",
        "tool_name": "shell",
        "progress": {"current": 1, "total": 3, "unit": "steps"},
        "detail": {"command": "pwd"},
        "reason": "gateway_runtime",
        "retry_after_seconds": 5,
        "parent_message_id": "parent-1",
    }


def test_set_agent_processing_status_posts_rich_payload():
    client = AxClient("https://example.com", "legacy-token", agent_id="agent-123", agent_name="codex")
    response = httpx.Response(
        202,
        json={"ok": True},
        request=httpx.Request("POST", "https://example.com/api/v1/agents/processing-status"),
    )
    client._http.post = MagicMock(return_value=response)

    result = client.set_agent_processing_status(
        "msg-1",
        "tool_call",
        agent_name="codex",
        space_id="space-123",
        activity="Running tests",
        tool_name="shell",
        progress={"current": 1, "total": 3, "unit": "steps"},
        detail={"command": "pytest tests/test_gateway_commands.py"},
        reason="tool started",
        error_message="",
        retry_after_seconds=5,
        parent_message_id="parent-1",
    )

    assert result["ok"] is True
    assert client._http.post.call_args.args[0] == "/api/v1/agents/processing-status"
    assert client._http.post.call_args.kwargs["json"] == {
        "message_id": "msg-1",
        "status": "tool_call",
        "agent_name": "codex",
        "activity": "Running tests",
        "tool_name": "shell",
        "progress": {"current": 1, "total": 3, "unit": "steps"},
        "detail": {"command": "pytest tests/test_gateway_commands.py"},
        "reason": "tool started",
        "error_message": "",
        "retry_after_seconds": 5,
        "parent_message_id": "parent-1",
    }


def test_list_tasks_passes_explicit_space_id():
    client = AxClient("https://example.com", "legacy-token")
    response = httpx.Response(
        200,
        json={"tasks": []},
        request=httpx.Request("GET", "https://example.com/api/v1/tasks"),
    )
    client._http.get = MagicMock(return_value=response)

    client.list_tasks(limit=7, space_id="space-123")

    assert client._http.get.call_args.args[0] == "/api/v1/tasks"
    assert client._http.get.call_args.kwargs["params"] == {
        "limit": 7,
        "space_id": "space-123",
    }


def test_list_agents_passes_explicit_space_id_and_limit():
    client = AxClient("https://example.com", "legacy-token")
    response = httpx.Response(
        200,
        json={"agents": []},
        request=httpx.Request("GET", "https://example.com/api/v1/agents"),
    )
    client._http.get = MagicMock(return_value=response)

    client.list_agents(space_id="space-123", limit=500)

    assert client._http.get.call_args.args[0] == "/api/v1/agents"
    assert client._http.get.call_args.kwargs["params"] == {
        "space_id": "space-123",
        "limit": 500,
    }


class TestCredentialManagement:
    """Verify credential management request payloads."""

    def _response(self, method: str, url: str, status_code: int, *, json=None, text: str | None = None):
        request = httpx.Request(method, url)
        if text is not None:
            return httpx.Response(
                status_code,
                text=text,
                headers={"content-type": "text/html; charset=utf-8"},
                request=request,
            )
        return httpx.Response(status_code, json=json or {}, request=request)

    def test_create_key_with_allowed_agents_sets_agent_scope(self):
        client = AxClient("https://example.com", "axp_u_UserKey.UserSecret")
        response = httpx.Response(
            201,
            json={"ok": True},
            request=httpx.Request("POST", "https://example.com/api/v1/keys"),
        )
        client._http.post = MagicMock(return_value=response)

        client.create_key("agent-key", allowed_agent_ids=["agent-123"])

        body = client._http.post.call_args.kwargs["json"]
        assert body["agent_scope"] == "agents"
        assert body["allowed_agent_ids"] == ["agent-123"]

    def test_create_task_sends_assignee_id_in_body(self):
        client = AxClient("https://example.com", "legacy-token", agent_id="creator-agent")
        response = httpx.Response(
            201,
            json={"id": "task-123", "space_id": "space-123", "assignee_id": "target-agent"},
            request=httpx.Request("POST", "https://example.com/api/v1/tasks"),
        )
        client._http.post = MagicMock(return_value=response)

        client.create_task(
            "space-123",
            "Review the spec",
            priority="medium",
            assignee_id="target-agent",
        )

        body = client._http.post.call_args.kwargs["json"]
        assert body["space_id"] == "space-123"
        assert body["assignee_id"] == "target-agent"

    def test_create_task_falls_back_from_hosted_html_and_verifies_space(self):
        client = AxClient("https://example.com", "legacy-token", agent_id="creator-agent")
        html_response = httpx.Response(
            200,
            text="<!doctype html><html></html>",
            headers={"content-type": "text/html; charset=utf-8"},
            request=httpx.Request("POST", "https://example.com/api/v1/tasks"),
        )
        create_response = httpx.Response(
            201,
            json={"id": "task-123", "title": "Review the spec"},
            request=httpx.Request("POST", "https://example.com/api/tasks"),
        )
        whoami_response = httpx.Response(
            200,
            json={"resolved_space_id": "space-123"},
            request=httpx.Request("GET", "https://example.com/auth/me"),
        )
        list_response = httpx.Response(
            200,
            json={"tasks": [{"id": "task-123", "space_id": "space-123"}]},
            request=httpx.Request("GET", "https://example.com/api/v1/tasks?limit=100&space_id=space-123"),
        )
        client._http.post = MagicMock(side_effect=[html_response, create_response])
        client._http.get = MagicMock(side_effect=[whoami_response, list_response])

        data = client.create_task("space-123", "Review the spec", priority="high")

        assert data["id"] == "task-123"
        assert client._http.post.call_args_list[0].args[0] == "/api/v1/tasks"
        assert client._http.post.call_args_list[1].args[0] == "/api/tasks"
        assert client._http.get.call_args_list[0].args[0] == "/auth/me"
        assert client._http.get.call_args_list[1].kwargs["params"] == {"limit": 100, "space_id": "space-123"}

    def test_create_task_fallback_refuses_when_session_space_differs(self):
        client = AxClient("https://example.com", "legacy-token", agent_id="creator-agent")
        html_response = httpx.Response(
            200,
            text="<!doctype html><html></html>",
            headers={"content-type": "text/html; charset=utf-8"},
            request=httpx.Request("POST", "https://example.com/api/v1/tasks"),
        )
        whoami_response = httpx.Response(
            200,
            json={"resolved_space_id": "madtank-space"},
            request=httpx.Request("GET", "https://example.com/auth/me"),
        )
        client._http.post = MagicMock(return_value=html_response)
        client._http.get = MagicMock(return_value=whoami_response)

        with pytest.raises(RuntimeError, match="Refusing to create the task"):
            client.create_task("ax-cli-dev-space", "Review the spec", priority="high")

        assert client._http.post.call_count == 1

    def test_create_task_fallback_rejects_unverified_space(self):
        client = AxClient("https://example.com", "legacy-token", agent_id="creator-agent")
        html_response = httpx.Response(
            200,
            text="<!doctype html><html></html>",
            headers={"content-type": "text/html; charset=utf-8"},
            request=httpx.Request("POST", "https://example.com/api/v1/tasks"),
        )
        create_response = httpx.Response(
            201,
            json={"id": "task-123", "title": "Review the spec"},
            request=httpx.Request("POST", "https://example.com/api/tasks"),
        )
        whoami_response = httpx.Response(
            200,
            json={"bound_agent": {"default_space_id": "space-123"}},
            request=httpx.Request("GET", "https://example.com/auth/me"),
        )
        list_response = httpx.Response(
            200,
            json={"tasks": [{"id": "other-task", "space_id": "space-123"}]},
            request=httpx.Request("GET", "https://example.com/api/v1/tasks?limit=100&space_id=space-123"),
        )
        client._http.post = MagicMock(side_effect=[html_response, create_response])
        client._http.get = MagicMock(side_effect=[whoami_response, list_response])

        with pytest.raises(RuntimeError, match="not visible in requested space"):
            client.create_task("space-123", "Review the spec", priority="high")

    def test_issue_agent_pat_sends_requested_audience(self):
        client = AxClient("https://example.com", "axp_u_UserKey.UserSecret")
        client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
        response = httpx.Response(
            201,
            json={"ok": True},
            request=httpx.Request("POST", "https://example.com/credentials/agent-pat"),
        )
        client._http.post = MagicMock(return_value=response)

        client.mgmt_issue_agent_pat("agent-123", audience="mcp")

        body = client._http.post.call_args.kwargs["json"]
        assert body["audience"] == "mcp"

    def test_issue_enrollment_sends_requested_audience(self):
        client = AxClient("https://example.com", "axp_u_UserKey.UserSecret")
        client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
        response = httpx.Response(
            201,
            json={"ok": True},
            request=httpx.Request("POST", "https://example.com/credentials/enrollment"),
        )
        client._http.post = MagicMock(return_value=response)

        client.mgmt_issue_enrollment(audience="both")

        body = client._http.post.call_args.kwargs["json"]
        assert body["audience"] == "both"

    def test_mgmt_create_agent_prefers_api_v1_route(self):
        client = AxClient("https://example.com", "axp_u_UserKey.UserSecret")
        client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
        client._http.post = MagicMock(
            return_value=self._response(
                "POST",
                "https://example.com/api/v1/agents/manage/create",
                201,
                json={"agent": {"id": "agent-123", "name": "new-agent"}},
            )
        )

        result = client.mgmt_create_agent("new-agent")

        assert result["agent"]["id"] == "agent-123"
        assert client._http.post.call_args.args[0] == "/api/v1/agents/manage/create"

    def test_mgmt_create_agent_falls_back_to_legacy_route_on_route_miss(self):
        client = AxClient("https://example.com", "axp_u_UserKey.UserSecret")
        client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
        client._http.post = MagicMock(
            side_effect=[
                self._response(
                    "POST",
                    "https://example.com/api/v1/agents/manage/create",
                    404,
                    json={"detail": "Not Found"},
                ),
                self._response(
                    "POST",
                    "https://example.com/agents/manage/create",
                    201,
                    json={"agent": {"id": "agent-123", "name": "new-agent"}},
                ),
            ]
        )

        result = client.mgmt_create_agent("new-agent")

        assert result["agent"]["id"] == "agent-123"
        assert [call.args[0] for call in client._http.post.call_args_list] == [
            "/api/v1/agents/manage/create",
            "/agents/manage/create",
        ]

    def test_mgmt_create_agent_falls_back_when_frontend_catches_route(self):
        client = AxClient("https://example.com", "axp_u_UserKey.UserSecret")
        client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
        client._http.post = MagicMock(
            side_effect=[
                self._response(
                    "POST",
                    "https://example.com/api/v1/agents/manage/create",
                    200,
                    text="<!DOCTYPE html><html></html>",
                ),
                self._response(
                    "POST",
                    "https://example.com/agents/manage/create",
                    201,
                    json={"agent": {"id": "agent-123", "name": "new-agent"}},
                ),
            ]
        )

        result = client.mgmt_create_agent("new-agent")

        assert result["agent"]["id"] == "agent-123"
        assert [call.args[0] for call in client._http.post.call_args_list] == [
            "/api/v1/agents/manage/create",
            "/agents/manage/create",
        ]

    def test_mgmt_create_agent_does_not_fallback_on_auth_failure(self):
        client = AxClient("https://example.com", "axp_u_UserKey.UserSecret")
        client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
        client._http.post = MagicMock(
            return_value=self._response(
                "POST",
                "https://example.com/api/v1/agents/manage/create",
                401,
                json={"detail": "Not authenticated"},
            )
        )

        with pytest.raises(httpx.HTTPStatusError):
            client.mgmt_create_agent("new-agent")

        assert client._http.post.call_count == 1

    def test_mgmt_list_agents_falls_back_to_legacy_route_on_route_miss(self):
        client = AxClient("https://example.com", "axp_u_UserKey.UserSecret")
        client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
        client._http.get = MagicMock(
            side_effect=[
                self._response(
                    "GET",
                    "https://example.com/api/v1/agents/manage/list",
                    405,
                    json={"detail": "Method Not Allowed"},
                ),
                self._response(
                    "GET",
                    "https://example.com/agents/manage/list",
                    200,
                    json=[{"id": "agent-123", "name": "new-agent"}],
                ),
            ]
        )

        result = client.mgmt_list_agents()

        assert result == [{"id": "agent-123", "name": "new-agent"}]
        assert [call.args[0] for call in client._http.get.call_args_list] == [
            "/api/v1/agents/manage/list",
            "/agents/manage/list",
        ]
