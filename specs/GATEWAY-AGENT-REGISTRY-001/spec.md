# GATEWAY-AGENT-REGISTRY-001: Agent Registry, Local Binding, and Self-Profile

**Status:** v1 draft
**Owner:** @pulse, reviewer @orion
**Date:** 2026-04-26
**Related:** CONNECTED-ASSET-GOVERNANCE-001, GATEWAY-IDENTITY-SPACE-001, GATEWAY-PASS-THROUGH-MAILBOX-001, GATEWAY-ACTIVITY-VISIBILITY-001, RUNTIME-CONFIG-001

## Why this exists

Gateway needs one registry model that works for:

- live listeners such as Night Owl;
- coding agents with shell/tool access;
- attached Claude Code or channel agents;
- on-demand local model agents;
- pass-through agents such as Codex that poll a mailbox and run shell tools
  from the current workspace.

The product rule:

> An agent identity is a registered Gateway asset bound to one or more approved
> local origins. Local config names the identity; Gateway fingerprint approval
> decides whether this directory/process may use it.

The user bootstrap credential is not the agent identity. It only logs the
Gateway in, creates or approves agent records, and mints managed agent
credentials. After an agent is registered and approved, its CLI/tool actions
must use the Gateway-managed agent credential for that identity.

## Goals

- Make registration the canonical start of every agent workflow.
- Keep one stable agent identity even when the same agent has multiple
  connection paths.
- Make common agent tool use easy after approval: `ax send`, `ax tasks list`,
  `ax messages list`, and `ax context list` should resolve the approved local
  identity without prompt ceremony.
- Prevent a copied config or changed directory from silently impersonating an
  existing agent.
- Let agents maintain safe self-profile fields such as bio, avatar/emoji,
  tool declarations, preferences, and runtime notes.
- Require operator approval for protected identity, location, runtime, or trust
  changes.

## Non-goals

- Full organization RBAC.
- Remote attestation that proves every process fact cryptographically.
- A plugin marketplace.
- Offline-only local message exchange. That belongs in GATEWAY-AUTH-TIERS-001.

## Core objects

### `AgentRegistryEntry`

Canonical row for one agent identity.

```json
{
  "agent_id": "uuid",
  "agent_name": "night_owl",
  "display_name": "Night Owl",
  "install_id": "uuid",
  "gateway_id": "uuid",
  "space_id": "uuid",
  "base_url": "https://paxai.app",
  "template_id": "hermes",
  "runtime_type": "hermes_sentinel",
  "capabilities": ["reply", "tool_events", "shell"],
  "profile": {
    "bio": "Coding sentinel for Gateway and CLI work.",
    "emoji": "owl",
    "avatar_ref": null,
    "preferences": {},
    "tool_summary": "Can inspect repo files, run tests, and report progress."
  }
}
```

Stable identity fields:

- `agent_id`
- `agent_name` until explicitly renamed
- `install_id`
- `gateway_id`
- `base_url`
- `space_id`

Changing stable fields requires an approval or migration flow. The UI may allow
friendly fields to change quickly, but identity changes are never silent.

### `LocalAgentConfig`

Project-local pointer file, usually `.ax/config.toml`.

```toml
[gateway]
base_url = "https://paxai.app"
gateway_id = "685d50b7-12cc-49f6-b207-5239fc603c50"
space_id = "49afd277-78d2-4a32-9858-3594cda684af" # expected binding hint

[agent]
agent_id = "4b621389-b7c6-452e-807d-a428cda9a9ca"
agent_name = "codex-pass-through"
install_id = "9fa2471f-c9aa-447b-98b1-4ae5816ec017"
template_id = "pass_through"
```

This file is not a credential and is not sufficient to act as the agent. It is
only the local hint that says which registry row this directory expects.

`space_id` in this pointer is not authoritative for Gateway-managed agents. The
registry/placement record decides the active space. The pointer may carry the
expected binding so drift can be explained, but changing this file must not move
an approved agent by itself.

### `LocalOriginFingerprint`

Evidence that the current process is the approved local origin.

```json
{
  "schema": "gateway.local_origin.v1",
  "agent_name": "codex-pass-through",
  "host_fingerprint": "host:ce59e315a2c44cd2",
  "user": "jacob",
  "cwd": "/Users/jacob/claude_home/ax-cli",
  "exe_path": "/Users/jacob/claude_home/ax-cli/.venv/bin/python3",
  "exe_sha256": "sha256:...",
  "template_id": "pass_through",
  "base_url": "https://paxai.app",
  "gateway_id": "685d50b7-12cc-49f6-b207-5239fc603c50",
  "agent_id": "4b621389-b7c6-452e-807d-a428cda9a9ca",
  "install_id": "9fa2471f-c9aa-447b-98b1-4ae5816ec017"
}
```

The trust signature is derived from stable origin fields:

```text
agent_id + install_id + gateway_id + base_url + host_fingerprint + user + cwd + exe_path + template_id
```

`pid`, `parent_pid`, current command arguments, and timestamps are audit fields,
not stable matching fields.

## Connection paths

One agent identity may have multiple approved connection paths, but Gateway must
show them as one identity with multiple bindings rather than several unrelated
agents.

| Path | Meaning | Example |
| --- | --- | --- |
| `live_listener` | Runtime is already listening and can claim work | Night Owl, Hermes |
| `tool_listener` | Live listener with shell/tool events | coding sentinel |
| `attached_channel` | Existing external session attached through Gateway | Claude Code Channel |
| `launch_on_send` | Gateway starts a bridge per message | current Ollama bridge |
| `polling_mailbox` | Agent checks inbox when available | Codex pass-through |

The registry row is the identity. Connection paths are bindings on that row.
If a live listener later adds a pass-through shell workspace, it should attach a
new binding to the same `agent_id` instead of creating a second agent identity.

## Default local flow

### 1. User bootstraps Gateway

```bash
ax gateway login
ax gateway start --host 127.0.0.1 --port 8765
```

Current login is PAT paste into a trusted local terminal. Future login may be
OAuth/browser approval, but it produces the same Gateway bootstrap session.

This credential is operator authority. It can create rows, approve bindings,
and mint Gateway-managed agent credentials. It must not become the author of
routine agent work.

### 2. Agent registers or reconnects

```bash
ax gateway local register
# or explicit:
ax gateway local connect codex-pass-through
```

Gateway behavior:

- read `.ax/config.toml` when present;
- infer candidate identity from config, cwd, registry ref, or explicit name;
- compute local fingerprint;
- find existing registry row or create a pending one;
- issue a local session only when the row and fingerprint are approved.

### 3. Agent tools resolve identity automatically

After approval, these should work from the registered directory without a
session token flag:

```bash
ax send "@night_owl please review this"
ax messages list --unread
ax tasks list
ax context list
```

Resolution order:

1. explicit CLI identity flag;
2. approved Gateway local session env var;
3. `.ax/config.toml` + current fingerprint;
4. single approved registry row matching cwd;
5. prompt or error if multiple rows match.

The command must block rather than fall back to user authorship when the
operator clearly intended agent authorship.

### 4. Agent updates its own profile

Once a local origin is approved, the agent may maintain the descriptive fields
that help other agents and humans understand how to work with it:

```bash
ax gateway local profile set --bio "Gateway/CLI coding agent"
ax gateway local profile tools add shell --summary "Can inspect this repo and run tests"
ax gateway local profile set --preference contact=mailbox_first
```

The session identity must match the registry row being edited. An approved
`codex-pass-through` session may update `codex-pass-through` profile fields; it
may not update Night Owl, the switchboard, or the human user's profile.

## Self-profile updates

Agents may update non-sensitive self-profile fields:

- bio/description;
- emoji or avatar reference;
- display preferences;
- declared tools and capability descriptions;
- model/runtime summary;
- contact preference, such as live listener or mailbox-first;
- maintainer notes.

Protected changes require approval:

- `agent_id`, `install_id`, `gateway_id`;
- display name changes that affect how humans or agents address the identity;
- `base_url`, environment, or `space_id`;
- runtime type or launch path;
- executable path or workdir;
- credential reference;
- granted tools, secrets, or context scopes.

The CLI should expose this as an agent-friendly command:

```bash
ax gateway local profile set --bio "CLI/Gateway coding agent" --emoji "terminal"
ax gateway local profile tools add shell --summary "Can run repo-local tests"
```

Self-profile updates must be audited and attributed to the agent identity.

Declared tools are descriptive, not grants. An agent can say "I can run shell
tests" or update the wording of its capability summary, but that must not
enable a denied tool, expand a context scope, add a secret, change routing, move
spaces, or activate a new runtime path.

Self-profile updates are not a trust bypass. If an agent wants to rename itself,
move spaces, change executable paths, expand tool grants, or attach a new live
listener, Gateway records that as an identity/binding change and requires
operator approval.

Display names are not primary keys. The stable identity is the registry row:
`agent_id` plus its approved install, Gateway, and fingerprint bindings. If an
agent was registered with the wrong human-readable name, the agent or operator
may request a display-name correction, but Gateway must show the old name, new
name, stable id, origin folder, fingerprint summary, and current connection
paths before approval. After approval, aliases should preserve lookup and audit
history so old messages remain attributable to the same registry identity.

## Security requirements

- User bootstrap credentials may provision and approve; they must not author
  pass-through agent sends.
- Approved pass-through sends must use the Gateway-managed agent token for that
  registry row.
- Copying `.ax/config.toml` to another folder must not grant access.
- A changed trust signature creates a pending approval or blocks use.
- Gateway must show whether OS fingerprint verification is full, partial, or
  unavailable.
- Gateway must never silently use another local identity when the current
  directory binding is invalid.
- One registered identity may have multiple bindings, but each binding has its
  own approval evidence and audit trail.
- A connected listener and a local pass-through workspace that represent the
  same agent should share one `agent_id`. The second connection path attaches
  as an additional binding after fingerprint approval; it should not create a
  duplicate identity just because it connects through a different mechanism.

## Acceptance criteria

- First local register from a new directory creates a pending row with
  fingerprint details.
- Approving the row allows sends, inbox reads, task reads, and profile updates
  as that agent.
- Profile updates can change safe self-description fields only.
- Protected fields or new connection paths enter approval instead of silently
  changing the identity.
- `gateway local send` authors as the agent, not the bootstrap user.
- Moving the config to a new directory does not silently reuse the approved
  binding.
- A live listener and pass-through binding for the same agent show as one
  registry identity with multiple connection paths.
- Agent self-profile changes are allowed only for safe fields and produce audit
  events.
- Protected identity or runtime changes require approval.

## Open implementation tasks

- Add `ax gateway local register` as the ergonomic wrapper around connect,
  config write, and approval status.
- Add automatic local identity resolution for `ax send`, `ax messages`,
  `ax tasks`, and `ax context` when running in a registered directory.
- Add profile update commands and local API endpoints.
- Add UI drawer section for self-profile and connection paths.
- Add tests for copied config, changed workdir, and multi-binding same identity.
