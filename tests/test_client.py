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
            json={"id": "task-123", "assignee_id": "target-agent"},
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

    def test_agent_pat_without_agent_id_uses_user_access(self, tmp_path, monkeypatch, mock_exchange):
        """Agent PAT without agent_id falls back to user_access."""
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
