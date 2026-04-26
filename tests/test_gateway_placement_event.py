"""Tests for the GATEWAY-PLACEMENT-POLICY-001 SSE listener — task `8467ec87`.

Covers:
- ``_apply_placement_event`` updates the local registry on a valid event
- Idempotency: same-space events are no-ops
- Agent ID mismatch is rejected
- Missing fields don't crash
- ``_post_placement_ack`` tolerates 404 from the not-yet-shipped backend endpoint

The SSE wire-up (event_type filter in ``_listener_loop``) is exercised
end-to-end indirectly; here we test the pure functions so we don't need to
spin up an actual SSE server.
"""

from __future__ import annotations

from typing import Any

from ax_cli.gateway import (
    _apply_placement_event,
    _post_placement_ack,
    find_agent_entry,
    load_gateway_registry,
)


def _seed_registry(tmp_path, monkeypatch, name="probe_agent", agent_id="aaaa1111-2222-3333-4444-555566667777", space_id="space-A"):
    """Set AX_GATEWAY_DIR to a tmp dir + write a minimal registry.json with one agent."""
    import json
    from pathlib import Path

    gw_dir = Path(tmp_path)
    gw_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AX_GATEWAY_DIR", str(gw_dir))
    monkeypatch.delenv("AX_GATEWAY_ENV", raising=False)
    monkeypatch.delenv("AX_USER_ENV", raising=False)
    monkeypatch.delenv("AX_ENV", raising=False)
    registry = {
        "gateway": {"gateway_id": "gw-test"},
        "agents": [
            {
                "name": name,
                "agent_id": agent_id,
                "space_id": space_id,
                "runtime_type": "exec",
                "template_id": "probe",
            }
        ],
    }
    (gw_dir / "registry.json").write_text(json.dumps(registry))
    return registry["agents"][0]


def test_apply_placement_event_updates_registry(tmp_path, monkeypatch):
    """Happy path: event with new current_space updates entry's space_id."""
    entry = _seed_registry(tmp_path, monkeypatch)

    result = _apply_placement_event(
        dict(entry),  # caller's entry
        {
            "agent_id": entry["agent_id"],
            "current_space": "space-B",
            "placement_state": "applied",
            "policy_revision": 7,
            "current_space_set_by": "ax_ui",
        },
    )

    assert result["applied"] is True
    assert result["previous_space"] == "space-A"
    assert result["new_space"] == "space-B"
    assert result["placement_state"] == "applied"
    assert result["policy_revision"] == 7

    # Verify it landed in the registry
    registry = load_gateway_registry()
    persisted = find_agent_entry(registry, "probe_agent")
    assert persisted is not None
    assert persisted["space_id"] == "space-B"
    assert persisted["placement_state"] == "applied"
    assert persisted["placement_revision"] == 7
    assert persisted["placement_source"] == "ax_ui"


def test_apply_placement_event_idempotent_when_same_space(tmp_path, monkeypatch):
    """If event reports current_space we already have, don't re-write the registry."""
    entry = _seed_registry(tmp_path, monkeypatch, space_id="space-A")

    result = _apply_placement_event(
        dict(entry),
        {
            "agent_id": entry["agent_id"],
            "current_space": "space-A",  # same as current
            "placement_state": "applied",
        },
    )

    assert result["applied"] is False
    assert result["reason"] == "already_at_target"


def test_apply_placement_event_rejects_agent_id_mismatch(tmp_path, monkeypatch):
    """Don't apply events meant for a different agent."""
    entry = _seed_registry(tmp_path, monkeypatch)

    result = _apply_placement_event(
        dict(entry),
        {
            "agent_id": "ffffffff-ffff-ffff-ffff-ffffffffffff",  # different
            "current_space": "space-B",
        },
    )

    assert result["applied"] is False
    assert result["reason"] == "agent_id_mismatch"


def test_apply_placement_event_rejects_missing_current_space(tmp_path, monkeypatch):
    """Malformed event without current_space → no apply, no crash."""
    entry = _seed_registry(tmp_path, monkeypatch)

    result = _apply_placement_event(
        dict(entry),
        {"agent_id": entry["agent_id"], "placement_state": "applied"},
    )

    assert result["applied"] is False
    assert result["reason"] == "missing_current_space"


def test_apply_placement_event_rejects_unknown_agent(tmp_path, monkeypatch):
    """Event references an agent NOT in our registry — don't auto-create."""
    _seed_registry(tmp_path, monkeypatch, name="probe_agent")

    # Caller passes an entry for a different agent (race/drift case)
    foreign_entry = {
        "name": "stranger",
        "agent_id": "00000000-0000-0000-0000-000000000099",
        "space_id": "space-X",
    }
    result = _apply_placement_event(
        foreign_entry,
        {
            "agent_id": foreign_entry["agent_id"],
            "current_space": "space-Y",
        },
        agent_name="stranger",
    )

    assert result["applied"] is False
    assert result["reason"] == "agent_not_in_registry"


def test_apply_placement_event_falls_back_to_space_id_field(tmp_path, monkeypatch):
    """Tolerate event payloads that use ``space_id`` instead of ``current_space``.

    Forward-compat: backend may shape the field either way during 31adc3a4
    iteration. Either is accepted.
    """
    entry = _seed_registry(tmp_path, monkeypatch, space_id="space-A")

    result = _apply_placement_event(
        dict(entry),
        {"agent_id": entry["agent_id"], "space_id": "space-C"},
    )

    assert result["applied"] is True
    assert result["new_space"] == "space-C"


# ── _post_placement_ack ─────────────────────────────────────────────────────


class _FakeHttp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self.body = body or {}
        self.calls: list[dict[str, Any]] = []

    def patch(self, path, json=None, **kw):
        self.calls.append({"path": path, "json": json})

        class _R:
            def __init__(s):
                s.status_code = self.status_code

            def json(s):
                return self.body

        return _R()


class _FakeClient:
    def __init__(self, http):
        self._http = http


def test_post_placement_ack_success_returns_true():
    http = _FakeHttp(204)
    client = _FakeClient(http)
    entry = {"agent_id": "aaaa-bbbb"}
    ok = _post_placement_ack(client, entry, placement_state="applied", policy_revision=3)
    assert ok is True
    assert len(http.calls) == 1
    assert http.calls[0]["path"] == "/api/v1/agents/aaaa-bbbb/placement/ack"
    body = http.calls[0]["json"]
    assert body["placement_state"] == "applied"
    assert body["policy_revision"] == 3
    assert "ack_at" in body


def test_post_placement_ack_404_is_silent_noop():
    """Backend's /placement/ack endpoint isn't shipped yet (31adc3a4 pending).

    A 404 should NOT throw — the listener can't ack against an endpoint that
    doesn't exist, but that shouldn't kill the worker.
    """
    http = _FakeHttp(404, {"detail": "Not Found"})
    client = _FakeClient(http)
    ok = _post_placement_ack(client, {"agent_id": "x"}, placement_state="applied")
    assert ok is False  # not raised, just reported as non-success


def test_post_placement_ack_skips_when_no_agent_id():
    """No agent_id → can't construct URL; return False without HTTP call."""
    http = _FakeHttp(200)
    client = _FakeClient(http)
    ok = _post_placement_ack(client, {"agent_id": None}, placement_state="applied")
    assert ok is False
    assert http.calls == []


def test_post_placement_ack_handles_http_exception_gracefully():
    """Connection error / TLS / etc shouldn't kill the listener."""
    class _ExplodingHttp:
        def patch(self, *a, **kw):
            raise RuntimeError("simulated network failure")

    client = _FakeClient(_ExplodingHttp())
    ok = _post_placement_ack(client, {"agent_id": "x"}, placement_state="applied")
    assert ok is False  # exception swallowed, returns False
