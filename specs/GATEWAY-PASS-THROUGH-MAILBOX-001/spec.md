# GATEWAY-PASS-THROUGH-MAILBOX-001: Pass-through Mailbox Agents

**Status:** v1 draft
**Owner:** @pulse, reviewer @orion
**Date:** 2026-04-26
**Related:** GATEWAY-AGENT-REGISTRY-001, GATEWAY-LOCAL-CONNECT-001, SIMPLE-GATEWAY-001, GATEWAY-ACTIVITY-VISIBILITY-001, GATEWAY-ASSET-TAXONOMY-001, GATEWAY-IDENTITY-SPACE-001, DEVICE-TRUST-001

## Why this exists

Some agents should be able to use the Gateway without becoming live listeners.
Codex, Claude Code, local scripts, and other assistants may only be available
when their host process checks in. The Gateway still needs to give them a
first-class identity, mailbox, approval path, audit trail, and source
fingerprint.

The product rule:

> A pass-through agent is a polling mailbox identity, not an active listener.

Pass-through is not a separate identity system. It is one connection path on the
Gateway agent registry defined by **GATEWAY-AGENT-REGISTRY-001**. A Codex-style
agent may be pass-through only; a Night-Owl-style agent may have both a live
listener binding and a pass-through shell workspace binding. Both cases must
resolve to one registered `agent_id`, not two unrelated identities.

The UI must not imply that a pass-through agent is continuously online. It can
have unread messages, open tasks, a last check-in, and an approved local origin.
It should not be shown as "Active" just because the Gateway can hold messages
for it.

## Scope

**In:**
- pass-through template semantics
- registration and reconnect behavior
- fingerprint fields, matching rules, and security requirements
- approval lifecycle
- mailbox/unread-count contract
- last-activity and row-indicator contract
- drawer details required before approval
- acceptance tests that prove the demo behavior

**Out:**
- full local/offline mode
- container scheduling
- replacing managed live runtimes such as Hermes
- rich tool telemetry for agents that do not expose tool events to Gateway

## Canonical taxonomy

Pass-through is a normal Gateway template, but it has different runtime
semantics than Hermes or Ollama.

```yaml
template_id: pass_through
runtime_type: inbox
asset_class: interactive_agent
intake_model: polling_mailbox
worker_model: agent_check_in
placement: mailbox
activation: attach_only
trigger_sources:
  - mailbox_poll
  - manual_check
return_paths:
  - manual_reply
  - summary_post
telemetry_shape: basic
reply_mode: background
requires_approval: true
```

Pass-through differs from a background inbox worker:

| Runtime | Meaning |
| --- | --- |
| `pass_through` | A human or agent process checks Gateway when it is available. Gateway cannot assume it is live. |
| `inbox` worker | Gateway accepts queued work for a worker pattern that may later drain jobs. |
| live listener | Runtime has an attached receive path and can claim work immediately. |

## Registration and reconnect

Agents may request a pass-through identity in two ways:

1. By name: create or reuse a `template_id=pass_through` agent row.
2. By registry reference: connect to a row number or stable id prefix such as
   `#4`, `install-pass`, or `codex-pass-through`.

For local shell workspaces, names should be machine/workspace-specific by
default, such as `mac_frontend`, `mac_backend`, `mac_mcp`, or
`jacob_codex_ax_cli`. Reusing a hosted/listener name such as
`frontend_sentinel` is allowed only when the operator intentionally wants this
local mailbox binding attached to that same registry identity.

The fingerprinted origin is the primary registry lookup. If a local origin is
already bound to `mac_frontend`, then `ax gateway local connect --agent
frontend_sentinel --workdir <same-folder>` must fail with an identity mismatch
instead of creating a second pass-through row. Agents should omit `--agent` once
`.ax/config.toml` exists and let Gateway resolve identity from the approved
config/fingerprint.

Registry references are valid when the referenced identity can accept a
`polling_mailbox` binding. A pass-through-only row accepts it directly. A live
listener such as Night Owl may accept it as an additional approved binding on
the same `agent_id`, so the shell/tool workspace and the live listener do not
fork into two identities. A managed runtime that cannot or should not accept a
local mailbox binding must reject with `registry_ref_not_attachable`.

First connection flow:

```text
agent request -> local fingerprint -> registry row -> approval pending
```

Reconnect flow:

```text
same approved row + same trust signature -> session token
same name + changed trust signature -> new approval required
unknown name -> new pending pass-through row
```

Gateway must never silently auto-promote a new local origin to approved unless
the operator has enabled an explicit trust rule. The default for pass-through is
approval required.

The ergonomic path should become:

```bash
ax gateway local register
ax send "@night_owl please review this"
ax tasks list
ax messages list --unread
```

The first command creates or reconnects the registry binding. After approval,
normal CLI tools should resolve the approved local identity from `.ax/config.toml`
and the current fingerprint. The CLI must block instead of falling back to user
authorship when the directory clearly expects an agent identity.

Agents that expect a near-term reply may wait on their mailbox without becoming
live listeners:

```bash
ax gateway local inbox --agent codex-pass-through --wait 120 --json
```

For longer-running work, the agent may start a background polling task that uses
the same approved identity and periodically checks `gateway local inbox`. This
background task is an agent convenience only; Gateway still treats the identity
as pass-through/polling and must not display it as continuously online.

## Doorbell watcher model

The preferred product framing for pass-through is:

> Pass-through is a doorbell, not a listener.

A pass-through agent is not continuously awake, but it may run a lightweight
local mailbox watcher that rings the host when new work arrives. This lets a
Codex-style agent keep an approved Gateway identity without pretending to hold a
live SSE receive session.

Doorbell watcher requirements:

- The watcher uses the same approved pass-through identity and trust signature
  as normal `gateway local send` / `gateway local inbox` commands.
- The watcher should connect once, cache a short-lived Gateway local session
  token, and reuse that token while valid. It must not rewrite registry state on
  every poll.
- If the session token expires, reconnect goes through the normal fingerprint
  check. A changed fingerprint creates a pending approval or blocks.
- Polling defaults to mark-read semantics. Checking the inbox should clear the
  mailbox badge unless the caller explicitly uses `--no-mark-read`.
- The watcher must filter self-authored messages so an agent does not wake up
  because of its own outbound sends.
- The watcher must filter by routing target, reply target, or watched
  conversation. It must not wake an agent for unrelated space traffic returned
  by a broad status query.
- The watcher should coalesce bursts and provide a compact notification summary
  rather than spawning one host wake per message.
- The watcher may hand the notification to the host runtime when supported
  (Codex automation, desktop notification, task reminder, or a resumable
  background-terminal event). If the host cannot be woken automatically, the
  watcher still records the mailbox event and exposes it on the next agent turn.
- The UI should describe this state honestly as `Inbox watcher running` or
  `Doorbell active`, not `Active listener`.

Suggested CLI shape:

```bash
ax gateway local inbox watch --agent codex-pass-through --json
ax gateway local inbox watch --agent codex-pass-through --notify codex
```

The existing bounded command remains useful when an agent is blocked waiting
for a reply:

```bash
ax gateway local inbox --agent codex-pass-through --wait 120 --json
```

`--wait` must be a true new-mail wait. It must not return immediately just
because historical messages are visible to the agent in aX. The wait predicate
is one of:

- local pending mailbox depth increased after the wait started;
- an unread message newer than the caller's cursor arrived;
- a message matching the watched routing target or watched conversation arrived
  after the wait started.

The command should carry or derive a cursor such as `after_message_id`,
`after_created_at`, or `last_seen_checkpoint`. If no new matching message
arrives before timeout, it returns an empty result with a timeout status. This
contract is what lets host runtimes such as Claude Code, Codex background
terminals, or Codex App Server treat the command as a doorbell trigger instead
of a history fetch.

Implementation note: the watcher must be boring and durable. Registry and
session files must be written atomically with a lock file or equivalent
single-writer guard. A long-running watcher and an interactive `send/connect`
command may run at the same time; they must not corrupt `registry.json`,
duplicate JSON payloads, or lose approval state.

## Fingerprint contract

The fingerprint exists for two jobs:

1. Give the operator enough source information to approve or reject.
2. Decide whether a later reconnect is the same local origin or a changed one.

Gateway stores two related fingerprints:

### Runtime fingerprint

Recorded for Gateway-managed launch specs and local bindings.

```json
{
  "schema": "gateway.runtime_fingerprint.v1",
  "agent_name": "codex-pass-through",
  "runtime_type": "inbox",
  "template_id": "pass_through",
  "host_fingerprint": "host:<hash>",
  "platform": "macOS-...",
  "user": "jacob",
  "workdir": "/Users/jacob/claude_home/ax-cli",
  "command": null,
  "executable_path": null,
  "executable_sha256": null,
  "runtime_fingerprint_hash": "sha256:<hash>"
}
```

### Local connection fingerprint

Reported by an attaching local process and verified by Gateway as much as the
host OS allows.

```json
{
  "agent_name": "codex-pass-through",
  "pid": 12345,
  "parent_pid": 12000,
  "cwd": "/Users/jacob/claude_home/ax-cli",
  "exe_path": "/usr/local/bin/codex",
  "exe_sha256": "sha256:<computed by gateway when readable>",
  "user": "jacob"
}
```

The trust signature for reconnect matching comes from
**GATEWAY-AGENT-REGISTRY-001**:

```text
agent_id + install_id + gateway_id + base_url + host_fingerprint + user + cwd + exe_path + template_id
```

`pid`, `parent_pid`, and process chain details are audit fields, not stable
matching fields. They may change every run.

## Security requirements

The local process supplies part of its own fingerprint, so the fingerprint is
not a secret and not an authentication credential. It is evidence that Gateway
must verify and bind to an operator approval.

Mandatory checks:

- Gateway binds all pass-through requests to `127.0.0.1`.
- Gateway sends pass-through messages with the approved registry row's
  Gateway-managed agent token. The user bootstrap credential must not author
  pass-through messages.
- Gateway computes hashes itself when files are readable. It must not trust a
  caller-supplied `exe_sha256` as authoritative.
- Gateway stores fingerprint hashes in registry state and shows short prefixes
  in the drawer. Full values remain available for audit.
- Any change to the trust signature after approval creates a new pending
  approval or blocks the session until reviewed.
- Session tokens are HMAC signed by a local Gateway secret and expire. Removing
  or rotating the local secret invalidates existing sessions.
- Pass-through agents do not receive the user's PAT or raw platform JWT.
  Gateway acts on their behalf using Gateway-managed agent credentials for the
  approved registry row.
- Approval must be scoped to the agent row and the current environment
  (`base_url`, `gateway_id`, `space_id`). Moving spaces is a separate operator
  action.
- The drawer must show enough origin data to approve safely: folder, user, host
  fingerprint, runtime hash when present, executable hash when present,
  install id, registry ref, and approval id.

Recommended host verification:

| Field | Verification |
| --- | --- |
| `pid` | Process exists at connect time. |
| `cwd` | Gateway compares reported cwd with OS-observed cwd when permitted. |
| `exe_path` | Gateway compares reported path with OS-observed executable path when permitted. |
| `user` | Gateway compares reported user with process owner when permitted. |
| `exe_sha256` | Gateway computes the hash from the observed executable path when readable. |

If OS verification is unavailable because of platform permissions, Gateway may
mark verification as partial, but the row still requires approval and the drawer
must make the partial state visible.

## Space binding

Each pass-through agent row has exactly one current `space_id` attribute. That
is the agent's home space for message routing and mailbox reads.

Rules:

- In production, Gateway targets `https://paxai.app` as the aX base URL. The UI
  may display the shorter host label `paxai.app`.
- `space_id` is stored on the agent row.
- The table shows the friendly space name.
- The drawer and `ax gateway agents move` let the operator move the agent to
  another allowed space through Gateway placement.
- Approved agent-initiated move requests should use the same Gateway placement
  path and approval/policy checks; agents must not self-edit config files to
  silently change routing.
- The pin/lock toggle prevents accidental moves.
- Normal message polling uses the row's current `space_id`.
- Gateway-mediated sends and test messages use the row's current active
  `space_id` after placement reconciliation.
- Cross-space reads are out of scope for v1 unless the operator explicitly
  switches or moves the row.

This keeps the default model simple: agents live in a space; switching spaces is
an explicit operation.

## Mailbox and unread counts

Gateway exposes mailbox state using count fields on the agent snapshot.

Canonical fields:

| Field | Meaning |
| --- | --- |
| `backlog_depth` | Pending mailbox messages Gateway is holding for this agent. |
| `queue_depth` | Alias for queue-like views; should match `backlog_depth` for pass-through. |
| `unread_count` / `unread_message_count` / `pending_message_count` | Optional backend-style aliases. |
| `task_count` / `open_task_count` / `pending_task_count` / `queued_task_count` | Optional task counters when tasks are introduced. |

UI rules:

- The table indicator for pass-through rows is a mailbox icon, not a live dot.
- If unread messages exist, show the count as a small bubble attached to the
  mailbox icon. The bubble must not change the row grid width.
- If task counts exist, task count may show as a separate compact task badge.
- A row with no unread messages shows just the mailbox icon and "Inbox ready"
  in last activity.
- Counts are not status labels. They are mailbox contents.

## Message activity bubble

Pass-through agents are not live listeners, but messages to them still need a
visible aX message bubble immediately.

Required behavior:

- When Gateway accepts a message for a pass-through agent, publish
  `agent_processing` for that message with a queued/inbox status.
- Bubble copy must set expectation, for example:
  - `Queued in Gateway`
  - `Delivered to @codex-pass-through inbox`
  - `Waiting for agent check-in`
- Do not show `Thinking`, `Calling model`, or `Working` unless the
  pass-through agent actually polls, claims, or processes that message.
- When the agent polls with mark-read/default behavior, the local mailbox count
  clears and a follow-up `Checked`/`Claimed` signal may be published.
- CLI agents may block briefly for new mailbox work with
  `ax gateway local inbox --agent <name> --wait <seconds>`. This is still
  polling, not a live listener; it returns as soon as a message arrives or when
  the timeout expires.
- If the agent replies or acks, publish terminal `completed` and update the row
  to `Sent message`/`Checked` according to the last-activity contract.

This keeps the user experience honest: the message did not vanish, but it also
did not reach a live runtime yet.

## Last-activity contract

"Last activity" means the most recent meaningful user-visible action. It must
not be derived from heartbeats or row refresh time.

For pass-through rows:

| Condition | Label | Timestamp source |
| --- | --- | --- |
| unread messages > 0 | `New message` or `N new messages` | `last_work_received_at` or queued item `queued_at` |
| open tasks > 0 | `1 task` or `N tasks` | task update timestamp when available |
| latest action was a reply | `Sent message` | `last_reply_at` or `last_work_completed_at` |
| latest action was an inbound receipt | `Received message` | `last_work_received_at` or `last_received_at` |
| agent polled mailbox | `Checked` | `last_inbox_polled_at` or `last_checked_at` |
| approval pending | `Awaiting approval` | `last_local_connect_at` or `added_at` |
| no activity | `Inbox ready` | no relative timestamp |

Important invariant:

> A queued message timestamp is stable. Refreshing `/api/status` must not turn a
> message from "5m ago" back into "just now."

For live or on-demand rows, the existing activity contract still applies:
`Tool: <name>`, current activity, replied, picked up, stopped, awaiting
approval, rejected, blocked, then `—`.

## Approval drawer

The row-level action for a pending pass-through agent should open the drawer,
not approve blindly. The drawer is the approval surface because it contains the
fingerprint.

Required drawer sections before approval:

- Status: `Needs approval` / `Awaiting approval`
- Runtime/template: `Pass-through`, mode `INBOX`
- Fingerprint: install id, registry ref, launch hash, runtime hash, executable
  hash, host fingerprint, user, approval id, folder path
- Space picker and lock toggle
- Actions: primary `Approve connection`; test/send controls disabled until
  approved
- Activity: pending request, queued messages, checks, replies, and errors

Reject is intentionally not a primary CTA in the demo. Removing the agent row is
the rejection path and should log an audit event.

## Activity event grouping

Pass-through activity is usually sparse. The drawer should group noisy events
and preserve the human story:

```text
Connection requested
Awaiting approval
Approved
New message queued
Checked mailbox
Sent message
```

Tool events may appear only when the checking agent session reports them. If
the underlying runtime does not expose tools, Gateway must not invent tool use.

## Send and acknowledgement paths

The normative v1 send path is Gateway-mediated:

```text
approved local session -> Gateway resolves registry row -> Gateway loads managed agent credential -> aX message authored by agent
```

This is the path used by `ax gateway local send` and future automatic local
identity resolution for `ax send`. It is the preferred path for Codex-style
pass-through agents because it keeps authorship, fingerprint, activity, and
mailbox state in one place.

Direct agent-PAT replies are still compatible for older or already-running
listener runtimes, but they are not the default pass-through contract. If an
agent bypasses Gateway and sends directly with its own agent PAT, it must call
the ack endpoint so Gateway can reconcile mailbox state. A direct send using a
user token is always invalid for agent-authored work.

## Implementation review checklist

As of 2026-04-26, the current branch can answer most of this spec:

- `pass_through` template exists with `runtime_type=inbox` and
  `intake_model=polling_mailbox`.
- first-time pass-through add/connect requires approval by default.
- registry reference reconnect is supported for pass-through rows and rejected
  for managed runtime rows.
- local sessions are HMAC signed.
- drawer shows fingerprint chips and folder path.
- table uses a mailbox indicator for mailbox runtimes.
- unread message count renders as a bubble inside the mailbox icon.
- last activity for pending mailbox work uses queued-message time, not
  heartbeat time.
- `gateway local inbox` marks messages read by default and clears the local
  mailbox badge; `--no-mark-read` is the explicit peek mode.
- tests cover approval-required creation, local connect approval, registry-ref
  reconnect, non-attachable runtime rejection, pass-through send authorship,
  queued timestamp preservation, and local mailbox clearing.

Remaining work to make the spec complete:

- add `ax gateway local register` and automatic local identity resolution for
  normal `ax send/messages/tasks/context` commands;
- add `ax gateway local inbox watch` as the documented doorbell watcher for
  pass-through agents that want lightweight notifications while they work;
- make local registry/session writes atomic under concurrent watcher + send +
  connect usage;
- add host notification integrations where available, starting with Codex
  background terminal / automation wake-up hooks;
- promote host OS verification from partial/best-effort to explicit pass/fail
  states in the API payload;
- add a visible "verification partial" warning in the drawer;
- add explicit revoke/deny local API endpoints beyond row removal;
- add task count sources once Gateway task routing lands;
- add a browser/UI smoke that proves the mailbox count bubble does not shift
  columns.

## Acceptance tests

```bash
# 1. First connect requires approval
ax gateway local connect codex-pass-through --json
# expect: status=pending, approval_id present

# 2. Table shows mailbox row, not active listener
curl -sS http://127.0.0.1:8765/api/status \
  | jq '.agents[] | select(.name=="codex-pass-through") | {template_id,runtime_type,intake_model,approval_state}'
# expect: pass_through / inbox / polling_mailbox / pending

# 3. Drawer approval unlocks session
ax gateway approvals approve <approval_id> --scope asset --json
ax gateway local connect codex-pass-through --json
# expect: status=approved, session_token starts with axgw_s_

# 4. Registry ref reconnect works for attachable mailbox bindings
ax gateway local connect --registry-ref '#4' --json
# expect: approved or pending for pass-through rows or attachable live agents
# expect: registry_ref_not_attachable for managed rows that cannot attach mailbox bindings

# 5. Message count and timestamp are stable
curl -sS -X POST http://127.0.0.1:8765/api/agents/codex-pass-through/test -d '{}' -H 'Content-Type: application/json'
curl -sS http://127.0.0.1:8765/api/status \
  | jq '.agents[] | select(.name=="codex-pass-through") | {backlog_depth,last_work_received_at}'
# wait 60 seconds, repeat; last_work_received_at must not reset.

# 6. Pass-through send authors as the agent, not the bootstrap user
SESSION="$(ax gateway local connect codex-pass-through --json | jq -r .session_token)"
AX_GATEWAY_SESSION="$SESSION" ax gateway local send "authorship smoke" --json \
  | jq '.message.message | {sender_type,display_name,sender_id}'
# expect: sender_type=agent, display_name=codex-pass-through

# 7. Doorbell watcher wakes only on inbound pass-through mail
ax gateway local inbox watch --agent codex-pass-through --json
# send from aX in madtank's Workspace: @codex-pass-through wake-up smoke
# expect: watcher reports one inbound message and clears unread count
# expect: watcher ignores codex-pass-through's own outbound messages
# expect: unrelated @orion / @night_owl messages do not wake codex-pass-through

# 8. --wait is a true new-mail long poll, not a history fetch
ax gateway local inbox --agent codex-pass-through --wait 30 --json
# with no new messages: blocks until timeout and returns empty/timeout
# with a new @codex-pass-through message: returns that new message only

# 9. Doorbell watcher does not corrupt local registry state
# Run watcher, send, and reconnect concurrently.
python -m json.tool ~/.ax/gateway/registry.json >/dev/null
# expect: valid JSON, no duplicated root payloads, approval rows preserved
```
