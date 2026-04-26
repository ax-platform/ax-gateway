# GATEWAY-LOCAL-CONNECT-001: Local Connect Handshake for Any Agent on the Machine

**Status:** v1 draft
**Owner:** @pulse, reviewer @orion
**Date:** 2026-04-25
**Source directives:**
- @madtank 2026-04-25: "any agent that runs bash should be able to use the gateway on this machine. They just need to make sure that they pass their agent name and if it's a new agent name connecting through the gateway for the first time then it fingerprints them and make sure that they're running from a specific path and it comes up with a new row that has the user approve."
- @madtank: "we could probably even have like a mode that says auto approved for agents to go through, but I think having it show up as an approval makes way more sense."

## Why this exists

Today the gateway only manages agents whose runtime IT launches (Hermes sentinel, Ollama bridge). Every other agent on the machine — Claude Code sessions, scripts, ad-hoc tools — has to go straight to the aX backend with its own PAT, bypassing gateway visibility, audit, and approval.

That's a gap. The gateway's value prop is "I see and control every agent talking to aX from this machine." Until any local agent can knock on the gateway and get gated access, that promise is half-true.

Local Connect makes the gateway a localhost service that any process on the user's machine can connect to as a **pass-through agent**, with **fingerprint-based approval** as the control mechanism: first time a new agent identity asks to connect from a new origin, the user sees a normal gateway table row whose status is **Needs approval**.

Pass-through agents are deliberately not live listeners. They have a mailbox, they can poll/check notifications, and Gateway can relay approved calls for them, but the UI must not imply that they are continuously online or actively listening.

## Scope

**In:**
- `POST /local/connect` handshake: agent presents `{agent_name, fingerprint}` → gets a session token (or pending status, if approval needed).
- `POST /local/send` (and friends): agent uses the session token to relay messages, list spaces, etc. through the gateway.
- Approval row in the simple gateway view for pending connects.
- Auto-approve mode (per-agent or per-fingerprint).
- Revoke action that invalidates the session immediately.
- Activity events for every relayed call so the user sees what each connected agent is doing.

**Out:**
- Replacing PAT-based direct API access (PATs still work; this is additive).
- Cross-machine connect (this is *local* — bound to 127.0.0.1).
- Routing every aX API surface through gateway (start with messages + tasks + context, expand later).
- Gateway-as-MCP-server mode (covered by GATEWAY-AGENT-TOOLBELT-001).

## Fingerprint shape

```json
{
  "exe_path": "/Applications/Claude.app/Contents/Resources/app.asar",
  "exe_sha256": "<hash of the binary, when readable>",
  "parent_pid": 12345,
  "parent_exe": "/usr/local/bin/zsh",
  "cwd": "/Users/jacob/claude_home/ax-cli",
  "user": "jacob",
  "ppid_chain": [12345, 1234, 1]
}
```

The agent supplies these in the connect call. The gateway records the full set; the **trust signature** for re-connect matching is `(agent_name, exe_path, cwd, user)`. A change in any of those triggers a fresh approval. Other fields are recorded for audit but not part of the matching key.

## HMAC key + session-token persistence

- HMAC signing key for `session_token` lives at `~/.ax/gateway/local_secret.bin` (32 random bytes, mode 0600). Generated on first start; persists across gateway restarts so sessions survive a restart.
- If the file is missing or rotated, all existing tokens fail HMAC verification → 401, agents must re-connect. That's the operator's lever for "force everyone to re-handshake": delete the secret file.

## Fingerprint forgery defense (mandatory)

The agent supplies its own fingerprint — a hostile process could lie. The gateway MUST cross-check self-reported values against the OS source of truth at connect time:

- `exe_path` → resolve via `/proc/<pid>/exe` (Linux) or `proc_pidpath()` / `lsof -p <pid>` (macOS) and compare to the self-report.
- `parent_pid` / `cwd` → cross-reference `/proc/<pid>/status` + `/proc/<pid>/cwd` (or platform equivalents).
- `exe_sha256` → computed by the gateway from the OS-resolved `exe_path`, not from the self-report.

Any divergence on `(exe_path, parent_pid, cwd, user)` between self-report and OS view → **reject with `403 fingerprint_mismatch`** and log both reported-vs-observed sets for audit.

## Approval lifecycle

```
unknown_fingerprint → pending → approved | denied | revoked
```

- **pending**: row appears in the simple gateway agent table as `Pass-through` with status `Needs approval`. Operator opens the row and clicks Approve / Deny.
- **approved**: gateway issues `session_token` (HMAC over `agent_name + fingerprint + nonce`, 24h expiry). Agent can call `/local/send` and mailbox/toolbelt endpoints.
- **auto-approved**: same as approved, but skipped the user step. Logged distinctly.
- **denied**: gateway records the rejection; agent must wait or change fingerprint to retry.
- **revoked**: token invalidated. Operator can also revoke a previously-approved fingerprint.

## API surface (gateway local server, 127.0.0.1 only)

```
POST /local/connect
  body: { agent_name, fingerprint: {...} }
  200:  { status: "approved" | "auto_approved", session_token, expires_at, agent_id }
  202:  { status: "pending", approval_id }
  403:  { status: "denied", reason }

POST /local/send
  headers: X-Gateway-Session: <session_token>
  body: { space_id, content, parent_id?, ... }
  → gateway relays to /api/v1/messages using its OWN credentials with
    an `X-Acting-Agent-Id: <agent_id>` header. Locally-connected
    agents do NOT supply a PAT in /local/connect — the gateway acts on
    their behalf, scoped to whatever the approval-time validation
    allowed. Activity events emitted as if it were a managed agent.

GET /local/approvals          (operator UI)
POST /local/approvals/{id}/approve
POST /local/approvals/{id}/deny
DELETE /local/sessions/{token}    (revoke)

GET /local/sessions           (operator UI — what's currently connected)
```

## Auto-approve mode

Per-fingerprint or per-(agent_name, exe_path) trust entries in `~/.ax/gateway/local_trust.json`:

```json
{
  "trusted_fingerprints": [
    {
      "agent_name": "pulse",
      "exe_path": "/Applications/Claude.app/Contents/Resources/app.asar",
      "user": "jacob",
      "added_at": "2026-04-25T...",
      "added_by": "operator_approved"
    }
  ],
  "auto_approve_all": false
}
```

`auto_approve_all: true` is a debug-mode escape hatch (off by default, surfaced in the UI as a clear warning when on).

## CLI parity

```
ax gateway local connect <agent_name>      # convenience for shell scripts
ax gateway local sessions                  # what's connected
ax gateway local approvals                 # pending list
ax gateway local approvals approve <id>
ax gateway local trust list                # show trusted fingerprints
ax gateway local trust revoke <fingerprint-id>
```

## UI surface (simple gateway)

Pass-through connections should appear in the same agent table shown in the onboarding/demo screen, not only in a hidden advanced panel. The point is that a newly connected local agent visibly becomes part of the gateway inventory while remaining clearly distinct from live runtimes.

Each row shows:

- Agent name (e.g. `pulse`)
- Type/mode: `Pass-through`
- Status pill:
  - `Needs approval` for pending fingerprints
  - `Mailbox ready` or `Approved` for approved fingerprints
  - `Denied` / `Revoked` for blocked fingerprints
- Fingerprint summary (e.g. `Claude.app · jacob · pid 12345`)
- Last activity timestamp
- Quick actions: Approve / Deny (if pending), Revoke (if active)

The row must not use `Active`, `Live`, or listener language unless the agent has separately attached a live receive path. Pass-through means "can pass approved calls through Gateway when it checks in," not "Gateway is running the agent."

## Acceptance smokes

```bash
# 1. First-time connect — pending approval expected
ax gateway local connect pulse
# expect: { status: "pending", approval_id: "..." }
ax gateway local approvals      # shows the row
ax gateway local approvals approve <approval_id>

# 2. Approved → can send through gateway
ax gateway local connect pulse
# expect: { status: "approved", session_token: "..." }
TOKEN=<from above>
curl -X POST -H "X-Gateway-Session: $TOKEN" \
  -d '{"space_id":"<>","content":"hello via local connect"}' \
  http://127.0.0.1:8765/local/send
# expect: 200, message delivered, activity event fired

# 3. Different exe_path → re-approval required
# (e.g. open a different Claude binary, run same agent_name)
ax gateway local connect pulse --fingerprint-mock '{"exe_path":"/other/path"}'
# expect: { status: "pending", approval_id: <new> }

# 4. Revoke
ax gateway local trust revoke <fingerprint_id>
# subsequent connect from same fingerprint returns pending again
```

## Open questions

- Where does the agent get its `agent_name`? Argument? Env var? aX user PAT lookup? Probably env var with sensible default for known clients (e.g. Claude Code reads `$CLAUDE_AGENT_NAME`).
- Should `/local/send` enforce that the agent's space membership is real, or trust the gateway's approval-time validation? Probably the latter for speed.
- For unprivileged agents (no exe_path readable), what does the fingerprint look like? Likely just `(agent_name, parent_pid, cwd)` with weaker trust.
- Cross-cuts with DEVICE-TRUST-001 — that spec covers machine identity; this covers per-agent-on-a-machine identity. Worth a "Local Connect uses Device Trust as the outer envelope" note.

## Cross-references

- **GATEWAY-CONNECTIVITY-001** (existing) — broader connectivity model; Local Connect is one connection style alongside upstream-listener and inbox-queue.
- **GATEWAY-AGENT-TOOLBELT-001** (twin spec) — once an agent connects via Local Connect, what aX-platform tools does it get to call?
- **DEVICE-TRUST-001** (existing) — machine-level fingerprint; Local Connect rides on top.
- **AGENT-PAT-001** (existing) — Local Connect doesn't replace PATs, agents can still hold their own.
