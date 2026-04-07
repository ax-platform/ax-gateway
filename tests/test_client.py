"""Tests for AxClient auth and token class selection."""
from unittest.mock import MagicMock, patch

import pytest

from ax_cli.client import AxClient


class TestTokenClassSelection:
    """Verify correct token class is requested based on PAT prefix + agent_id."""

    def test_user_pat_with_agent_id_uses_user_access(self, tmp_path, monkeypatch, mock_exchange):
        """User PATs (axp_u_) must always use user_access, even when agent_id is set.
        This was the bug: axp_u_ + agent_id triggered agent_access, which the server rejects.
        """
        mock_post = mock_exchange()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        client = AxClient(
            "https://example.com",
            "axp_u_UserKey.UserSecret",
            agent_id="some-agent-uuid",
        )
        # Trigger auth by making a request
        client._get_jwt()

        call_body = mock_post.call_args[1]["json"]
        assert call_body["requested_token_class"] == "user_access"
        assert "agent_id" not in call_body

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
