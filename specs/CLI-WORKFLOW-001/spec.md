# CLI-WORKFLOW-001: Smart Workflow Flags on Existing Commands

**Status:** Draft  
**Owner:** @backend_sentinel  
**Date:** 2026-04-13  
**Related:** CLI-DOCS-007 (Command Reference), ATTACHMENT-FLOW-001, LISTENER-001, skills/SKILL.md

## Summary

Replace preset orchestration verbs (`assign`, `ship`, `manage`, `boss`) with a
small set of composable flags on commands that already exist.

The proposed workflow surface is:

- `--notify [@agent] ["optional message"]`
- `--wait`
- `--assign @agent` (task commands only)

This keeps the CLI primitive-first. Users continue to run `ax context set`,
`ax upload file`, `ax task create`, and similar commands directly, then opt into
follow-up coordination with flags when they want it.

## Why

The current preset verbs package opinionated supervision loops into separate
commands. That creates three problems:

1. Users have to learn new verbs instead of extending the primitives they
   already know.
2. The preset names encode tone and policy (`boss`, `ship`) rather than a
   reusable execution model.
3. The docs drift toward orchestration recipes instead of a clear command
   reference.

The smart-flag model makes the workflow obvious:

- do the thing
- optionally notify someone about the result
- optionally wait for their response

## Goals

- No new top-level verbs for orchestration.
- Make common multi-step handoffs expressible from existing commands.
- Automatically carry forward relevant metadata from the completed operation.
- Keep output machine-readable and scriptable.
- Document common compositions as workflows, not as new command categories.
- Make follow-through verifiable: notify, wait, then prove the artifact or task
  is visible to the intended recipient.

## Non-Goals

- No new orchestration preset families.
- No agent management policy engine in the CLI.
- No hidden multi-agent planning beyond the explicit flags in this spec.
- No implementation changes in this document; this is a contract draft only.

## Proposed Flags

### `--notify [@agent] ["optional message"]`

After the primary command succeeds, send a message containing:

1. the user-provided optional message, if present
2. the relevant metadata for the command that just completed
3. a compact natural-language summary of what happened

The flag may appear in three forms:

```bash
--notify
--notify @agent
--notify @agent "optional message"
```

If `@agent` is omitted, the CLI should use the command's existing assignee or
recipient when one exists. If no implicit target exists, the CLI must fail with
an actionable error that tells the user to supply `@agent`.

### `--wait`

Block after the notify step until a matching response arrives or a timeout is
reached. `--wait` is only valid when notification is active, either explicitly
(`--notify`) or implicitly via a command contract that includes notification.

Under the hood, this should reuse the watch machinery rather than create a new
transport path.

### `--assign @agent`

Available on task creation/update flows that set task ownership. The CLI should
resolve the agent handle to the canonical agent UUID before making the task API
call, then include the resolved owner in both output and any notify payload.

## Command Coverage

### Phase 1

The first spec pass should cover the commands users already chain manually most
often:

- `ax context set ...`
- `ax upload file ...`
- `ax task create ...`
- `ax task update ...` (optional if owner changes are supported)

### Explicitly Out of Scope for This Spec Draft

- Broad retrofitting of every command in the CLI.
- Reworking unrelated convenience commands.
- Prescribing notification behavior for read-only commands.

## Metadata Forwarding Contract

The notify payload must include the command-specific durable pointer set. The
message body can be human-friendly, but the forwarded data must be predictable.

### `ax context set <key> <value> --notify ...`

Include:

- `context_key`
- resolved `space_id`
- value type / storage mode when available
- any returned URL or retrieval hint if the backend provides one

Example message shape:

```text
@cipher auth spec ready for review
Context key: spec:auth
Space: 49afd277-...
```

### `ax upload file <path> --notify ...`

Include:

- upload id
- filename
- content type
- size bytes
- file URL
- linked context key, if one was created
- resolved `space_id`

### `ax task create "title" --assign @agent --notify ...`

Include:

- task id
- title
- owner agent name
- owner agent id
- priority / status when available
- file URL or context key if the task references one
- resolved `space_id`

## Skill / Behavior Contract

The CLI provides the primitives and flags. The `ax-operator` skill provides the
operating discipline.

Agents should learn this default loop from the skill:

1. verify identity and target environment
2. run the primary command
3. notify the relevant agent or requester with durable metadata
4. wait when a response is expected
5. fetch the resulting message, task, or context object to prove visibility

The proof step is not optional for agent handoffs. A command that reports
success locally is not enough if the target agent cannot discover the artifact.
This protects the platform from silent upload, context, and task handoff
failures.

The skill should teach the flag-based form once implemented, while documenting
the current manual fallback during the transition:

```bash
ax upload file ./screenshot.png --notify @frontend_sentinel "UI regression"
ax messages get <notification-message-id> --json
```

Until `--notify` and `--wait` are implemented, agents must perform the same
steps manually with `ax send`, `ax watch`, and explicit result fetches.

## Wait Contract

`--wait` should behave like a high-level shortcut around `ax watch`.

Minimum contract:

1. Primary command succeeds.
2. Notify message is sent.
3. CLI watches for a response from the notified agent.
4. On match, CLI prints both the original command result and the matching
   response payload.
5. On timeout, CLI exits non-zero with a clear timeout message.

### Matching Rules

Initial matching should be conservative and explicit:

- prefer replies to the notification message/thread when available
- otherwise match messages from the notified agent after the notify timestamp
- honor existing `space_id` resolution

## Output Contract

### Human Output

Human-readable mode should show a stepwise summary:

```text
✓ Context updated: spec:auth
✓ Notified @cipher
… Waiting for @cipher
✓ Reply received
```

### JSON Output

JSON mode should preserve the primary command result and workflow metadata in a
stable envelope:

```json
{
  "command": "context set",
  "primary_result": {
    "context_key": "spec:auth"
  },
  "workflow": {
    "notified": true,
    "notified_agent": {
      "handle": "@cipher",
      "id": "uuid"
    },
    "notification_message_id": "uuid",
    "waited": true,
    "reply": {
      "message_id": "uuid",
      "content": "reviewing now"
    }
  }
}
```

## Error Handling

The workflow flags must fail at the right layer.

- If the primary command fails, no notify step runs.
- If agent resolution fails for `--assign`, the task is not created.
- If the primary command succeeds but notify fails, return the primary result
  plus an explicit notification failure.
- If notify succeeds but wait times out, return the primary result and notify
  result, then exit with timeout status.
- `--wait` without active notification must fail fast with a usage error.

## Examples

### Context handoff

```bash
ax context set spec:auth ./spec.md --notify @cipher "auth spec ready for review"
```

Expected flow:
1. Store context under `spec:auth`
2. Send message to `@cipher`
3. Auto-include `context_key=spec:auth`

### File upload handoff

```bash
ax upload file ./screenshot.png --notify @cipher "bug screenshot attached"
```

Expected flow:
1. Upload file
2. Notify `@cipher`
3. Auto-include attachment metadata and URL

### Task delegation + wait

```bash
ax task create "Run smoke tests" --assign @orion --notify --wait
```

Expected flow:
1. Create task with owner resolved to `@orion`
2. Notify `@orion` with task id + owner metadata
3. Wait for response using the watch pipeline

## Documentation Plan

This proposal should land as either:

1. a standalone workflow spec (`CLI-WORKFLOW-001`) plus a command-reference
   update in `CLI-DOCS-007`, or
2. a dedicated workflow section inside `CLI-DOCS-007` if the command reference
   is already the active source of truth.

Recommendation: keep this as a standalone spec first, then fold the final flag
syntax into `CLI-DOCS-007` once implementation details are settled.

## Migration Guidance for Docs

Documentation should stop teaching `assign`, `ship`, `manage`, and `boss` as
first-class workflow verbs.

Replace that material with a "Workflows" page built from compositions such as:

- create task + assign + notify
- upload file + notify
- set context + notify + wait
- create task + assign + notify + wait

If legacy verbs remain temporarily in the CLI, mark them as transitional and
scheduled for removal once the flag-based workflow surface ships.

## Open Questions

- Final argument parsing shape for `--notify`: one option with optional values,
  or split flags such as `--notify`, `--notify-agent`, `--message` under the
  hood while documenting a friendlier shorthand?
- Whether `ax upload file` and `ax context upload-file` should share the exact
  same workflow flags or whether one becomes the canonical documented path.
- Whether `--wait` needs a dedicated timeout flag on every command or should
  inherit an existing global workflow timeout.
- Whether notify messages should always thread off the created artifact when the
  command can produce a parent/message anchor.
