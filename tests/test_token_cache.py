"""Tests for token exchange and caching (AUTH-SPEC-001 §13)."""

import pytest

from ax_cli.token_cache import (
    TokenExchanger,
    _cache_key,
    _extract_key_id,
)


class TestExtractKeyId:
    def test_user_pat(self):
        assert _extract_key_id("axp_u_TestKey.SecretPart") == "TestKey"

    def test_agent_pat(self):
        assert _extract_key_id("axp_a_AgentKey.SecretPart") == "AgentKey"

    def test_long_key_id(self):
        assert _extract_key_id("axp_u_93C7bk2KNK.v9zx-Zx7ZbTpGid") == "93C7bk2KNK"

    def test_invalid_prefix(self):
        assert _extract_key_id("not_a_pat") is None

    def test_no_dot_separator(self):
        assert _extract_key_id("axp_u_NoDotHere") is None

    def test_dot_at_start(self):
        assert _extract_key_id("axp_u_.JustSecret") is None


class TestCacheKey:
    def test_deterministic(self):
        k1 = _cache_key("key1", "user_access", None, "ax-api", "messages")
        k2 = _cache_key("key1", "user_access", None, "ax-api", "messages")
        assert k1 == k2

    def test_different_for_different_inputs(self):
        k1 = _cache_key("key1", "user_access", None, "ax-api", "messages")
        k2 = _cache_key("key1", "agent_access", "agent-123", "ax-api", "messages")
        assert k1 != k2

    def test_agent_id_matters(self):
        k1 = _cache_key("key1", "agent_access", "agent-A", "ax-api", "messages")
        k2 = _cache_key("key1", "agent_access", "agent-B", "ax-api", "messages")
        assert k1 != k2

    def test_none_agent_id_is_consistent(self):
        k1 = _cache_key("key1", "user_access", None, "ax-api", "messages")
        k2 = _cache_key("key1", "user_access", None, "ax-api", "messages")
        assert k1 == k2

    def test_key_length(self):
        k = _cache_key("key1", "user_access", None, "ax-api", "messages")
        assert len(k) == 24  # truncated SHA-256


class TestTokenExchanger:
    def test_exchange_calls_api(self, tmp_path, monkeypatch, sample_pat, mock_exchange):
        mock_post = mock_exchange()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        exchanger = TokenExchanger("https://example.com", sample_pat)
        token = exchanger.get_token("user_access")

        assert token == "fake.jwt.token"
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert "auth/exchange" in call_kwargs[0][0]
        assert call_kwargs[1]["json"]["requested_token_class"] == "user_access"

    def test_caches_token(self, tmp_path, monkeypatch, sample_pat, mock_exchange):
        mock_post = mock_exchange()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        exchanger = TokenExchanger("https://example.com", sample_pat)
        token1 = exchanger.get_token("user_access")
        token2 = exchanger.get_token("user_access")

        assert token1 == token2
        assert mock_post.call_count == 1  # only one exchange call

    def test_force_refresh_bypasses_cache(self, tmp_path, monkeypatch, sample_pat, mock_exchange):
        mock_post = mock_exchange()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        exchanger = TokenExchanger("https://example.com", sample_pat)
        exchanger.get_token("user_access")
        exchanger.get_token("user_access", force_refresh=True)

        assert mock_post.call_count == 2

    def test_different_token_classes_not_shared(self, tmp_path, monkeypatch, sample_pat, mock_exchange):
        call_count = 0

        def make_response(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            from unittest.mock import MagicMock

            import httpx
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = {
                "access_token": f"token-{call_count}",
                "expires_in": 900,
            }
            resp.raise_for_status = MagicMock()
            return resp

        import httpx
        monkeypatch.setattr(httpx, "post", make_response)
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        exchanger = TokenExchanger("https://example.com", sample_pat)
        t1 = exchanger.get_token("user_access")
        t2 = exchanger.get_token("agent_access", agent_id="agent-123")

        assert t1 != t2
        assert call_count == 2

    def test_expired_token_refreshes(self, tmp_path, monkeypatch, sample_pat, mock_exchange):
        mock_post = mock_exchange(expires_in=1)  # expires in 1 second
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        exchanger = TokenExchanger("https://example.com", sample_pat)
        exchanger.get_token("user_access")

        # Token expires within _REFRESH_BUFFER (30s), so next call should re-exchange
        exchanger.get_token("user_access")

        assert mock_post.call_count == 2

    def test_agent_id_included_in_exchange(self, tmp_path, monkeypatch, sample_pat, mock_exchange):
        mock_post = mock_exchange()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        exchanger = TokenExchanger("https://example.com", sample_pat)
        exchanger.get_token("agent_access", agent_id="my-agent-uuid")

        body = mock_post.call_args[1]["json"]
        assert body["agent_id"] == "my-agent-uuid"
        assert body["requested_token_class"] == "agent_access"


    def test_agent_name_ttl_and_resource_included_in_exchange(self, tmp_path, monkeypatch, sample_agent_pat, mock_exchange):
        mock_post = mock_exchange()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        exchanger = TokenExchanger("https://example.com", sample_agent_pat)
        exchanger.get_token(
            "agent_access",
            agent_name="cli-sentinel-local",
            scope="tasks:read tasks:write",
            requested_ttl=3600,
            resource="https://paxai.app/api",
        )

        body = mock_post.call_args[1]["json"]
        assert body == {
            "requested_token_class": "agent_access",
            "audience": "ax-api",
            "scope": "tasks:read tasks:write",
            "requested_ttl": 3600,
            "resource": "https://paxai.app/api",
            "agent_name": "cli-sentinel-local",
        }

    def test_clear_cache(self, tmp_path, monkeypatch, sample_pat, mock_exchange):
        mock_post = mock_exchange()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        exchanger = TokenExchanger("https://example.com", sample_pat)
        exchanger.get_token("user_access")
        exchanger.clear_cache()
        exchanger.get_token("user_access")

        assert mock_post.call_count == 2  # had to re-exchange after clear

    def test_invalid_pat_raises(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        exchanger = TokenExchanger("https://example.com", "not_a_valid_pat")
        with pytest.raises(ValueError, match="Cannot extract key_id"):
            exchanger.get_token("user_access")

    def test_disk_cache_persists(self, tmp_path, monkeypatch, sample_pat, mock_exchange):
        mock_post = mock_exchange()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        exchanger1 = TokenExchanger("https://example.com", sample_pat)
        exchanger1.get_token("user_access")

        # New exchanger instance should load from disk
        exchanger2 = TokenExchanger("https://example.com", sample_pat)
        token = exchanger2.get_token("user_access")

        assert token == "fake.jwt.token"
        assert mock_post.call_count == 1  # only the first exchange, second loaded from disk

    def test_disk_cache_permissions(self, tmp_path, monkeypatch, sample_pat, mock_exchange):
        mock_exchange()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        exchanger = TokenExchanger("https://example.com", sample_pat)
        exchanger.get_token("user_access")

        cache_file = tmp_path / ".ax" / "cache" / "tokens.json"
        assert cache_file.exists()
        mode = cache_file.stat().st_mode & 0o777
        assert mode == 0o600
