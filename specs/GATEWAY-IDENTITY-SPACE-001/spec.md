# GATEWAY-IDENTITY-SPACE-001: Gateway Identity, Space Binding, and Visibility

**Status:** Draft  
**Owner:** @madtank  
**Date:** 2026-04-22  
**Related:** GATEWAY-CONNECTIVITY-001, GATEWAY-AGENT-REGISTRY-001, CONNECTED-ASSET-GOVERNANCE-001, RUNTIME-CONFIG-001, AGENT-PAT-001, IDENTIFIER-DISPLAY-001, CONTRACT-QA-001

## Purpose

Define the v1 contract for how Gateway resolves, verifies, shows, and enforces:

- which identity it is acting as,
- which environment it is targeting,
- which space that identity is currently operating in,
- which spaces that identity is allowed to access, and
- when Gateway must block a send/listen action because those facts do not line
  up.

This spec exists because a multi-agent machine can hold many local identities,
many spaces, and many environments at once. The Gateway must make that visible
and safe.

The central product rule is:

> Gateway must never silently borrow another identity or silently target an
> unexpected space.

If an asset does not have a valid identity binding for the requested
environment and space, Gateway should block the action and surface the setup gap
explicitly.

## Relationship to other specs

- [GATEWAY-CONNECTIVITY-001](../GATEWAY-CONNECTIVITY-001/spec.md)
  defines whether Gateway can safely route work right now using `Mode +
  Presence + Reply + Confidence`.
- [CONNECTED-ASSET-GOVERNANCE-001](../CONNECTED-ASSET-GOVERNANCE-001/spec.md)
  defines asset registry, provenance, approvals, attestation, grants, and
  audit.
- [GATEWAY-AGENT-REGISTRY-001](../GATEWAY-AGENT-REGISTRY-001/spec.md)
  defines the local registry row, `.ax/config.toml` pointer, local origin
  fingerprint, and self-profile model.
- This spec defines the missing identity-and-space layer between them:
  - who Gateway is acting as,
  - where it is acting,
  - whether that space is allowed for that identity,
  - and how that should be shown to the operator and sender.

These layers must remain separate:

- **AssetDescriptor** says what the asset is.
- **AgentStatusSnapshot** says whether Gateway can route to it now.
- **Identity/Space binding** says who it is acting as and where that identity is
  valid.

## Goals

- Make acting identity explicit in Gateway UI, CLI, and local API.
- Make active space, default space, and allowed spaces visible everywhere an
  operator can send or review work.
- Ensure every managed asset has its own identity binding per environment.
- Prevent hidden fallback from one local identity to another.
- Prevent hidden fallback from a user bootstrap credential to an agent runtime
  identity.
- Block sends/listens when the active space is not allowed for the acting
  identity.
- Give Doctor and onboarding a deterministic contract for verifying identity and
  space access.

## Non-goals

- Designing the full agent registry or policy engine.
- Replacing `Mode + Presence + Reply + Confidence`.
- Solving full org-level RBAC in this spec.
- Defining the full OAuth model.
- Defining all multi-org or cross-tenant behavior.

## Core framing

Gateway must track three separate layers:

1. **Bootstrap credential**
   - the human login or trusted device credential used to provision agent
     identities
2. **Acting identity**
   - the asset identity Gateway is currently using for sends, listens, or
     status updates
3. **Space binding**
   - the space Gateway is currently targeting and the list of spaces that
     acting identity is allowed to access

These must not be conflated.

### Wrong behavior

- `codex` cannot speak in prod, so Gateway silently reuses `night_owl`
- a repo-local home config changes the active identity without the operator
  noticing
- Gateway shows an agent as healthy but hides that it is pointing at the wrong
  space
- sender actions succeed through a user credential path when the operator
  expected agent-authored behavior
- a pass-through agent sends through the Gateway bootstrap user session instead
  of its own Gateway-managed agent credential

### Correct behavior

- Gateway shows `acting as codex`
- Gateway shows `environment: prod`
- Gateway shows `current space: ax-cli-dev`
- Gateway shows `allowed spaces: ax-cli-dev, madtank's Workspace`
- if `codex` has no prod binding or no access to `ax-cli-dev`, Gateway blocks
  the action and says so
- once approved, `codex` sends, reads tasks, and polls messages through its own
  managed agent identity from that registered directory

## Definitions

### Acting identity

The concrete asset identity Gateway is using for an action such as:

- listening for messages
- sending a direct message
- replying in a thread
- posting processing status
- claiming work

Examples:

- `codex`
- `night_owl`
- `cli-managed-bot`

### Environment

The backend host and auth context where the identity is operating.

Examples:

- `dev.paxai.app`
- `paxai.app`
- local

### Active space

The concrete space a send/listen action is currently targeting.

### Default space

The space the identity would use if no explicit target space is chosen.

### Allowed spaces

The list of spaces the acting identity is permitted to operate in according to
the backend.

### Active space source

How Gateway resolved the active target space for the current action:

- `explicit_request`
- `gateway_binding`
- `visible_default`
- `none`

### Space status

The evaluation of whether the active space is valid for the acting identity:

- `active_allowed`
- `active_not_allowed`
- `no_active_space`
- `unknown`

### Environment status

The evaluation of whether the requested environment and the bound environment
line up:

- `environment_allowed`
- `environment_mismatch`
- `environment_unknown`
- `environment_blocked`

## Canonical model

### `GatewayIdentityBinding`

Describes the identity Gateway should use for one asset in one environment.

```json
{
  "identity_binding_id": "idbind_codex_prod_ax_cli_dev",
  "asset_id": "db1f2a10-cdb2-4fce-a028-7fe0edb2d08f",
  "gateway_id": "gw_jacob_macbook",
  "install_id": "inst_codex_prod_local",
  "environment": {
    "base_url": "https://paxai.app",
    "label": "prod",
    "host": "paxai.app"
  },
  "acting_identity": {
    "agent_id": "e9877470-5e3a-4f08-869d-22fc86b2e063",
    "agent_name": "codex",
    "principal_type": "agent"
  },
  "active_space_id": "ed81ae98-50cb-4268-b986-1b9fe76df742",
  "default_space_id": "ed81ae98-50cb-4268-b986-1b9fe76df742",
  "credential_ref": {
    "kind": "token_file",
    "id": "cred_codex_prod",
    "display": "Gateway-managed agent token",
    "path_redacted": "~/.ax/runtimes/codex-prod/.ax/token"
  },
  "binding_state": "verified",
  "created_via": "gateway_setup",
  "created_from": "ax_template",
  "last_verified_at": "2026-04-22T23:00:00Z"
}
```

`credential_ref` is a non-sensitive display contract for UI, audit, and local
API use. Full local token paths or other secret material references should
remain in Gateway-private state only and must not be casually exposed through
operator-facing surfaces.

### `IdentitySpaceBindingSnapshot`

Derived status object for UI/CLI/runtime decisions.

```json
{
  "identity_binding_id": "idbind_codex_prod_ax_cli_dev",
  "asset_id": "db1f2a10-cdb2-4fce-a028-7fe0edb2d08f",
  "gateway_id": "gw_jacob_macbook",
  "install_id": "inst_codex_prod_local",
  "acting_agent_id": "e9877470-5e3a-4f08-869d-22fc86b2d08f",
  "acting_agent_name": "codex",
  "principal_type": "agent",
  "base_url": "https://paxai.app",
  "environment_label": "prod",
  "environment_status": "environment_allowed",
  "active_space_id": "ed81ae98-50cb-4268-b986-1b9fe76df742",
  "active_space_name": "ax-cli-dev",
  "active_space_source": "gateway_binding",
  "default_space_id": "ed81ae98-50cb-4268-b986-1b9fe76df742",
  "default_space_name": "ax-cli-dev",
  "allowed_spaces": [
    {
      "space_id": "ed81ae98-50cb-4268-b986-1b9fe76df742",
      "name": "ax-cli-dev",
      "is_default": true
    }
  ],
  "allowed_space_count": 1,
  "space_status": "active_allowed",
  "identity_status": "verified",
  "last_space_verification_at": "2026-04-22T23:00:00Z"
}
```

### Optional status values

#### `binding_state`

- `verified`
- `unbound`
- `mismatch`
- `blocked`

#### `identity_status`

- `verified`
- `unknown_identity`
- `credential_mismatch`
- `fallback_blocked`
- `bootstrap_only`
- `blocked`

#### `environment_status`

- `environment_allowed`
- `environment_mismatch`
- `environment_unknown`
- `environment_blocked`

#### `space_status`

- `active_allowed`
- `active_not_allowed`
- `no_active_space`
- `unknown`

#### `active_space_source`

- `explicit_request`
- `gateway_binding`
- `visible_default`
- `none`

## Required rules and invariants

### 1. Explicit acting identity

Gateway must always know and show which identity an action will use.

Required surfaces:

- fleet view drill-in
- message send composer
- doctor output
- local status API

### 2. Per-asset identity bindings

Each managed asset must have its own binding per environment.

Examples:

- `codex` on prod
- `codex` on dev
- `night_owl` on prod

These are distinct bindings and must not be merged implicitly.

### 3. No silent cross-identity fallback

Gateway must not silently fall back from:

- one agent identity to another
- a managed agent identity to a human bootstrap identity
- one environment binding to another environment binding

If the requested identity is missing, invalid, or blocked, the action must fail
closed and show the problem.

### 4. No hidden space fallback

Gateway must not silently rely on:

- browser/session "current space"
- a stale home-level default space
- an unrelated repo-local space

The active space used for send/listen must be explicit or derivable from a
visible binding rule.

Gateway should resolve active space using this deterministic precedence:

1. explicit send/listen target space, if provided
2. explicit Gateway identity binding `active_space_id`
3. visible binding `default_space_id`
4. otherwise `no_active_space`

Every resolved `active_space_id` must still be checked against
`allowed_spaces`. A visible default is convenience only; it does not authorize
access by itself.

### 5. Active space must be allowed

Before Gateway sends, listens, or claims work, it must verify that the active
space is in the acting identity's allowed-space list.

If not:

- block the action
- mark `space_status = active_not_allowed`
- surface `confidence = BLOCKED`
- set a structured reason

### 6. Default space is informational, not a hidden permission bypass

`default_space_id` is useful for setup and convenience, but it does not
override explicit target spaces and it does not authorize anything by itself.

### 7. User bootstrap credential is not a runtime identity

The user PAT or user login session may bootstrap, verify, or mint agent
credentials, but after binding it must not be used for normal agent-authored
operations.

If Gateway is about to perform an agent action through a user identity, it must
surface that as a blocked or warning state rather than silently proceeding.

For normal agent-authored operations, `bootstrap_only` should be treated as
blocked. Bootstrap credentials may be used only for setup, verification,
minting, or repair flows.

### 8. Identity-space binding must be verified before agent-authored actions

Gateway must verify identity-space binding before:

- sending
- listening
- claiming work
- posting status
- replying
- emitting agent-authored lifecycle events

Identity-space verification is not optional just because runtime presence or
delivery plumbing is healthy.

## Integration with connectivity

This spec extends the operator truth from:

- `Mode + Presence + Reply + Confidence`

to include visible identity and space context:

- `Acting As`
- `Environment`
- `Current Space`
- `Allowed Spaces`

### Confidence integration

The following structured `confidence_reason` values should exist or be added:

- `identity_unbound`
- `identity_mismatch`
- `bootstrap_only`
- `environment_mismatch`
- `active_space_not_allowed`
- `no_active_space`
- `space_unknown`

Recommended mapping:

- `identity_unbound` -> `BLOCKED`
- `identity_mismatch` -> `BLOCKED`
- `bootstrap_only` -> `BLOCKED`
- `environment_mismatch` -> `BLOCKED`
- `active_space_not_allowed` -> `BLOCKED`
- `no_active_space` -> `LOW` or `BLOCKED`
- `space_unknown` -> `LOW`

### Reachability integration

Even if `mode = LIVE`, Gateway should not claim the asset is safely reachable if
the identity or space binding is invalid.

Examples:

- `LIVE · IDLE · REPLY · HIGH`
  - valid live listener, valid acting identity, active space allowed
- `LIVE · OFFLINE · REPLY · LOW`
  - valid identity binding, active space allowed, runtime not live
- `LIVE · BLOCKED · REPLY · BLOCKED`
  - acting identity mismatch or active space not allowed

## UI and CLI requirements

### Fleet view

Fleet rows should show:

- `Asset`
- `Type`
- `Mode`
- `Presence`
- `Output`
- `Confidence`
- `Acting As`
- `Current Space`
- `Allowed Spaces`

Compact example:

```text
codex  Live Listener  LIVE  IDLE  Reply  HIGH
acting as codex · prod · ax-cli-dev · 1 allowed space
```

### Drill-in

Drill-in should show:

- acting identity name and id
- environment/base URL
- active space name and id
- default space name and id
- allowed spaces list
- last verification time
- identity status
- space status

### Composer / send controls

Before send, the UI should show:

- `Sending as codex`
- `Environment: prod`
- `Target space: ax-cli-dev`

If the target is not allowed, the send button must block.

### Doctor

Doctor must verify:

- acting identity exists
- acting identity matches expected asset
- requested environment matches the bound environment/base URL
- active space resolves
- active space is allowed
- allowed spaces list is fetchable
- default space, if present, is consistent with backend response
- the credential in use is agent-authored rather than bootstrap-only

## Setup and onboarding

### First bind

When connecting an asset to Gateway in a new environment, setup should produce:

- identity binding
- active/default space selection
- allowed-space verification
- a visible summary before enabling send/listen

### Existing asset, new environment

If `codex` exists in prod but has no usable local runtime credential yet:

- Gateway should say `prod binding missing` or `prod runtime credential missing`
- Gateway should not fall back to another identity such as `night_owl`

### Existing asset, wrong current space

If the identity is valid but the target space is not in `allowed_spaces`:

- show the target space
- show the allowed spaces
- block the action

## Audit expectations

Gateway should emit audit-worthy events for:

- `identity_binding_verified`
- `identity_binding_missing`
- `identity_mismatch_detected`
- `space_binding_verified`
- `space_binding_blocked`
- `fallback_blocked`
- `bootstrap_identity_blocked`

Every event should include:

- `gateway_id`
- `asset_id`
- `install_id`
- `identity_binding_id`
- `acting_agent_id`
- `acting_agent_name`
- `runtime_instance_id`, when available
- `base_url`
- `environment_status`
- `active_space_id`
- `active_space_source`
- `default_space_id`
- `allowed_space_ids`
- `decision`
- `reason`
- `observed_at`

Additional audit-worthy events:

- `environment_mismatch_detected`
- `active_space_resolved`
- `identity_space_snapshot_updated`

## Acceptance tests

### Identity correctness

- A managed `codex` binding for prod resolves `acting as codex`, not
  `night_owl`.
- A managed `codex` binding for prod never silently uses a valid dev binding.
- Gateway blocks a `codex` send if only `night_owl` has a valid binding.
- Two installs for the same asset on the same Gateway are distinguished by
  `install_id`, not only `asset_id`.
- A user bootstrap token can verify or mint but cannot be used as the acting
  runtime identity for normal sends.

### Space correctness

- A valid prod binding with `active_space = ax-cli-dev` and
  `allowed_spaces = [ax-cli-dev]` is `active_allowed`.
- A binding whose target space is not in the allowed-space list is blocked.
- A missing active space is surfaced explicitly and does not silently use a
  hidden backend current-space fallback.
- An explicit `--space` target overrides the visible default but must still be
  in `allowed_spaces`.
- A backend or browser "current space" that is not present in the Gateway
  binding is ignored.

### Visibility

- Fleet view shows acting identity and current space.
- Drill-in shows active, default, and allowed spaces.
- Composer shows `Sending as <identity> in <space>`.

### Fallback blocking

- If a home-level config for `night_owl` exists and `codex` is requested,
  Gateway blocks instead of silently using `night_owl`.
- If a repo-local config points to the wrong environment, Gateway blocks or
  warns rather than silently crossing environments.
- An environment mismatch emits `environment_mismatch_detected`.

### Multi-space agents

- An identity with two allowed spaces shows both spaces and a count.
- Changing the active space updates the visible target before send/listen.

### Enforcement

- Gateway blocks status posting if the acting identity binding is invalid.
- Gateway blocks claim if the resolved active space is not allowed.
- `bootstrap_only` is allowed for doctor/setup flows but blocked for
  send/listen/claim/reply/status.

## First implementation slice

The first implementation slice for this spec should be:

- `IdentitySpaceBindingSnapshot`
- Doctor identity/space verification
- send/listen/claim blocking on invalid identity or space binding

Minimum local objects:

- `GatewayIdentityBinding`
- `IdentitySpaceBindingSnapshot`

First enforcement cases:

- requested `codex` + valid prod `codex` binding + allowed space -> allow
- requested `codex` + only `night_owl` binding exists -> block
- requested `codex` + valid credential + disallowed active space -> block
- requested `codex` + requested prod but repo/home config points to dev -> block
- requested agent action would use bootstrap credential -> block

This slice should not require the full grants, vault, or policy engine before
it is useful.

## Roadmap

### v1 minimum

- Local identity-space snapshot in Gateway state
- Visible acting identity and current space in fleet/drill-in
- Doctor checks for identity and allowed spaces
- Block silent fallback to another identity
- Block sends to disallowed spaces

### Later

- Full aX-backed canonical identity binding registry
- Approval policy for first-time space bindings
- UI-based identity/space switching flows
- Cross-environment identity dashboards
- Richer org/workspace policy overlays
