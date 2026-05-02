# AXCTL-BOOTSTRAP-001: Bootstrap and Secure Storage

**Status:** Draft  
**Owner:** @alex / @ChatGPT  
**Date:** 2026-04-13  
**Related:** DEVICE-TRUST-001, AGENT-PAT-001, docs/agent-authentication.md, docs/credential-security.md

## Summary

Define a proposed future device-bound bootstrap flow. This is not the current
user setup path. Current product instructions use `axctl login` for user
bootstrap.

In the future flow, the user starts with a one-time bootstrap token created in
the aX UI. A device-enrollment command uses that token once to enroll a local
trusted device, then discards the raw bootstrap token. After enrollment, normal
CLI operation should use a device-bound credential and agent-scoped PATs, not a
reusable user token.

Critical rule:

> User bootstrap token material must never be readable by agents, plugins,
> background jobs, or future MCP servers.

## Terms

| Term | Meaning |
|------|---------|
| User bootstrap token | Future one-time or short-lived token shown once in the aX UI and pasted into a device-enrollment command. |
| Device credential | Long-lived local credential created during init and bound to a local device keypair. |
| Device keypair | Locally generated asymmetric keypair. Public key is registered with aX; private key remains local. |
| Trusted setup agent | A local automation agent explicitly trusted by the user to run setup commands on the enrolled device. It may invoke `axctl`, but it must not receive raw user bootstrap token material. |
| Agent PAT | Scoped token minted for one agent/audience and used only to exchange for short-lived JWTs. |
| Access JWT | Short-lived bearer token used for actual API calls. |

## Goals

- Make first-run setup boring and safe.
- Avoid placing user PATs in `.ax/config.toml`.
- Avoid making user PATs available to agents.
- Use OS secure storage in v1 instead of inventing a custom vault.
- Provide an upgrade path to hardware-backed keys later.
- Preserve API-first operation: the CLI calls documented backend endpoints.

## Non-Goals

- No custom encrypted vault in v1.
- No agent access to raw user bootstrap tokens.
- No browser-only requirement for CLI setup.
- No requirement for hardware attestation in v1.
- No replacement of existing agent PAT/JWT runtime behavior in this spec alone.

## Flow

```mermaid
sequenceDiagram
    actor User
    participant UI as aX UI
    participant CLI as Future device enrollment
    participant OS as OS Secret Store
    participant API as aX API

    User->>UI: Create bootstrap token
    UI-->>User: Show token once
    User->>CLI: Paste token into masked prompt
    CLI->>CLI: Generate device keypair
    CLI->>API: Consume token + register device public key
    API-->>CLI: Device ID + device credential + revocation handle
    CLI->>OS: Store device credential securely
    CLI->>CLI: Discard raw bootstrap token
    CLI->>API: Verify with device credential
    API-->>CLI: Short user/device JWT
```

## Agent Team Setup Flow

Current product instructions use `axctl login` as the local credential broker
for setting up agent teams. This spec captures a proposed future
device-enrollment flow, not the current CLI bootstrap command.

Implementation status:

- Current shipped CLI supports the compatibility setup path through
  `axctl login` plus `axctl token mint`.
- The device-bound flow in this spec is the target v1
  security model, not the current implementation.
- Current trusted setup agents may invoke `axctl token mint --save-to --profile`
  only inside a user-approved local setup context. They still must not receive
  the raw user PAT in messages, tasks, or context.

Compatibility setup flow:

1. The user runs `axctl login` directly in the trusted shell.
2. The token prompt hides input and then prints a masked receipt, for example
   `axp_u_********`.
3. `axctl` stores the user setup credential in `~/.ax/user.toml`, separate from
   local agent/runtime config.
4. A trusted setup agent may run `axctl token mint` to create scoped agent
   credentials, but it does not read the user's raw token.

```mermaid
sequenceDiagram
    actor User
    participant CLI as axctl on trusted device
    participant Agent as Trusted setup agent
    participant API as aX API
    participant FS as Local secret store / mode 0600 files

    User->>CLI: axctl login
    CLI->>API: Verify user setup credential
    API-->>CLI: User/device authorization
    User->>Agent: Set up @demo-agent, @cipher, @sentinel
    Agent->>CLI: axctl token mint --create --profile --save-to
    CLI->>API: Device/user authorized PAT request
    API-->>CLI: Scoped agent PAT shown once
    CLI->>FS: Store agent PAT and profile
    CLI-->>Agent: Return redacted setup metadata
    Agent->>CLI: ax profile verify <agent-profile>
    CLI->>API: Exchange agent PAT for agent JWT
    API-->>CLI: Verified agent identity
```

Rules:

- The user bootstrap token is consumed by the future device-enrollment command
  and then discarded.
- The trusted setup agent can ask `axctl` to create profiles and scoped agent
  PATs, but it must not read the user bootstrap token.
- If the CLI stores a newly minted agent PAT, command output should default to
  redacted metadata. Raw agent PAT printing requires an explicit flag.
- Each runtime agent receives its own profile and agent-bound credential.
- The backend remains the authority for whether the device/user context may mint
  each agent credential.

Important local trust caveat:

If a setup agent has unrestricted shell access to the same Unix account as the
user, local file permissions alone are not a hard isolation boundary. The v1
security boundary is backend policy plus avoiding raw user-token storage. OS
secret storage, device signing, and hardware-backed keys should progressively
reduce local credential exposure.

## Future Device Enrollment UX

If device-bound bootstrap is implemented later, it should be a separate
device-enrollment flow. Current user instructions should continue to say
`axctl login` until that command exists and is shipped.

Minimum prompt flow:

1. Ask for aX environment URL.
2. Ask for workspace or space selection if not provided.
3. Prompt for bootstrap token using masked input.
4. Generate a local device keypair.
5. Show device fingerprint before final enrollment.
6. Register the device with the backend.
7. Store the device credential in OS secure storage.
8. Verify identity with `auth whoami`.
9. Offer to mint or configure an agent profile.

Example:

```text
future device-enrollment command

aX URL: https://next.paxai.app
Bootstrap token: ********
Device name: Alex's Laptop

Device fingerprint:
SHA256: 4F2A 91C7 9B10 55E0

Authorize this device in aX? [Y/n]
✓ Device enrolled
✓ Credential stored in macOS Keychain
✓ Verified as alex in Alex's Workspace
```

## Storage Contract

Preferred v1 storage:

- macOS: Keychain
- Windows: Credential Manager
- Linux desktop: Secret Service / libsecret

Fallbacks:

- Linux/headless without secret service may use a mode `0600` token file.
- Fallback must print a warning and include the path.
- Fallback should require explicit confirmation unless running in CI mode.

Prohibited:

- Raw bootstrap tokens in `.ax/config.toml`.
- Raw bootstrap tokens in global config.
- Raw bootstrap tokens in agent worktrees.
- Raw bootstrap tokens exposed through `profile env`.

For agent-team setup, stored agent PATs may use mode `0600` files in v1. The
CLI must clearly distinguish these scoped runtime credentials from the user
bootstrap credential.

## Local PIN

A local PIN may be added later as UX hardening, but it is not the trust anchor.

PIN rules:

- PIN may unlock local use of the device credential.
- PIN must not be treated as equivalent to device approval.
- PIN loss should not require account recovery if the user can revoke and
  re-enroll the device.

## API Contract Draft

### `POST /api/v1/auth/bootstrap/consume`

Consumes a user bootstrap token and registers a device.

Request:

```json
{
  "bootstrap_token": "axp_u_bootstrap_...",
  "device_public_key": "base64url...",
  "device_name": "Alex's Laptop",
  "device_fingerprint": "sha256:...",
  "space_id": "optional-default-space",
  "client": {
    "name": "axctl",
    "version": "0.4.0",
    "platform": "darwin-arm64"
  }
}
```

Response:

```json
{
  "device_id": "dev_...",
  "device_credential": "opaque-refresh-or-wrapped-secret",
  "revocation_handle": "rvk_...",
  "user_id": "user-uuid",
  "default_space_id": "space-uuid"
}
```

Server behavior:

- Verify bootstrap token.
- Reject expired, revoked, or previously consumed one-time tokens.
- Store device public key and fingerprint.
- Emit audit event `device.enrolled`.
- Return a credential scoped to this device.

## Bootstrap Token Policy

Recommended v1:

- Bootstrap tokens are shown once.
- Bootstrap tokens have a short TTL, ideally minutes to hours.
- Bootstrap tokens can be explicitly revoked from Settings.
- Bootstrap tokens may be one-time use when practical.

If a future device-enrollment command accepts a longer-lived user PAT for
compatibility, it should classify it as a bootstrap credential and migrate the
user toward device trust.

## Security Properties

Non-negotiable v1 properties:

- User bootstrap token shown once in UI.
- The device-enrollment command uses masked input.
- Bootstrap token is discarded after device enrollment.
- Agents cannot read bootstrap token material.
- Device credential is stored in OS secure storage when available.
- Device enrollment and credential use are audited.
- User can revoke the device independently from agent credentials.
- Trusted setup agents can configure agent profiles without raw user bootstrap
  token access.

## Acceptance Criteria

- The device-enrollment command can enroll a device from a bootstrap token.
- The device-enrollment command never writes the bootstrap token to
  `.ax/config.toml`.
- `axctl auth whoami` works after enrollment without re-pasting the bootstrap token.
- `axctl profile env` never prints a user bootstrap token.
- The backend records `device_id`, public key fingerprint, user id, and created time.
- The UI can show enrolled devices and revoke one device without revoking all agent PATs.
- `ax token mint --save-to` can store a scoped agent PAT without printing it to
  stdout by default.
