"""Tests for `axctl bootstrap-agent` — see
shared/state/axctl-friction-2026-04-17.md §0.

The one-shot command composes four APIs and two scope vocabularies, so we
mock httpx at the client layer and assert on the request shape + the
workspace artifacts written to a tmp_path. The critical invariant under
test is the /credentials/agent-pat → /api/v1/keys fallback, since that's
the footgun the command exists to hide."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from ax_cli.commands import bootstrap as bootstrap_cmd
from ax_cli.main import app

runner = CliRunner()


SPACE_ID = "ed81ae98-50cb-4268-b986-1b9fe76df742"
AGENT_ID = "6452707e-2892-412f-8439-8ae46dfcc4e6"


class _FakeHttp:
    """Captures each request so tests can assert request shape + sequence.

    Configure responses per ``(METHOD, prefix)`` prefix match — lets us
    simulate the mgmt-route-miss on /credentials/agent-pat while still
    returning real JSON for /api/v1/agents and /api/v1/keys.
    """

    def __init__(self, routes: dict[tuple[str, str], tuple[int, dict | str, str | None]]):
        """routes keys: (METHOD, url-prefix-match) → (status, body, content_type)."""
        self.routes = routes
        self.calls: list[dict] = []

    def _lookup(self, method: str, url: str):
        for (m, prefix), payload in self.routes.items():
            if m == method and url.startswith(prefix):
                return payload
        return (404, {"detail": "no route mocked"}, "application/json")

    def _respond(self, method: str, url: str, json=None, params=None, headers=None):
        self.calls.append(
            {"method": method, "url": url, "json": json, "params": params, "headers": dict(headers or {})}
        )
        status, body, ct = self._lookup(method, url)
        request = httpx.Request(method, f"http://test.local{url}")
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body, request=request)
        return httpx.Response(
            status, content=body or b"", headers={"content-type": ct or "text/plain"}, request=request
        )

    def post(self, url, json=None, params=None, headers=None, **kw):
        return self._respond("POST", url, json=json, params=params, headers=headers)

    def put(self, url, json=None, headers=None, **kw):
        return self._respond("PUT", url, json=json, headers=headers)

    def patch(self, url, json=None, headers=None, **kw):
        return self._respond("PATCH", url, json=json, headers=headers)

    def get(self, url, params=None, headers=None, **kw):
        return self._respond("GET", url, params=params, headers=headers)


class _FakeClient:
    base_url = "https://next.paxai.app"

    def __init__(self, http: _FakeHttp):
        self._http = http
        # Mirror AxClient surface the bootstrap module touches
        self._base_headers: dict = {}
        self._exchanger = None

    def _parse_json(self, r: httpx.Response):
        return r.json()

    def update_agent(self, identifier, **fields):
        r = self._http.put(f"/api/v1/agents/manage/{identifier}", json=fields)
        r.raise_for_status()
        return r.json()

    def mgmt_issue_agent_pat(self, agent_id, *, name=None, expires_in_days=90, audience="cli"):
        body = {"agent_id": agent_id, "expires_in_days": expires_in_days, "audience": audience}
        if name:
            body["name"] = name
        r = self._http.post("/credentials/agent-pat", json=body)
        r.raise_for_status()
        return r.json()

    def create_key(
        self,
        name,
        *,
        allowed_agent_ids=None,
        bound_agent_id=None,
        audience=None,
        scopes=None,
        space_id=None,
    ):
        body = {"name": name}
        if allowed_agent_ids:
            body["agent_scope"] = "agents"
            body["allowed_agent_ids"] = allowed_agent_ids
        if bound_agent_id:
            body["bound_agent_id"] = bound_agent_id
        if audience:
            body["audience"] = audience
        if scopes:
            body["scopes"] = scopes
        headers = {"X-Space-Id": space_id} if space_id else None
        r = self._http.post("/api/v1/keys", json=body, headers=headers)
        r.raise_for_status()
        return r.json()


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def user_pat(monkeypatch):
    """Pretend a user PAT is resolved + the user env is 'default'."""
    monkeypatch.setattr(
        bootstrap_cmd, "resolve_user_token", lambda: "axp_u_test1234567890abcd.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    )
    monkeypatch.setattr(bootstrap_cmd, "resolve_user_base_url", lambda: "https://next.paxai.app")
    monkeypatch.setattr(bootstrap_cmd, "_resolve_user_env", lambda: "default")
    monkeypatch.setattr(bootstrap_cmd, "_user_config_path", lambda: Path("/tmp/nope-not-real.toml"))


@pytest.fixture
def verify_stub(monkeypatch):
    """Stub the /auth/me verify call so we don't need to mock the whole
    transport for a post-mint read."""
    monkeypatch.setattr(
        bootstrap_cmd,
        "_verify_with_new_token",
        lambda **kw: [{"space_id": kw["space_id"], "name": "ax-cli-dev", "is_default": True}],
    )


# ── happy path (canonical mgmt route works) ────────────────────────────


def test_bootstrap_happy_path_uses_mgmt(monkeypatch, tmp_path, user_pat, verify_stub):
    http = _FakeHttp(
        {
            ("GET", "/api/v1/agents"): (200, {"agents": []}, None),  # doesn't exist yet
            ("POST", "/api/v1/agents"): (
                201,
                {"id": AGENT_ID, "name": "axolotl", "space_id": SPACE_ID},
                None,
            ),
            ("POST", "/credentials/agent-pat"): (
                201,
                {"token": "axp_a_mintedViaMgmt", "credential_id": "c-1"},
                None,
            ),
        }
    )
    monkeypatch.setattr(bootstrap_cmd, "get_user_client", lambda: _FakeClient(http))

    result = runner.invoke(
        app,
        [
            "bootstrap-agent",
            "axolotl",
            "--space-id",
            SPACE_ID,
            "--description",
            "Friendly amphibian",
            "--model",
            "codex:gpt-5.4",
            "--audience",
            "both",
            "--save-to",
            str(tmp_path / "axolotl"),
        ],
    )
    assert result.exit_code == 0, result.output

    # Mgmt path was used — no fallback request to /api/v1/keys
    posts = [c for c in http.calls if c["method"] == "POST"]
    assert any(c["url"] == "/credentials/agent-pat" for c in posts), "mgmt path should have been tried"
    assert not any(c["url"] == "/api/v1/keys" for c in posts), "fallback should not fire when mgmt works"

    # Workspace was written, 0600
    token_file = tmp_path / "axolotl" / ".ax" / "token"
    config_file = tmp_path / "axolotl" / ".ax" / "config.toml"
    assert token_file.exists()
    assert config_file.exists()
    assert oct(token_file.stat().st_mode)[-3:] == "600"
    assert oct(config_file.stat().st_mode)[-3:] == "600"
    assert token_file.read_text() == "axp_a_mintedViaMgmt"
    cfg = config_file.read_text()
    assert f'agent_id = "{AGENT_ID}"' in cfg
    assert f'space_id = "{SPACE_ID}"' in cfg
    assert 'principal_type = "agent"' in cfg


# ── fallback when /credentials/agent-pat is frontend-caught ─────────────


def test_bootstrap_falls_back_to_keys_when_mgmt_returns_html(monkeypatch, tmp_path, user_pat, verify_stub):
    html_fixture = "<!DOCTYPE html><html><body>frontend</body></html>"
    http = _FakeHttp(
        {
            ("GET", "/api/v1/agents"): (200, {"agents": []}, None),
            ("POST", "/api/v1/agents"): (201, {"id": AGENT_ID, "name": "axolotl"}, None),
            # Prod-style frontend catch — 200 + HTML
            ("POST", "/credentials/agent-pat"): (200, html_fixture, "text/html; charset=utf-8"),
            ("POST", "/api/v1/keys"): (
                201,
                {"token": "axp_a_mintedViaFallback", "credential_id": "c-2"},
                None,
            ),
        }
    )
    monkeypatch.setattr(bootstrap_cmd, "get_user_client", lambda: _FakeClient(http))

    result = runner.invoke(
        app,
        [
            "bootstrap-agent",
            "axolotl",
            "--space-id",
            SPACE_ID,
            "--save-to",
            str(tmp_path / "ax"),
        ],
    )
    assert result.exit_code == 0, result.output

    urls = [(c["method"], c["url"]) for c in http.calls]
    assert ("POST", "/credentials/agent-pat") in urls
    assert ("POST", "/api/v1/keys") in urls

    # Fallback call shape: must be agent-bound and space-locked
    keys_call = next(c for c in http.calls if c["url"] == "/api/v1/keys")
    assert keys_call["json"]["bound_agent_id"] == AGENT_ID
    assert keys_call["json"]["allowed_agent_ids"] == [AGENT_ID]
    assert keys_call["json"]["audience"] == "both"
    assert keys_call["json"]["scopes"] == bootstrap_cmd.DEFAULT_KEY_SCOPES
    assert keys_call["headers"].get("X-Space-Id") == SPACE_ID

    assert (tmp_path / "ax" / ".ax" / "token").read_text() == "axp_a_mintedViaFallback"


def test_bootstrap_falls_back_on_404(monkeypatch, tmp_path, user_pat, verify_stub):
    http = _FakeHttp(
        {
            ("GET", "/api/v1/agents"): (200, {"agents": []}, None),
            ("POST", "/api/v1/agents"): (201, {"id": AGENT_ID, "name": "axolotl"}, None),
            ("POST", "/credentials/agent-pat"): (404, {"detail": "not found"}, None),
            ("POST", "/api/v1/keys"): (201, {"token": "axp_a_via404fallback"}, None),
        }
    )
    monkeypatch.setattr(bootstrap_cmd, "get_user_client", lambda: _FakeClient(http))

    result = runner.invoke(
        app,
        ["bootstrap-agent", "axolotl", "--space-id", SPACE_ID, "--save-to", str(tmp_path / "a")],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "a" / ".ax" / "token").read_text() == "axp_a_via404fallback"


# ── error propagation on existence check (PR #67 review, v2) ──────────


@pytest.mark.parametrize("status_code", [401, 403, 500, 503])
def test_bootstrap_does_not_swallow_existence_check_errors(monkeypatch, tmp_path, user_pat, status_code):
    """Regression for axolotl's PR #67 review finding: a 401/403/5xx on the
    existence check MUST NOT be silently treated as 'agent not found'. If
    that happens, bootstrap would proceed to create and potentially clobber
    an existing agent (or bury a real infra failure under a confusing
    downstream error). Propagate loudly instead."""
    http = _FakeHttp(
        {
            ("GET", "/api/v1/agents"): (status_code, {"detail": "boom"}, None),
            # If the bug regresses, the command will happily proceed to POST
            # and mint — we assert it does NOT reach those routes.
            ("POST", "/api/v1/agents"): (201, {"id": AGENT_ID, "name": "axolotl"}, None),
            ("POST", "/credentials/agent-pat"): (201, {"token": "axp_a_shouldNotHappen"}, None),
        }
    )
    monkeypatch.setattr(bootstrap_cmd, "get_user_client", lambda: _FakeClient(http))

    result = runner.invoke(
        app,
        ["bootstrap-agent", "axolotl", "--space-id", SPACE_ID, "--save-to", str(tmp_path / "a")],
    )
    assert result.exit_code != 0, result.output

    posts = [c for c in http.calls if c["method"] == "POST"]
    assert not any(c["url"] == "/api/v1/agents" for c in posts), (
        f"bootstrap swallowed {status_code} and proceeded to create — regression"
    )
    assert not any("credentials" in c["url"] or "keys" in c["url"] for c in posts)


def test_bootstrap_handles_404_on_existence_as_not_found(monkeypatch, tmp_path, user_pat, verify_stub):
    """A 404 from the list endpoint is the one case where 'not found' is
    the sensible interpretation — the space is gone or the caller isn't a
    member; the downstream POST will produce a clearer error. Bootstrap
    should continue rather than halt."""
    http = _FakeHttp(
        {
            ("GET", "/api/v1/agents"): (404, {"detail": "not a member"}, None),
            # The POST is expected to fail too in this case, but we're only
            # asserting that the existence check didn't halt bootstrap.
            ("POST", "/api/v1/agents"): (201, {"id": AGENT_ID, "name": "axolotl"}, None),
            ("POST", "/credentials/agent-pat"): (201, {"token": "axp_a_404case"}, None),
        }
    )
    monkeypatch.setattr(bootstrap_cmd, "get_user_client", lambda: _FakeClient(http))

    result = runner.invoke(
        app,
        ["bootstrap-agent", "axolotl", "--space-id", SPACE_ID, "--save-to", str(tmp_path / "a")],
    )
    assert result.exit_code == 0, result.output
    assert any(c["method"] == "POST" and c["url"] == "/api/v1/agents" for c in http.calls)


# ── already-exists behaviour ────────────────────────────────────────────


def test_bootstrap_aborts_when_agent_exists_without_allow_existing(monkeypatch, tmp_path, user_pat):
    http = _FakeHttp(
        {
            ("GET", "/api/v1/agents"): (
                200,
                {"agents": [{"id": AGENT_ID, "name": "axolotl"}]},
                None,
            ),
        }
    )
    monkeypatch.setattr(bootstrap_cmd, "get_user_client", lambda: _FakeClient(http))

    result = runner.invoke(
        app,
        ["bootstrap-agent", "axolotl", "--space-id", SPACE_ID, "--save-to", str(tmp_path / "a")],
    )
    assert result.exit_code == 2, result.output
    assert "already exists" in result.output
    # Didn't mint anything
    posts = [c for c in http.calls if c["method"] == "POST"]
    assert not any("credentials" in c["url"] or "keys" in c["url"] for c in posts)


def test_bootstrap_reuses_existing_agent_when_allowed(monkeypatch, tmp_path, user_pat, verify_stub):
    http = _FakeHttp(
        {
            ("GET", "/api/v1/agents"): (
                200,
                {"agents": [{"id": AGENT_ID, "name": "axolotl"}]},
                None,
            ),
            ("POST", "/credentials/agent-pat"): (201, {"token": "axp_a_reuse"}, None),
        }
    )
    monkeypatch.setattr(bootstrap_cmd, "get_user_client", lambda: _FakeClient(http))

    result = runner.invoke(
        app,
        [
            "bootstrap-agent",
            "axolotl",
            "--space-id",
            SPACE_ID,
            "--allow-existing",
            "--save-to",
            str(tmp_path / "a"),
        ],
    )
    assert result.exit_code == 0, result.output
    # No POST to /api/v1/agents because we reused
    assert not any(c for c in http.calls if c["method"] == "POST" and c["url"] == "/api/v1/agents")


# ── guardrails on token type ────────────────────────────────────────────


def test_bootstrap_rejects_agent_pat(monkeypatch, user_pat):
    monkeypatch.setattr(bootstrap_cmd, "resolve_user_token", lambda: "axp_a_agentTokenShouldFail")
    result = runner.invoke(
        app,
        ["bootstrap-agent", "axolotl", "--space-id", SPACE_ID],
    )
    assert result.exit_code == 1
    assert "Cannot bootstrap with an agent PAT" in result.output


def test_bootstrap_requires_user_token(monkeypatch, user_pat):
    monkeypatch.setattr(bootstrap_cmd, "resolve_user_token", lambda: None)
    result = runner.invoke(app, ["bootstrap-agent", "axolotl", "--space-id", SPACE_ID])
    assert result.exit_code == 1
    assert "No user token found" in result.output


# ── dry-run ─────────────────────────────────────────────────────────────


def test_bootstrap_dry_run_touches_nothing(monkeypatch, tmp_path, user_pat):
    http = _FakeHttp({})  # no routes — any call would 404
    monkeypatch.setattr(bootstrap_cmd, "get_user_client", lambda: _FakeClient(http))

    result = runner.invoke(
        app,
        [
            "bootstrap-agent",
            "axolotl",
            "--space-id",
            SPACE_ID,
            "--save-to",
            str(tmp_path / "a"),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert http.calls == []
    assert not (tmp_path / "a").exists()


# ── effective-config line is printed ───────────────────────────────────


def test_bootstrap_prints_effective_config(monkeypatch, tmp_path, user_pat, verify_stub):
    http = _FakeHttp(
        {
            ("GET", "/api/v1/agents"): (200, {"agents": []}, None),
            ("POST", "/api/v1/agents"): (201, {"id": AGENT_ID, "name": "axolotl"}, None),
            ("POST", "/credentials/agent-pat"): (201, {"token": "axp_a_x"}, None),
        }
    )
    monkeypatch.setattr(bootstrap_cmd, "get_user_client", lambda: _FakeClient(http))

    result = runner.invoke(
        app,
        ["bootstrap-agent", "axolotl", "--space-id", SPACE_ID, "--save-to", str(tmp_path / "a")],
    )
    assert result.exit_code == 0, result.output
    assert "base_url=" in result.output
    assert "user_env=" in result.output
