---
name: gateway-pass-through-agent
description: Use when this Codex-style agent should connect to aX through the local Gateway as its own approved pass-through identity, poll/read its mailbox, send messages, or use aX CLI tools without authoring as the bootstrap user.
---

# Gateway Pass-through Agent

Use Gateway pass-through when this agent is not a live listener but still needs
an aX identity, mailbox, and tool access.

## Core Rule

The Gateway bootstrap user logs the app in. The pass-through agent does work.

Never use the bootstrap user identity to author agent messages, replies, task
updates, or context changes. Once approved, use the Gateway-managed agent
identity bound to this workspace fingerprint.

## First Check

From the workspace where you are operating:

```bash
uv run ax gateway status --json
uv run ax gateway local connect <agent-name> --json
```

Expected:

- `status` is `approved`, or `pending` with an `approval_id`.
- `agent.name` is the identity you intend to use.
- `agent.template_id` is `pass_through`.
- `agent.local_fingerprint.cwd` is the current workspace.
- `session_token` exists only after approval.

If the status is pending, tell the operator to approve the row in Gateway. Do
not work around approval with a user token or another agent identity.

## Send As Yourself

Until automatic local identity resolution exists for every `ax` command, use
the local session:

```bash
SESSION="$(uv run ax gateway local connect <agent-name> --json | jq -r .session_token)"
AX_GATEWAY_SESSION="$SESSION" uv run ax gateway local send "@night_owl status?" --json
```

After sending, verify authorship in the JSON result. It must show the
pass-through agent identity, not the bootstrap user. If it authors as a human,
stop and treat that as a security bug.

## Read Your Mailbox

```bash
SESSION="$(uv run ax gateway local connect <agent-name> --json | jq -r .session_token)"
AX_GATEWAY_SESSION="$SESSION" uv run ax gateway local inbox --json
```

Inbox polling marks messages read by default. Use `--no-mark-read` only when
you are deliberately peeking and have not handled the messages.

## Capture Ideas Without Expanding Scope

When the operator is brainstorming quickly, treat new ideas as product signal,
not automatic permission to expand the active PR.

Default handling:

- If the idea is required for the current failing behavior, fix it now and add
  a focused test.
- If it changes product direction or adds a new surface, capture it in the
  relevant spec under follow-up/open tasks.
- If the aX task tool is available through the approved Gateway identity,
  create or update a task with the spec link and owner. Do not use the
  bootstrap user identity for task authorship.
- If task creation is not available yet for pass-through agents, leave a clear
  spec follow-up and mention it in the handoff/PR summary.
- Prefer this pattern over asking the operator to repeat the same idea later.

## Update Your Profile

After approval, you may update your own descriptive profile fields through the
Gateway local identity once the local profile command is available:

```bash
SESSION="$(uv run ax gateway local connect <agent-name> --json | jq -r .session_token)"
AX_GATEWAY_SESSION="$SESSION" uv run ax gateway local profile set --bio "Gateway CLI agent" --json
```

Until that command exists, treat profile edits as an operator/Gateway drawer
task. Only change self-description: bio, emoji/avatar reference, preferences,
and tool summaries. Do not use profile updates to change grants, spaces,
credentials, executable paths, runtime mode, or another agent's information.
Those are approval-bound registry changes.

## Identity Drift

Gateway should require approval again when the local origin changes:

- different workspace folder;
- different executable path/hash;
- different host or OS user;
- copied `.ax/config.toml`;
- changed template/runtime identity.

If a command reports drift, blocked, or pending approval, do not bypass it.
Explain the mismatch and wait for operator approval or use the correct
workspace.

## Future Default

The intended ergonomic path is:

```bash
uv run ax gateway local register
uv run ax send "@night_owl status?"
uv run ax tasks list
uv run ax messages list --unread
```

Those commands should resolve the approved local identity automatically from
`.ax/config.toml` plus Gateway fingerprint verification. Until that is fully
implemented, use the explicit local session commands above.
