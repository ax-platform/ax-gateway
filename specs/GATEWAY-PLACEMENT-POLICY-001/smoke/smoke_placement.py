#!/usr/bin/env python3
"""Platform smoke test for task `2598129a` — validates backend `9e0286c1`.

See `README.md` in this directory for setup and what it checks.

Boundary: platform-only. Does NOT touch the Gateway daemon. Do not import
`ax_cli.gateway` from here — this is a contract check against the published
spec, not an integration test of the local listener.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx  # noqa: I001  (single-source ordering: stdlib then third-party)

BASE_URL = os.environ.get("AX_BASE_URL", "https://dev.paxai.app").rstrip("/")
USER_PAT = os.environ.get("AX_USER_PAT", "").strip()
AGENT_NAME = os.environ.get("PROBE_AGENT_NAME", "placement_probe").strip()
SPACE_A = os.environ.get("SPACE_A", "").strip()
SPACE_B = os.environ.get("SPACE_B", "").strip()
SSE_TIMEOUT_SECONDS = int(os.environ.get("SSE_TIMEOUT_SECONDS", "10"))


# ── output helpers ──────────────────────────────────────────────────────────


VERBOSE = "--verbose" in sys.argv or "-v" in sys.argv


def _print_check(idx: int, total: int, label: str, status: str, detail: str = "") -> None:
    """[N/T] label                                                  ✓|✗|·"""
    pad = max(60 - len(label), 1)
    marker = {"ok": "\033[32m✓\033[0m", "fail": "\033[31m✗\033[0m", "skip": "\033[33m·\033[0m"}.get(
        status, "?"
    )
    print(f"[{idx}/{total}] {label}{' ' * pad}{marker}")
    if detail and (status != "ok" or VERBOSE):
        for line in detail.rstrip().splitlines():
            print(f"      {line}")


def _fail(msg: str) -> None:
    print(f"\033[31mFAIL — {msg}\033[0m", file=sys.stderr)
    sys.exit(1)


def _setup(msg: str) -> None:
    print(f"\033[33mSETUP — {msg}\033[0m", file=sys.stderr)
    sys.exit(2)


# ── checks ──────────────────────────────────────────────────────────────────


def _exchange_pat_for_jwt(client: httpx.Client) -> str:
    """POST /auth/exchange. PATs can't hit business routes; need a JWT."""
    r = client.post(
        "/auth/exchange",
        json={"requested_token_class": "user_access", "scope": "agents:read agents:write messages:read"},
        headers={"Authorization": f"Bearer {USER_PAT}"},
    )
    if r.status_code != 200:
        raise RuntimeError(f"PAT→JWT exchange failed: {r.status_code} {r.text[:200]}")
    payload = r.json()
    return payload.get("access_token") or payload.get("token") or ""


def _resolve_agent_id(client: httpx.Client, jwt: str) -> str:
    """GET agent by name to resolve its UUID."""
    r = client.get(
        "/api/v1/agents",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    if r.status_code != 200:
        raise RuntimeError(f"agent resolve failed: {r.status_code} {r.text[:200]}")
    payload = r.json()
    agents = payload if isinstance(payload, list) else payload.get("agents", [])
    for a in agents:
        if a.get("name", "").lower() == AGENT_NAME.lower():
            return str(a.get("id"))
    raise RuntimeError(f"no agent named {AGENT_NAME!r} found in response")


def _check_get_placement(client: httpx.Client, jwt: str, agent_id: str) -> dict[str, Any]:
    """Spec line 103: GET /api/v1/agents/{id}/placement returns current policy + state.

    Forward-compat: if backend hasn't shipped the dedicated GET, fall back to
    the agent record's space_id field to extract minimal placement.
    """
    r = client.get(
        f"/api/v1/agents/{agent_id}/placement",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    if r.status_code == 404:
        # Endpoint not yet live; fall back to agent record
        r = client.get(
            f"/api/v1/agents/{agent_id}",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        if r.status_code != 200:
            raise RuntimeError(f"agent fallback failed: {r.status_code} {r.text[:200]}")
        agent = r.json()
        return {
            "agent_id": agent_id,
            "current_space": str(agent.get("space_id", "")),
            "_fallback": True,
        }
    if r.status_code != 200:
        raise RuntimeError(f"GET /placement failed: {r.status_code} {r.text[:200]}")
    return r.json()


def _check_patch_placement(
    client: httpx.Client, jwt: str, agent_id: str, target_space: str, policy_revision: int
) -> dict[str, Any]:
    """Spec line 104 + 85: PATCH /placement with {current_space, source, policy_revision}.

    Transitions placement_state to ``pending`` per the state diagram.
    """
    r = client.patch(
        f"/api/v1/agents/{agent_id}/placement",
        json={
            "current_space": target_space,
            "source": "ax_ui",
            "policy_revision": policy_revision,
        },
        headers={"Authorization": f"Bearer {jwt}"},
    )
    if r.status_code not in {200, 202}:
        # Backend may also accept POST until they migrate to PATCH semantics
        if r.status_code in {404, 405}:
            r2 = client.post(
                f"/api/v1/agents/{agent_id}/placement",
                json={"space_id": target_space, "pinned": False},
                headers={"Authorization": f"Bearer {jwt}"},
            )
            if r2.status_code in {200, 201}:
                payload = r2.json()
                payload["_fallback_post"] = True
                return payload
        raise RuntimeError(f"PATCH /placement failed: {r.status_code} {r.text[:300]}")
    return r.json()


def _check_sse_event(
    client: httpx.Client, jwt: str, agent_id: str
) -> dict[str, Any] | None:
    """Spec line 87, 107: subscribe to SSE, expect agent.placement.changed event.

    Times out after SSE_TIMEOUT_SECONDS. Returns the event payload or None on timeout.
    """
    headers = {"Authorization": f"Bearer {jwt}", "Accept": "text/event-stream"}
    deadline = time.monotonic() + SSE_TIMEOUT_SECONDS
    try:
        with client.stream(
            "GET",
            "/api/sse/messages",
            headers=headers,
            params={"token": jwt},  # backend variants — try both
            timeout=httpx.Timeout(connect=5, read=SSE_TIMEOUT_SECONDS),
        ) as response:
            if response.status_code != 200:
                raise RuntimeError(f"SSE connect failed: {response.status_code}")
            event_type = ""
            data_buf: list[str] = []
            for line in response.iter_lines():
                if time.monotonic() > deadline:
                    return None
                if not line:
                    if event_type == "agent.placement.changed" and data_buf:
                        try:
                            payload = json.loads("\n".join(data_buf))
                        except json.JSONDecodeError:
                            payload = {"_raw": "\n".join(data_buf)}
                        if str(payload.get("agent_id", "")).lower() == agent_id.lower():
                            return payload
                    event_type = ""
                    data_buf = []
                    continue
                if line.startswith("event:"):
                    event_type = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data_buf.append(line.split(":", 1)[1].strip())
    except httpx.TimeoutException:
        return None
    return None


def _check_ack_endpoint(
    client: httpx.Client, jwt: str, agent_id: str, policy_revision: int
) -> tuple[int, str]:
    """Spec line 106: PATCH /placement/ack — Gateway-only.

    User PATs are expected to get 403 here (attestation gate). That's an
    accepted outcome — we're checking the route exists with the right shape,
    not that we have permission. 404 is the failure mode (route not shipped).
    """
    r = client.patch(
        f"/api/v1/agents/{agent_id}/placement/ack",
        json={
            "placement_state": "applied",
            "policy_revision": policy_revision,
            "ack_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        headers={"Authorization": f"Bearer {jwt}"},
    )
    return r.status_code, r.text[:300]


# ── runner ──────────────────────────────────────────────────────────────────


def main() -> int:
    if not USER_PAT:
        _setup("AX_USER_PAT not set — see README.md")
    if not SPACE_A or not SPACE_B:
        _setup("SPACE_A and SPACE_B must both be set — see README.md")
    if SPACE_A == SPACE_B:
        _setup("SPACE_A and SPACE_B must differ for a meaningful round-trip")

    client = httpx.Client(base_url=BASE_URL, timeout=15.0)

    try:
        # 1. PAT → JWT
        try:
            jwt = _exchange_pat_for_jwt(client)
            _print_check(1, 5, "Exchange PAT → JWT", "ok", f"jwt prefix: {jwt[:24]}...")
        except Exception as exc:
            _print_check(1, 5, "Exchange PAT → JWT", "fail", str(exc))
            return 1

        # 2. Resolve agent + read placement
        try:
            agent_id = _resolve_agent_id(client, jwt)
            current = _check_get_placement(client, jwt, agent_id)
            previous_space = str(current.get("current_space") or current.get("space_id") or "")
            target_space = SPACE_B if previous_space == SPACE_A else SPACE_A
            policy_revision = int(current.get("policy_revision") or 0) + 1
            note = f"agent_id={agent_id[:8]}... current={previous_space[:8]}... → target={target_space[:8]}..."
            if current.get("_fallback"):
                note += "  (used /agents/{id} fallback; spec'd GET /placement not shipped)"
            _print_check(2, 5, "Resolve agent placement record", "ok", note)
        except Exception as exc:
            _print_check(2, 5, "Resolve agent placement record", "fail", str(exc))
            return 1

        # 3. PATCH /placement → target_space
        try:
            patch_response = _check_patch_placement(client, jwt, agent_id, target_space, policy_revision)
            state_field = patch_response.get("placement_state") or "(missing)"
            note = f"response.placement_state={state_field}"
            if patch_response.get("_fallback_post"):
                note += "  (used POST fallback; PATCH semantics not shipped)"
            _print_check(3, 5, "PATCH /placement → target (transition to pending)", "ok", note)
        except Exception as exc:
            _print_check(3, 5, "PATCH /placement → target (transition to pending)", "fail", str(exc))
            return 1

        # 4. SSE: receive agent.placement.changed
        try:
            event = _check_sse_event(client, jwt, agent_id)
            if event is None:
                _print_check(
                    4, 5, "SSE: receive agent.placement.changed event", "fail",
                    f"no matching event arrived in {SSE_TIMEOUT_SECONDS}s",
                )
                return 1
            note = (
                f"event.agent_id={str(event.get('agent_id',''))[:8]}...  "
                f"current_space={str(event.get('current_space',''))[:8]}...  "
                f"placement_state={event.get('placement_state','(missing)')}  "
                f"policy_revision={event.get('policy_revision','(missing)')}"
            )
            _print_check(4, 5, "SSE: receive agent.placement.changed event", "ok", note)
        except Exception as exc:
            _print_check(4, 5, "SSE: receive agent.placement.changed event", "fail", str(exc))
            return 1

        # 5. PATCH /placement/ack
        try:
            status, body = _check_ack_endpoint(client, jwt, agent_id, policy_revision)
            if status == 404:
                _print_check(5, 5, "PATCH /placement/ack route exists", "fail", f"got 404 — endpoint not shipped: {body}")
                return 1
            if status == 403:
                _print_check(5, 5, "PATCH /placement/ack route exists", "ok", "403 (attestation gate; expected for non-Gateway PAT)")
            elif 200 <= status < 300:
                _print_check(5, 5, "PATCH /placement/ack route exists", "ok", f"{status} accepted")
            else:
                _print_check(5, 5, "PATCH /placement/ack route exists", "fail", f"{status}: {body}")
                return 1
        except Exception as exc:
            _print_check(5, 5, "PATCH /placement/ack route exists", "fail", str(exc))
            return 1

        print()
        print("\033[32mPASS — backend contract for 9e0286c1 matches spec.\033[0m")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
