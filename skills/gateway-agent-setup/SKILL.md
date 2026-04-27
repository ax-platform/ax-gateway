---
name: gateway-agent-setup
description: |
  Create, update, doctor, and supervise Gateway-managed aX assets through the
  local Gateway control plane. Use when an agent needs to set up or modify a
  managed Hermes, Ollama, Echo, Claude Code Channel, or pass-through asset
  without falling back to ad hoc local state.
---

# Gateway Agent Setup

This skill is the setup and maintenance wrapper for Gateway-managed agents.
Gateway is the control plane. The browser UI is a human-readable view of the
same control plane; it is not the only place setup happens.

Use this skill when the task is:
- creating a managed agent
- updating a managed agent's template or launch settings
- running Gateway Doctor after setup
- checking approval, identity, environment, or space state
- verifying a persistent runtime such as Hermes stays healthy

## Principles

1. Bootstrap is human-scoped; runtime is agent-scoped.
   - User PAT/bootstrap login stays in Gateway.
   - Managed agents get their own Gateway-owned runtime token and identity.

2. Prefer Gateway-native templates first.
   - `echo`
   - `ollama`
   - `hermes`
   - `claude_code_channel`
   - `pass_through`

3. Treat setup as an agent-operable workflow.
   - CLI and local API are the primary control surface.
   - UI is a review and intervention surface over the same state.

4. Do not hide setup gaps.
   - If Hermes checkout or identity/space binding is wrong, keep the asset
     blocked and explain why.

## Golden Path

### 1. Make sure the Gateway is up

```bash
uv run ax gateway start
uv run ax gateway status
```

If Gateway is not logged in yet:

```bash
uv run ax gateway login
```

### 2. Inspect templates

```bash
uv run ax gateway templates
```

Pick the template that matches the asset class and intake model you want.

### 3. Add the managed asset

Examples:

```bash
uv run ax gateway agents add echo-bot --template echo
uv run ax gateway agents add northstar --template hermes
uv run ax gateway agents add ollama-bot --template ollama
uv run ax gateway agents add codex-pass-through --template pass_through
```

### 4. Update instead of recreating when possible

Use update when the identity should stay the same but the setup needs to
change.

```bash
uv run ax gateway agents update northstar --template hermes
uv run ax gateway agents update northstar --workdir /absolute/path/to/ax-cli
uv run ax gateway agents update ollama-bot --desired-state stopped
```

### 5. Run Gateway Doctor

```bash
uv run ax gateway agents doctor northstar
```

Doctor is the canonical preflight and repair surface. It should be used after
create/update and before asking humans to trust the asset.

### 5a. Prefer agent-authored tests

Gateway test sends should default to an agent-authored path, not the bootstrap
user identity.

```bash
uv run ax gateway agents test northstar
```

For diagnostics, a user-authored test is still allowed explicitly:

```bash
uv run ax gateway agents test northstar --author user
```

Custom payloads should use the normal send path, not the test path. This is
how to simulate alerting and scheduled inputs such as Splunk, Datadog, or cron
jobs:

```bash
uv run ax gateway agents send switchboard-<space> "Datadog alert: api latency is above threshold" --to northstar
```

### 6. Check approvals when Gateway detects drift or a new binding

```bash
uv run ax gateway approvals list
uv run ax gateway approvals show <approval-id>
uv run ax gateway approvals approve <approval-id> --scope asset
```

For the current demo UI, rejection is row removal or trust revocation rather
than a primary deny button. Use an explicit deny command only if the installed
CLI exposes one.

### 7. Connect pass-through agents as themselves

Pass-through agents are polling mailbox identities, not live listeners. They
must send and read through their own approved Gateway identity, never through
the bootstrap user or a switchboard identity.

```bash
uv run ax gateway local connect codex-pass-through --json
uv run ax gateway local inbox --agent codex-pass-through --json
uv run ax gateway local send --agent codex-pass-through "@night_owl status?" --json
```

If `local connect` returns `pending`, the operator must approve the fingerprint
in the drawer before the agent can send or poll.

The explicit `AX_GATEWAY_SESSION` path is only for low-level session debugging
or older CLI builds. Normal agent instructions should use `--agent` so Gateway
resolves the approved local identity and marks checked mailbox items read by
default.

## Hermes Notes

Hermes is a persistent live-listener asset when healthy. It should stay
running, not cold-start on every send.

Hermes requires:
- a local `hermes-agent` checkout, typically at `~/hermes-agent` or resolved
  through `HERMES_REPO_PATH`
- provider auth or Hermes auth material

If Hermes setup is incomplete:
- Gateway should keep the asset blocked
- Doctor should show a clean setup error
- the runtime should not start and then answer with raw stderr in chat

## Output Standard

When using this skill, always leave the operator with:
- the managed agent name
- current `Mode + Presence + Reply + Confidence`
- whether Doctor passed, warned, or failed
- the exact blocking setup gap if one exists
- the next command or UI surface to use

## Review Checklist

Before handing off:
- the asset exists in Gateway registry
- identity and space binding are visible
- pass-through sends author as the pass-through agent, not the bootstrap user
- Doctor is current
- Hermes/Ollama setup gaps are explicit
- no user bootstrap token is being used as the acting runtime identity
