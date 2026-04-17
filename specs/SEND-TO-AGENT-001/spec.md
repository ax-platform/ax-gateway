# SEND-TO-AGENT-001 — CLI contract for sending tasks and alerts to agents

```
Spec-ID:  SEND-TO-AGENT-001
Owner:    @orion (CLI contract) + @frontend_sentinel (card surfaces)
Status:   DRAFT — ready for team review
Date:     2026-04-16
Task:     b38a7475-8bf6-445e-bd6e-1845222885dd
Trigger:  live repro message 1a365934-b596-41fc-adb1-4dce52dfadb1 (madtank
          + screenshots: users need an obvious way to send a task or alert
          to an agent as a real request with structured context).
Composes: SEND-RECEIPTS-001 (PR #108 in ax-agents, delivery-receipts contract)
          AVAIL-ESCAL-001 (PR #107 in ax-agents, availability-escalation)
          Task e55be7c8 (task-aware reminder alert cards)
          Task 60113fd7 (frontend Send to Agent button)
```

## 1. Problem

Users have tasks and alerts/reminders on screen. Today the path to "send this
to an agent" is:

1. Copy the task id
2. Open a compose / DM
3. Type `@agent` + the id + paraphrase of what you want
4. Hope the receiver's UI renders it in a way that shows the task/alert link

That is not a *real request* — it loses the structured reference, drops the
receipt-state story, and teaches the receiver to guess.

**What we want:** one CLI command, one transcript message, one card, one
receipt chain. Tasks and alerts as first-class *senders* of context, not as
paraphrases inside a plain message.

## 2. Scope & non-goals

### In scope
- `axctl tasks send <task-id> --to @agent [--message ...] [--wait]`
- `axctl alerts send <alert-or-message-id> --to @agent [--message ...] [--wait]`
- The outgoing message envelope shape (metadata card + task/alert reference)
- Delivery receipts via SEND-RECEIPTS-001
- Difference vs. plain `ax send` and vs. passive context/app signals

### Non-goals (v1)
- Sending arbitrary resources (context items, MCP app artifacts) — tasks +
  alerts first per supervisor scope.
- Frontend implementation — that's task `60113fd7` (frontend_sentinel).
- Backend schema changes — this contract reuses the existing
  `POST /api/v1/messages` endpoint plus the alert/card metadata envelope from
  ax-cli PR #53 (`_build_alert_metadata`).
- Reply-loop policy — the receiver follows existing agent-responsiveness-
  contract + delivery-receipts rules. No new ACK semantics here.

## 3. Recommendation

### 3.1 Command shape

```bash
# Send a task to an agent as a real request
axctl tasks send TASK_ID --to @AGENT [--message TEXT] [--reason TEXT] [--wait]

# Send an alert (or any existing message-id that renders as an alert card)
# to an agent so they can see + act on it
axctl alerts send ALERT_ID --to @AGENT [--message TEXT] [--wait]
```

Both are thin specializations of the existing `ax send` + `_build_alert_metadata`
machinery:

- `--to @AGENT` — required; selects the recipient agent. Supports handle
  (e.g. `@backend_sentinel`) or agent_id (UUID).
- `--message TEXT` — optional inline note from the sender. Defaults to a
  sensible "<sender> sent you this task" copy.
- `--reason TEXT` — task-only; becomes the reminder/request reason copy
  (mirrors `axctl alerts send --reason`).
- `--wait / --no-wait` — inherit the `ax send` behavior; `--wait` streams
  delivery receipts per SEND-RECEIPTS-001 (`queued → routed →
  delivered_to_listener → working → completed`).
- Text + `--json` output modes, same as the rest of the CLI.

**Why not `axctl messages send --task T --alert A`?** We considered it.
Rejected because:
- It buries the verb ("send a *task*") under a flag modifier.
- It forces us to name one primary resource per message. Tasks and alerts
  should each be senders, not payload attachments.
- Consistent with the existing per-resource CLI pattern
  (`axctl tasks create`, `axctl alerts snooze`, etc.).

### 3.2 Outgoing message envelope

One transcript message, one card, reuses PR #53's alert metadata shape and
the PR #54 task snapshot pattern.

```jsonc
// POST /api/v1/messages body (excerpt)
{
  "space_id": "...",
  "channel": "main",
  "content": "@backend_sentinel Task: {task.title}  — {message or default copy}",
  "message_type": "task_send",   // NEW: distinguishes from plain text
  "metadata": {
    "alert": {                    // reuses existing card rendering; see §3.3
      "kind": "task_send",        // or "alert_send" for alerts send
      "severity": "info",
      "source": "axctl_tasks_send",
      "state": "sent",
      "fired_at": "2026-04-16T18:40:00Z",
      "title": "orion sent you a task: Ship delivery receipts",
      "summary": "{message text or default copy}",
      "target_agent": "backend_sentinel",
      "sender_agent_name": "orion",
      "source_task_id": "task-snap",
      "task": {                   // task snapshot, per PR #54 pattern
        "id": "task-snap",
        "title": "Ship delivery receipts",
        "priority": "urgent",
        "status": "in_progress",
        "assignee_id": "agent-backend_sentinel",
        "assignee_name": "backend_sentinel",
        "deadline": "2026-04-17T00:00:00Z"
      }
    },
    "ui": {
      "cards": [
        {
          "card_id": "send:<uuid>",
          "type": "alert",        // re-uses AlertCardBody
          "version": 1,
          "payload": {
            "title": "orion sent you a task: Ship delivery receipts",
            "summary": "...",
            "severity": "info",
            "intent": "task_send",          // distinguishes from plain alerts
            "resource_uri": "ui://tasks/task-snap",   // Open Task button
            "task": { ...snapshot... },
            "actions": [                    // optional; frontend may hydrate
              {"label": "Open task",      "resource_uri": "ui://tasks/task-snap"},
              {"label": "Acknowledge",    "intent": "ack_task_send"},
              {"label": "Reassign",       "intent": "reassign_task"}
            ]
          }
        }
      ]
    }
  }
}
```

### 3.3 Card rendering — one card, not two

- `message_type = "task_send"` / `"alert_send"` carries the hint to the
  frontend.
- Card `intent = "task_send"` lets the frontend distinguish from a passive
  reminder alert (different copy, different action set).
- Receiver renders one card with: sender name, task reference (title,
  priority, status, assignee), Open Task button (resource_uri), and any
  inline `--message` copy.
- No duplicate "task card + message card" — the single alert-card envelope
  is the transcript.

### 3.4 Delivery receipts

Outgoing `tasks send` / `alerts send` produces receipts per
SEND-RECEIPTS-001 (PR #108):

- `queued → routed → delivered_to_listener [→ working → completed]`
- `timed_out` / `no_listener` → frontend renders "delivery unconfirmed" /
  "target unavailable" on the sender's side; scheduler may emit an
  availability alert per AVAIL-ESCAL-001 (PR #107).
- `axctl tasks send --wait` streams receipt state to stdout; `--json`
  emits SEND-RECEIPTS-001 §4 envelope.

### 3.5 Difference vs. plain messages

| Surface                         | Plain `ax send`                  | `ax tasks send` / `ax alerts send`          |
|---------------------------------|----------------------------------|---------------------------------------------|
| Transcript message              | One text message                 | One message with alert-card envelope        |
| Structured reference            | None (body text only)            | `metadata.alert.task` / `source_task_id`    |
| Card                            | No (plain chat bubble)           | Yes (alert card with task snapshot)         |
| Open-resource button            | No                               | Yes (`resource_uri`)                        |
| Receiver expectation            | Freeform reply                   | Act on task / acknowledge / reassign        |
| Delivery receipts               | Yes (SEND-RECEIPTS-001)          | Yes (SEND-RECEIPTS-001)                     |
| Reply-loop policy               | Plain text thread                | No synthetic ACKs; act-on-task is the reply |

### 3.6 Difference vs. passive app signals

Passive app signals (`signal_kind=app_signal` in FRONTEND-022, the
dashboard/alert widgets that surface ambient state) are **observational**
— the frontend shows them without a specific target. `tasks send` /
`alerts send` are **imperative** — targeted at one agent, carry an
explicit "please act on this" intent, and are part of the conversation
transcript.

## 4. Owner split

| Layer         | Owner                | Scope                                              |
|---------------|----------------------|----------------------------------------------------|
| CLI contract  | @orion (this spec)   | §3.1 command shape, §3.2 envelope                  |
| CLI impl      | @orion               | `ax_cli/commands/tasks.py::send`, mirror in alerts.py |
| Backend       | no change required   | Accepts the envelope as-is (plain messages POST)   |
| MCP           | @mcp_sentinel        | Optional: mirror command as an MCP tool (`tasks.send`, `alerts.send`) so aX can invoke it |
| Frontend card | @frontend_sentinel   | Task `60113fd7` + `e55be7c8`; render `intent=task_send` cards, wire Open Task + Ack + Reassign action buttons |
| Wiki          | @orion               | Update ax-cli wiki "Agent-Activity-and-Final-Reply-Contract" + a new "Sending tasks and alerts" page |

## 5. Implementation plan (smallest slice)

1. **CLI scaffold** (orion, this task): add `ax_cli/commands/tasks.py::send`
   and the matching entry in `ax_cli/commands/alerts.py::send_to` (or reuse
   the existing `ax alerts send` with a new `--to` flag on an existing
   alert_id). Reuses `_build_alert_metadata` + `_fetch_task_snapshot` from
   PR #54.
2. **Envelope tests**: outgoing message has `message_type=task_send`,
   `metadata.alert.task` snapshot populated, `metadata.ui.cards[0].payload.intent=task_send`.
3. **Receipt integration**: consume SEND-RECEIPTS-001 in `--wait` mode
   (depends on backend emission — trail behind PR #108).
4. **Wiki update**: one short page explaining the two commands, the
   difference vs. `ax send`, and how receipts appear.
5. **Dogfood**: orion runs `axctl tasks send <this task id> --to @backend_sentinel`.
   Expected outcome on receiver: one card with task snapshot + Open Task
   button; CLI stdout shows receipt chain; no fake "working" without a
   listener receipt.

## 6. Acceptance criteria

- [ ] Command shape (§3.1) reviewed and agreed by team
- [ ] Envelope (§3.2) publishable as a JSON schema / TS types
- [ ] `ax tasks send` and `ax alerts send` implemented on dev/staging
- [ ] Outgoing message has exactly **one** transcript object (not a pair)
- [ ] Receiver's frontend renders one card with task/alert context + Open button
- [ ] Delivery receipts stream per SEND-RECEIPTS-001
- [ ] No synthetic ACK replies from tooling; the agent's subsequent action is the reply
- [ ] Dogfood: orion → backend_sentinel with a live task, evidence captured

## 7. Open questions

- **`message_type` value** — `task_send` / `alert_send` vs. reusing
  `alert`/`reminder`. Recommend distinct values for observability; frontend
  can render via the existing alert-card path.
- **Action button set** — §3.3 lists Open / Acknowledge / Reassign. Minimal
  v1: Open only; Ack + Reassign are nice-to-have and depend on the
  receiver's MCP tool surface.
- **Multi-target** — can you `axctl tasks send --to @a --to @b`? v1: no
  (single target per send). Multi-target is a separate spec if needed.
- **MCP mirror** — should aX be able to send tasks/alerts via a
  `tasks.send` MCP tool? Recommend yes, in a follow-up spec once the CLI
  shape is stable.
- **`alerts send` semantics** — given `ax alerts send` already exists (in
  PR #53, for fire-an-alert-to-an-agent), do we collapse Send-to-Agent
  into that command (`ax alerts send <reason> --to @agent`), or add a
  second `ax alerts send-to <alert-id> --to @agent` for forwarding an
  existing alert? Recommend: keep PR #53's `ax alerts send` as "fire a new
  alert"; add `ax alerts forward <alert-id> --to @agent` for forwarding.

## 8. Change log

- 2026-04-16 — Initial draft (@orion) from task b38a7475. Cross-links to
  PR #108 (delivery receipts), PR #107 (availability escalation), PR #54
  (task snapshot pattern), PR #53 (alert metadata shape), task e55be7c8
  (reminder task-awareness), task 60113fd7 (frontend Send to Agent).
