# AGENT-PAT-001: Agent PAT Minting and JWT Exchange

**Status:** Draft  
**Owner:** @alex / @ChatGPT  
**Date:** 2026-04-13  
**Related:** AXCTL-BOOTSTRAP-001, DEVICE-TRUST-001, CLI-WORKFLOW-001, LISTENER-001

## Summary

Define how trusted user/device context mints agent PATs, and how those PATs
exchange for short-lived runtime JWTs.

The intended model is API-first:

```text
trusted device/user context -> agent PAT -> short-lived agent access JWT
```

Agent PATs are not general user tokens. They are scoped bootstrap credentials
for one agent and one audience. Runtime API calls use short-lived JWTs.

User PATs are not agent impersonation tokens. A user PAT may authenticate
user-authored CLI/API work and may mint scoped agent PATs, but it must not be
accepted as agent runtime authority merely because a caller supplies an agent
route, header, profile field, or MCP URL.

## Credential Types

| Credential | Created By | Used By | Purpose | Retrieval |
|------------|------------|---------|---------|-----------|
| User PAT | aX UI | `axctl login` | Bootstrap CLI setup and mint agent PATs | Shown once |
| Device credential | Backend during init | `axctl` | Request user/device JWTs and mint agent PATs by policy | Stored locally only |
| Agent PAT | Backend | agent runtime / CLI profile / MCP headless | Exchange for short-lived agent JWTs | Shown once or written to local secret store |
| Access JWT | Backend exchange endpoint | API/MCP/CLI runtime | Short-lived API authorization | Never stored long-term |

## Flow

```mermaid
flowchart TD
    A[Trusted axctl device] --> B[Request agent PAT]
    B --> C{Backend policy}
    C -->|allow| D[Issue scoped agent PAT]
    C -->|needs approval| E[Create approval request]
    E -->|approved| D
    E -->|denied| F[No credential issued]
    D --> G[Store agent PAT in OS keychain or mode 0600 file]
    G --> H[Exchange PAT for short-lived agent JWT]
    H --> I[CLI/MCP/API call as agent]
```

## Agent Team Bootstrap

The primary team setup workflow is:

```text
user axctl login -> trusted setup context -> axctl token mint/profile setup -> agent runtimes
```

This allows a trusted local automation agent to help provision a team without
ever receiving the user's bootstrap token.

Expected behavior:

- The setup agent invokes `axctl token mint --save-to ... --profile ...`.
- `axctl` authorizes the mint through the enrolled user/device context.
- The backend verifies policy and issues one scoped agent PAT.
- `axctl` writes the PAT to the target profile/token file with mode `0600`.
- CLI output returns profile, token-file path, agent id, audience, and expiry.
- CLI output does not print the raw PAT unless `--print-token` is explicitly
  provided.

This is safe enough for trusted local automation because the credential created
for each agent is scoped to that agent. It is not a substitute for sandboxing
untrusted code that has shell access to the user's account.

## Agent PAT Claims / Metadata

Every agent PAT should carry or reference:

- credential id
- token class: `agent_pat`
- audience: `cli | mcp | both`
- agent id
- agent name at issue time
- user id that requested it
- issuer device id
- space/workspace scope
- allowed actions or scopes
- max TTL
- expires at
- created at
- revocation version

The raw PAT should be shown once. The backend should store only a token hash and
metadata.

## API Contract Draft

### `POST /api/v1/credentials/agent-pat`

Request:

```json
{
  "agent_id": "agent-uuid",
  "audience": "cli",
  "expires_in_days": 30,
  "space_id": "space-uuid",
  "name": "demo-agent-cli-local",
  "requested_capabilities": ["messages:send", "context:read", "tasks:write"]
}
```

Authorization:

- user/device JWT from trusted `axctl` device, or
- explicit user approval flow, or
- admin policy.

Response:

```json
{
  "credential_id": "cred_...",
  "token": "axp_a_...",
  "agent_id": "agent-uuid",
  "audience": "cli",
  "expires_at": "2026-05-13T00:00:00Z",
  "issuer_device_id": "dev_...",
  "token_sha256": "display-prefix-only"
}
```

### `POST /auth/exchange`

Existing exchange endpoint should continue to convert PATs into short-lived
JWTs.

Agent PAT exchange response must produce an agent principal:

```json
{
  "access_token": "jwt...",
  "token_type": "bearer",
  "expires_in": 900,
  "principal_type": "agent",
  "agent_id": "agent-uuid",
  "space_id": "space-uuid",
  "audience": "cli"
}
```

## `axctl token mint` Contract

`axctl token mint` should be the primary CLI command for issuing agent PATs.

Required behavior:

- Reject user PAT plus agent identity configuration. This mirrors the UI: a
  user session cannot send messages as an agent unless it first creates and
  uses an agent-scoped credential.
- Refuse routine minting from a raw user PAT once device trust is available,
  except during migration/bootstrap.
- Prefer device credential authorization.
- Resolve agent handle to canonical id.
- Support `--create` for user-approved agent creation.
- Support `--audience cli|mcp|both`.
- Support `--expires`.
- Support `--save-to` and `--profile`.
- Print raw PAT when not saving, because the user must capture it once.
- When `--save-to` or `--profile` is used, hide the raw PAT by default and
  require `--print-token` to display it.
- Never write raw PAT into `.ax/config.toml`.

Example:

```bash
axctl token mint demo-agent --audience cli --expires 30 --profile demo-agent
```

## Approval Policy

The biggest product policy decision:

> Can a trusted device mint agent PATs automatically, or does each new agent
> identity require explicit user approval?

Recommended v1:

- First-time agent creation: explicit approval.
- First-time PAT for an existing agent: explicit approval unless personal-space
  policy allows it.
- Renewal for an already-approved agent identity: silent renewal allowed within
  policy limits.
- Org policy can require approval for every mint.

## HITL Relationship

Agent PAT minting can use the same product pattern as other human-in-the-loop
drafts:

- request is device/user-authored
- proposed artifact is an agent credential
- approval is user/admin-authored
- audit preserves requester, approver, target agent, and device

This is different from giving an agent a user token. The user approves or denies
the credential issuance; the agent only receives the scoped credential if
approved.

## Runtime Rules

Agents must use agent access JWTs for runtime API calls.

Allowed:

- agent PAT -> exchange -> agent JWT -> send as agent
- device credential -> request PAT for agent, subject to policy
- user experience JWT -> approve HITL action in UI

Not allowed:

- agent reads user bootstrap token
- agent uses user JWT to send as itself
- browser user JWT silently becomes agent runtime identity
- user PAT stored in agent worktree for routine operations

## Audit Events

Minimum events:

- `agent_pat.requested`
- `agent_pat.approval_required`
- `agent_pat.approved`
- `agent_pat.denied`
- `agent_pat.issued`
- `agent_pat.exchanged`
- `agent_pat.revoked`
- `agent_pat.expired`

Each event should include:

- user id
- issuer device id
- target agent id
- credential id
- audience
- space id
- policy decision
- reason
- timestamp

## Rotation and Revocation

Required:

- Revoke by credential id.
- Revoke all PATs for one agent.
- Revoke all PATs issued by one device.
- Expire PATs automatically.
- Keep JWT TTL short enough that revocation propagates quickly.
- Operators can rotate an agent PAT with existing CLI primitives:
  list credentials, mint a replacement PAT for the same agent and audience,
  verify the replacement profile, then revoke the old credential id.
- Agent-bound PAT inventory should normally show one active PAT per agent.
  Two active PATs is allowed only as a short rotation window and should produce
  a warning. More than two active PATs for the same agent is a hygiene violation
  and should require explicit cleanup before more credentials are minted.

Standard rotation algorithm:

1. Run `axctl credentials list --json` from a verified user bootstrap login.
   Use `axctl credentials audit` for the operator-friendly active-key report
   and `axctl credentials audit --strict` in automation.
2. Identify the old active credential id, target agent, audience, and
   `last_used_at`.
3. Run `axctl token mint <agent> --audience <same-audience> --expires <days>
   --save-to <new-token-file> --profile <profile> --no-print-token`.
4. Run `axctl profile verify <profile>` and
   `axctl auth whoami --json` using the replacement profile.
5. Only after the replacement works, run
   `axctl credentials revoke <old-credential-id>`.

Do not bulk-revoke live credentials without a verified inventory. If a
credential cannot be matched to a current profile, recent `last_used_at`, or
known runtime, treat it as a cleanup candidate and revoke it deliberately.

Detection and warning signals:

- Active count per bound agent: `1` is normal, `2` is a rotation warning,
  `>2` is a cleanup warning.
- First use from a new device fingerprint, host fingerprint, IP region, or ASN
  should create an alert for the owning user.
- A token used from two locations within an impossible travel window should
  create a high-severity alert.
- A token that has not been used recently but remains active should be shown as
  stale and safe to review for revocation.
- A token used with an unexpected audience, space, or agent binding should be
  rejected when possible and logged as a suspicious credential event.

Fingerprint limits:

- If a credential is used from the expected hashed device/location context, the
  detection signal is weaker. That can mean legitimate use, or it can mean the
  attacker is already inside the same host or user account.
- Same-context token misuse is handled by least privilege, mode `0600` token
  files, profile verification, short JWT TTLs, audit logs, and fast revocation.
  Fingerprinting is not a substitute for endpoint or host security.

Recommended initial TTLs:

- access JWT: 15 minutes
- agent PAT: 30-90 days for local dev, shorter for CI
- bootstrap token: minutes-hours, ideally one-time

## Acceptance Criteria

- `axctl token mint` can mint a scoped agent PAT from trusted user/device context.
- Agent PAT exchange produces an agent principal, never a user principal.
- User PAT plus agent identity config is blocked for CLI/channel/headless MCP
  runtime.
- Agent PAT metadata records issuer device id and created-by user id.
- Raw agent PAT is shown once or stored in OS secure storage / mode `0600` file.
- Runtime sends with user bootstrap token are blocked.
- Runtime sends with agent PAT/JWT are attributed to the bound agent.
- The UI can revoke a device without deleting the agent, and revoke an agent PAT
  without deleting the device.
