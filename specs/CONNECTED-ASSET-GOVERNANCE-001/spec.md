# CONNECTED-ASSET-GOVERNANCE-001: Registry, Provenance, Capabilities, Grants, Secrets, and Approval

**Status:** Draft  
**Owner:** @madtank  
**Date:** 2026-04-22  
**Related:** GATEWAY-CONNECTIVITY-001, GATEWAY-AGENT-REGISTRY-001, GATEWAY-ASSET-TAXONOMY-001, AGENT-PAT-001, DEVICE-TRUST-001, RUNTIME-CONFIG-001, AX-SCHEDULE-001

## Purpose

Define the governance and registry layer above Gateway connectivity and asset
taxonomy.

The existing Gateway specs answer:

- what kind of connected asset this is,
- how work flows through it, and
- whether Gateway can safely route work through it right now.

This spec answers the next control-plane questions:

- Who or what is this asset?
- Where did it come from?
- What is it allowed to do?
- What secrets, tools, and context can it access?
- Which Gateway or device is allowed to run it?
- What changed since approval?
- When is human approval required?
- Which policy, grant, or approval decision allowed an action?

This spec establishes the governance frame for aX as the canonical registry and
Gateway as the trusted local enforcement edge.

## Core Framing

The architectural split is:

- **aX** is the canonical registry, collaboration, context, policy, and audit
  plane.
- **Gateway** is the local execution, enforcement, credential boundary,
  runtime supervision, and real-time signal plane.
- **Connected assets** are agents, workers, jobs, listeners, tools, and
  proxies registered in aX and enforced through Gateway.

Gateway must be treated as an **agent-operable control plane**. The local UI is
a human-readable view over that control plane, not the only place where lifecycle,
approval, doctor, or binding actions exist.

The default setup and maintenance path for managed assets should be an
agent-facing Gateway skill built on these same primitives, not a privileged UI
side path with different semantics.

### Relationship to existing Gateway specs

- [GATEWAY-ASSET-TAXONOMY-001](../GATEWAY-ASSET-TAXONOMY-001/spec.md)
  defines what kind of connected asset this is and how work flows through it.
- [GATEWAY-CONNECTIVITY-001](../GATEWAY-CONNECTIVITY-001/spec.md)
  defines whether the path is safe, healthy, live, stale, queued, blocked, or
  expected to reply right now.
- [GATEWAY-AGENT-REGISTRY-001](../GATEWAY-AGENT-REGISTRY-001/spec.md)
  defines the concrete registry row, local config pointer, local fingerprint,
  connection bindings, and self-profile update model for agents.
- This governance spec defines who controls the asset, how it is approved,
  what it can access, and how drift, grants, secrets, and approvals are
  enforced and audited.

These three layers must remain separate:

- `AssetDescriptor` says **what the asset is**.
- `AgentStatusSnapshot` says **whether its Gateway path is safe/healthy now**.
- `InvocationStatusSnapshot` says **what is happening to one work item**.
- Governance objects say **why this asset is allowed, what it can do, and who
  approved it**.

## Goals

- Make aX the source of truth for connected asset identity, ownership,
  provenance, grants, policy, and audit.
- Make Gateway the local trusted edge that enforces policy, protects
  credentials, validates runtime provenance, and emits real-time evidence.
- Prevent assets from being implicitly trusted just because they exist.
- Allow arbitrary local agents, workers, jobs, listeners, and proxies to be
  registered and governed without forcing them all into one interaction model.
- Support seamless user flows where assets are created in aX, locally bound
  through Gateway, and then enforced and observed without requiring users to
  reason about two disconnected systems.

## Non-goals

- Replacing the current Gateway status model.
- Replacing the asset taxonomy model.
- Making every decision online-only. Gateway may enforce cached policy within a
  defined offline window.
- Building a full production secret manager in v1.
- Designing third-party marketplace governance in v1.
- Supporting arbitrary cross-machine HA Gateways in v1.

## Control-plane Responsibilities

### aX responsibilities

aX is the source of truth for:

- asset identity
- ownership
- workspace/project membership
- asset taxonomy
- capability declarations
- tool manifests
- policy templates
- grants
- secret references
- context access rules
- approval rules
- human-readable registry UX
- audit history

### Gateway responsibilities

Gateway is the local enforcement plane for:

- local runtime launch and attach
- local runtime fingerprinting
- path/process validation
- local secret materialization
- capability-token issuance
- policy enforcement before execution
- tool-call enforcement
- doctor/preflight checks
- revocation enforcement
- real-time telemetry emission
- runtime attestation and drift detection

Gateway should enforce policy received from aX and report evidence back. It
should not become the long-term policy authority.

### Runtime responsibilities

The runtime should remain least-privileged:

- receives assigned work
- receives only scoped local capability or secret access
- emits Gateway events
- returns results
- cannot mint identities
- cannot impersonate another asset
- cannot directly call aX as the user
- cannot self-expand permissions

## Identity Hierarchy

The governance model needs a stable identity chain:

```text
user_id / org_id / workspace_id
  -> gateway_id
    -> asset_id
      -> install_id
        -> runtime_instance_id
          -> invocation_id
            -> tool_call_id
```

### Definitions

- `asset_id`
  - logical registered asset in aX, such as `@hermes-bot`
- `gateway_id`
  - registered Gateway/device/controller
- `install_id`
  - specific local install/binding of an asset to a Gateway
- `runtime_instance_id`
  - concrete running process/session/container
- `invocation_id`
  - one unit of work
- `tool_call_id`
  - one capability/tool action within an invocation

### Identity invariants

- Same `asset_id`, same `gateway_id`, same `install_id`, new
  `runtime_instance_id` is a normal restart.
- Same `asset_id`, same `gateway_id`, different path/hash/launch spec is
  drift.
- Same `asset_id`, different `gateway_id` is a new device/server binding and
  requires approval or policy allowance.
- Unknown `gateway_id` cannot claim a known `asset_id`.
- A runtime cannot claim a different `asset_id` than the one bound to its
  install.

## Registry Objects

### `AssetDescriptor`

Defines what the asset is. This spec extends the taxonomy layer by making the
descriptor a canonical registry object in aX.

### `AssetBinding`

Defines where and how an asset is installed or attached.

Example:

```json
{
  "asset_id": "asset_hermes",
  "gateway_id": "gw_jacob_macbook",
  "install_id": "inst_456",
  "binding_type": "local_runtime",
  "path": "/Users/jacob/hermes-agent",
  "launch_spec": {
    "runtime_type": "exec",
    "command": "python3 examples/hermes_sentinel/hermes_bridge.py",
    "workdir": "/Users/jacob/claude_home/ax-cli"
  },
  "created_by": "user_123",
  "created_via": "web_ui",
  "created_from": "ax_template",
  "approved_state": "approved",
  "first_seen_at": "2026-04-22T18:00:00Z",
  "last_verified_at": "2026-04-22T18:15:00Z"
}
```

### `RuntimeAttestation`

Records what Gateway observed about a concrete runtime instance.

```json
{
  "runtime_instance_id": "rt_789",
  "asset_id": "asset_hermes",
  "gateway_id": "gw_jacob_macbook",
  "install_id": "inst_456",
  "host_fingerprint": "host_sha256:...",
  "path": "/Users/jacob/hermes-agent",
  "launch_spec_hash": "sha256:...",
  "executable_hash": "sha256:...",
  "repo_remote": "git@github.com:...",
  "repo_commit": "abc123",
  "working_tree_dirty": false,
  "environment_profile_hash": "sha256:...",
  "capability_manifest_hash": "sha256:...",
  "attestation_state": "verified",
  "observed_at": "2026-04-22T18:20:00Z"
}
```

### `CapabilityManifest`

Declares broad things the asset can do.

### `ToolManifest`

Declares callable actions exposed by or granted to the asset.

### `Grant`

Represents a permission allowing an asset/runtime/invocation to use a
capability, tool, context, or secret under conditions.

### `Policy`

Represents a decision rule:

- `allow`
- `deny`
- `require_approval`
- `allow_with_limits`
- `allow_once`

### `SecretRef`

Represents a secret without exposing the secret value.

### `ApprovalRequest`

Represents a first-class human approval decision.

### `AuditEvent`

Captures what happened, who or what requested it, and which policy/grant or
approval allowed or blocked it.

## Provenance and Creation

Every asset should have provenance metadata.

Example:

```json
{
  "asset_id": "asset_hermes",
  "created_by": "user_123",
  "created_from": "ax_template",
  "created_via": "web_ui",
  "template_id": "hermes",
  "gateway_id": "gw_jacob_macbook",
  "install_id": "inst_456",
  "source": {
    "kind": "local_repo",
    "path": "/Users/jacob/hermes-agent",
    "repo_remote": "git@github.com:example/hermes-agent.git",
    "commit": "abc123",
    "branch": "staging",
    "launch_spec_hash": "sha256:..."
  },
  "first_seen_at": "2026-04-22T18:00:00Z",
  "last_verified_at": "2026-04-22T18:15:00Z"
}
```

### `created_from`

Must support at least:

- `ax_template`
- `custom_bridge`
- `imported_local`
- `agent_created`
- `api_created`
- `schedule_created`
- `external_integration`
- `mcp_server`

### `created_via`

Must support at least:

- `web_ui`
- `desktop_client`
- `cli`
- `gateway_discovery`
- `agent_assistant`
- `api`

## Runtime Attestation and Drift Detection

Gateway must compare what is running against what was approved.

### Minimum attestation inputs

- `gateway_id`
- host/device fingerprint
- `asset_id`
- `install_id`
- canonical path
- launch command hash
- executable hash
- repo remote
- repo commit
- working tree dirty flag
- container image digest, when applicable
- environment profile hash
- declared tool manifest hash
- declared capability manifest hash

### `attestation_state`

- `verified`
- `drifted`
- `unknown`
- `blocked`

### Attestation semantics

- `verified`
  - runtime matches the approved binding
- `drifted`
  - same asset and gateway, but path/hash/manifest changed
- `unknown`
  - runtime claims an asset but has no approved install binding
- `blocked`
  - runtime violates policy or appears from an unapproved gateway/path

### User-facing copy

- `Verified local install`
- `Changed since approval`
- `New machine requesting access`
- `Unknown runtime blocked`

## Capabilities and Tools

Capabilities and tools must be separated.

- **capability**
  - broad thing the asset can do
- **tool**
  - callable action exposed by or granted to the asset
- **grant**
  - permission allowing a capability/tool under conditions
- **policy**
  - rule deciding whether the request is allowed, denied, or needs approval

### Example capability declaration

```json
{
  "capabilities": [
    {
      "id": "read_repo",
      "risk": "low",
      "resources": ["repo:/Users/jacob/hermes-agent"],
      "requires_approval": false
    },
    {
      "id": "run_shell",
      "risk": "high",
      "resources": ["host:local"],
      "requires_approval": true
    },
    {
      "id": "send_email",
      "risk": "high",
      "resources": ["gmail:jacob"],
      "requires_approval": true
    }
  ]
}
```

### Example tool inventory

- `read_file`
- `search_files`
- `write_file`
- `run_command`
- `open_browser`
- `send_message`
- `create_task`
- `update_task`
- `read_secret`
- `call_mcp_tool`
- `send_email`
- `post_summary`

Important invariant:

**A tool is not a permission by itself. A tool must be backed by a grant.**

## Grants and Policy

### Policy evaluation model

Every request should evaluate:

- `subject`
  - user, asset, runtime instance, gateway
- `action`
  - invoke, claim, read_context, use_tool, read_secret, write_file, post_message
- `resource`
  - workspace, thread, repo, file path, secret, task, external integration
- `conditions`
  - gateway, install, attestation, asset class, risk, time window, branch/path,
    approval state, spend/runtime limits, human presence, and more

### Example policy

```json
{
  "policy_id": "pol_shell_high_risk",
  "effect": "require_approval",
  "subject": {"asset_id": "asset_hermes"},
  "action": "tool.run_shell",
  "resource": "host:local",
  "conditions": {
    "attestation_state": "verified",
    "gateway_id": "gw_jacob_macbook",
    "risk": "high"
  }
}
```

### Toggle rule

Do not model governance only as booleans. UI toggles are acceptable, but they
must compile down to explicit policy or grant decisions.

Examples of user-facing toggles:

- `Allow this once`
- `Always allow for this asset`
- `Allow only on this Gateway`
- `Require approval for shell commands`
- `Block external sends`

These must be backed by persistent policy/grant objects rather than opaque UI
state.

## Vault and Secret Materialization

The user-facing model should remain simple:

- **aX Vault**
  - stores secret references, grants, metadata, policy, and audit
- **Gateway Vault**
  - local encrypted cache/materializer for secrets approved for a specific
    Gateway
- **Runtime**
  - receives only narrow ephemeral material or Gateway-mediated access, not
    broad vault access

### Example secret grant

```json
{
  "secret_ref": "vault://openai/project-key",
  "granted_to": "asset_hermes",
  "gateway_id": "gw_jacob_macbook",
  "scope": ["read"],
  "conditions": {
    "install_id": "inst_456",
    "attestation_state": "verified",
    "allowed_tools": ["llm_call"],
    "expires_at": "2026-04-22T20:00:00Z"
  }
}
```

### Secret invariants

- Runtime should not receive long-lived broad secrets by default.
- Secret access should prefer:
  - short-lived local capability token, or
  - Gateway-mediated access.
- Revoked grants must stop future materialization immediately.

## Human Approval Model

Approval must be policy-driven, not hardcoded per runtime or tool.

### Typical approval triggers

- new asset
- new gateway/device
- runtime drift
- first use of a capability
- first use of a secret
- shell command execution
- writing outside approved path
- sending external messages
- spending money or API quota
- accessing sensitive context
- destructive action
- production environment access

### Example approval object

```json
{
  "approval_id": "appr_123",
  "requested_by": "asset_hermes",
  "gateway_id": "gw_jacob_macbook",
  "action": "tool.run_shell",
  "resource": "repo:/Users/jacob/hermes-agent",
  "risk": "high",
  "reason": "Command writes files",
  "requested_at": "2026-04-22T18:30:00Z",
  "status": "pending",
  "decision": null,
  "expires_at": "2026-04-22T19:00:00Z"
}
```

### Approval decisions

- `approve_once`
- `approve_for_session`
- `approve_for_asset`
- `approve_for_gateway`
- `approve_for_workspace`
- `deny_once`
- `deny_always`

## Context Access Model

The registry must support context permissions alongside tool and secret grants.

Context may include:

- threads and conversations
- documents and repo trees
- task boards
- project notes and memories
- external system mirrors

Grants should define whether an asset may:

- read context
- write context
- summarize or transform context
- cross-post context into another workspace or thread

## Client Creation Flow

Users should not feel like they are operating two disconnected applications.

### Target flow

1. User starts in aX and chooses `Create Agent / Asset`.
2. aX asks:
   - what type is it?
   - what should it do?
   - where should it run?
   - what tools, context, and secrets does it need?
3. aX creates:
   - `AssetDescriptor`
   - initial taxonomy
   - draft policies and grants
   - Gateway install or attach plan
4. Gateway receives:
   - local binding instructions
   - doctor checks
   - approval requirements
5. User confirms local requirements only when needed:
   - local path
   - repo checkout
   - model/runtime
   - secret materialization
6. Gateway reports:
   - `verified`, `drifted`, or `blocked`
   - current `Mode + Presence + Reply + Confidence`

### Agent-assisted creation

Agent-assisted creation is allowed.

Agent self-registration without registry approval is not.

An assistant or agent may generate a proposed asset definition, install plan,
policy draft, and grant request. The user or workspace policy must still
approve it in aX/Gateway before it becomes active.

## Audit Events

Every governance-relevant decision should emit an audit event with evidence.

Examples:

- asset created
- asset bound to gateway
- runtime attested
- attestation drift detected
- grant issued
- grant revoked
- secret materialized
- approval requested
- approval granted/denied
- policy evaluated
- invocation blocked
- tool call allowed/blocked

Each audit event should include:

- actor/subject
- target resource
- decision
- policy/grant/approval identifiers
- gateway/runtime evidence
- timestamp

## UI Surfaces

### aX asset page

Should show:

- asset identity
- taxonomy
- provenance
- current Gateway bindings
- capabilities and tools
- grants and policies
- secret references
- approval history
- audit trail
- current connectivity snapshot

### Gateway local UI

Should show:

- local binding health
- attestation state
- drift reason
- doctor results
- local approvals waiting
- current secret/materialization state
- local capability token scope

Every action exposed in the Gateway UI must also be available through a stable
CLI and/or local API so managed agents can operate the same control plane under
policy, not through UI-only affordances.

### Invocation approval modal

Should clearly answer:

- what is asking?
- from which Gateway/device/path?
- what action/resource is requested?
- why is approval required?
- what scope does approval cover?
- when does it expire?

## Acceptance Tests

Minimum tests for this governance model:

- same asset starts from approved path on approved Gateway -> allowed
- same asset starts from different path -> drifted, approval required for
  sensitive capabilities
- same asset starts from different Gateway -> blocked or approval required
- runtime claims wrong `asset_id` -> blocked and audited
- runtime requests tool not in manifest -> blocked
- runtime requests capability without grant -> blocked or approval required
- runtime requests secret with valid grant -> Gateway materializes scoped access
- runtime requests secret after grant revoked -> blocked
- high-risk shell command -> approval required
- low-risk read-only context access with valid grant -> allowed
- agent-assisted creation -> draft asset and policy request, not active runtime
  power
- Gateway offline from aX -> cached policy enforced only within allowed offline
  window
- approval denied -> invocation blocked with structured reason
- policy changed in aX -> Gateway receives update and revokes local access

## Roadmap

### v1

- Make aX the canonical asset registry.
- Make Gateway enforce asset binding, grants, and local approval decisions.
- Keep PAT bootstrap acceptable, but never expose PATs or broad secrets to
  runtimes.
- Introduce provenance, attestation, grants, secret refs, and approval objects
  in the registry model.

### Later

- richer org/workspace policy templates
- stronger local attestation proof formats
- broader vault providers
- deeper MCP/service proxy governance
- multi-Gateway asset migration and failover policy

## Key Product Rule

Connected assets are not trusted because they exist.

They are trusted because they are:

- registered,
- bound,
- attested,
- granted,
- observed,
- auditable, and
- revocable.

That is the governance layer that lets Gateway and aX stay flexible without
becoming unpredictable.
