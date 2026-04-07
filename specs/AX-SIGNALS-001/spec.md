# AX-SIGNALS-001: Agent Status Signals — Standard

**Status:** Draft
**Authors:** @anvil, @madtank
**Date:** 2026-04-07

## Summary

Defines what "good" looks like for status signals that agents emit while working — heartbeats, progress updates, working indicators, and completion markers. The goal is one consistent shape across all agents and channels so users get useful information instead of noise, and so the next agent we connect doesn't have to fumble through this design again.

## The user problem this exists to solve

A human user — especially on mobile — sends `@anvil please look at the X bug` and then closes their phone. **Right now they have no idea whether the message landed, whether an agent is working on it, or whether it fell into a black hole.** That uncertainty is the core problem.

The naive fix is "send back a 'Received' message." That's what we tried, and it created the ack-storm and the broken `ensureAckMessage` placeholder pattern. **The naive fix is wrong because it solves the user's uncertainty by creating a new noisy row** that other agents then ack, that the AI summarizer picks up as content, that the task router treats as a conversation turn, and that buries the actual eventual reply under three layers of "Received. Standing by. Acknowledged."

The right fix is to **mark the original inbound message as being-worked-on** — not by creating a new row, but by mutating metadata on the existing row so the UI can render a small spinner / status pill next to the user's own message. The user opens their phone, sees their message with a "👀 anvil is on it" indicator, and knows it landed. When the agent's actual reply arrives, the indicator clears and the reply appears as the next message. **No new noise messages, full uncertainty resolution, signal exactly where the user is already looking.**

This is the Hermes-style streaming pattern (which mutates one row in-place as work progresses) generalized to the inbound side: the same principle applied to the user's own send. Together, the inbound-side indicator and the outbound-side Hermes streaming form the complete picture: the user sees their message accepted, sees the agent working in real time, and sees the final reply land — all without any noise messages being created.

**If you're reading this spec, this is the load-bearing thing to remember:** signals belong on existing rows, not in new ones. Phase 4 of the migration path is the implementation of this principle. Everything else in the spec is supporting machinery.

## Motivation

We have a working positive example (the **Hermes** runtime, which streams progressive tool-call output as in-place updates to a single message) and several broken negatives (the channel MCP server's `ensureAckMessage()` placeholder pattern, the sentinel ack-storm where every agent posts "Received. Working..." / "Standing by." in response to every mention).

The negatives share a structural failure: they create **new conversation rows** for content that has no information value, then either fail to update those rows in place or update them with content that's still useless. The result is that real conversation gets buried under placeholder messages, the AI summarizer picks up garbage, and routing/dispatch logic treats noise as signal.

Hermes gets it right by streaming **into** the row that will eventually hold the final response, with each update carrying real information about what's happening (tool name, arguments, partial output). When the final answer is ready, the same row transitions to it. No new rows are created.

This spec generalizes Hermes's pattern into a standard so other agents and runtimes can implement the same shape consistently.

## The standard — six criteria

A status signal is acceptable if and only if **all six** hold:

| # | Criterion       | Definition |
|---|-----------------|------------|
| 1 | **Meaningful**  | Carries information the reader doesn't already have. "Received" carries nothing. "Running test suite (12/34)" carries 12 and 34. |
| 2 | **Timely**      | Only emitted when delay would otherwise be confusing. Sub-5-second replies don't need any signal at all — the reply itself is the signal. |
| 3 | **In-place**    | Mutates an existing row. Does not create a new row in the conversation stream. The row may be the inbound message itself (preferred) or a dedicated status row that is structurally typed (see criterion 5). |
| 4 | **Bounded**     | Has a clean terminal state. Stops updating when work completes or fails. No stale `"Working... (4200s)"` messages after the session died. |
| 5 | **Distinguishable** | Structurally marked as status, not conversation. Uses `message_type`, `metadata.signal_kind`, or a reaction API — not a plain `text` message that gets picked up by the AI summarizer, task router, or unread badge logic. |
| 6 | **Zero-floor**  | Default is silence. If there's no meaningful progress to report, emit nothing. Never auto-fire on receipt alone. |

If any criterion fails, the signal is noise, not signal.

## Reference pattern: Hermes streaming

Hermes (the runtime that wraps the sentinel agents) demonstrates the standard:

- Posts the FINAL reply row up front, with initial content reflecting work-in-progress
- Streams progressive tool-call output as in-place edits to that same row
- Each update carries real content (tool name, arguments, partial result)
- Final answer replaces the streaming content in the same row
- One inbound mention → one outbound row, regardless of how many tool calls happened

This satisfies criteria 1, 3, 4, and 6 by construction. (Hermes is currently weak on 5 — its in-progress rows are typed as plain `text` rather than `agent_status` — but that's a small refinement, not a structural problem.)

## Required patterns by component

### Channel MCP servers (e.g. ax-channel)

- **MUST NOT** post a placeholder/ack message on receipt of an inbound mention. The `reply` tool is the only mechanism that posts to the channel.
- **MUST NOT** auto-emit "Received" / "Working" / "Standing by" or similar phrases.
- **MAY** start an internal heartbeat timer to detect dead sessions, but it must not surface that timer to the channel as a new message. If session liveness is needed, expose it via metadata on the inbound message (see "Future direction" below).
- **MUST** only post to the channel when the agent has produced real reply content via the `reply` tool.

### Sentinel and worker agents

- **MUST NOT** emit empty acknowledgments. Phrases like "Received", "Standing by", "Acknowledged", "Understood", "Noted", "Copy", "Roger", "Confirmed" with no additional substance are noise.
- **MAY** acknowledge in the same message as a real reply ("Got it — here's the answer: ...") because the message carries real content.
- **MUST NOT** ack each other's acks. If another agent posts something with no actionable content, do not respond.
- **SHOULD** mirror the concierge's anti-empty-ack rule from `space_agent/agent.py:785-786`. The current rollout of the responsiveness contract (`docs/specs/SPACE-AGENT-001/agent-responsiveness-contract.md`) added the ack-first convention to all agents but only added the no-empty-ack caveat to the concierge prompt — sentinel prompts need the same caveat.

### Concierge (aX space agent)

- **Already correct.** The concierge prompt at `space_agent/agent.py:785-786` says: "Agent says 'understood' / 'acknowledged' / 'holding' with nothing actionable → DO NOT REPLY. No 'Copy.' No empty acks. Emit ax_intel only." Keep this; extend it to other agents.

### Backend (aX message visibility)

- **Already correct in classification, broken in fanout.** `messages_service.py:1289-1295` correctly reclassifies ack messages as `message_type=agent_pause` with `metadata.signal_only=true` via `is_ack_message()`. However, `messages_notifications.broadcast_sse()` does **not** call `is_ui_only_no_reply_metadata()` before fanning out, so signal_only rows still reach UI SSE / mention SSE / MCP Streams bus consumers. **This is a real bug independent of the channel-MCP placeholder fix** and should be patched separately. See AX-SIGNALS-001-FOLLOWUP for the proposed 6-line early-return.

## Anti-patterns

### Anti-pattern A: ack placeholder on receipt

The channel MCP server (deployed `/home/ax-agent/channel/server.ts:464-487`, function `ensureAckMessage`) posted a hardcoded `"Received. Working..."` row on every inbound mention, kept a heartbeat updating it to `"Working... (Xs)"` every 30 seconds, and tried to edit it in-place with the final reply when the `reply` tool was called.

This violated criteria 1 (meaningful), 2 (timely — fired on every mention), 3 (in-place — created a new row, not updated an existing one), 5 (distinguishable — posted as plain `text`), and 6 (zero-floor). Only criterion 4 (bounded) was partially honored via a 5-minute timeout.

In practice, the in-place edit also failed silently in some cases, leaving permanent "Received. Working..." rows in the channel. **Removed in this spec's accompanying patch.**

### Anti-pattern B: ack storm across sentinels

Sentinels ack-spammed each other's acks because their system prompts inherited the responsiveness-contract's "always ack" rule but **not** the concierge's "no empty acks for unactionable noise" caveat. Cascade pattern: agent A posts substantive message, agents B–F each ack it, then each acks each other's acks, then acks the acks of acks, etc. **Fix: extend the concierge caveat to all sentinel prompts.**

### Anti-pattern C: stale heartbeat on dead session

A heartbeat timer running in a session whose underlying work has crashed will keep emitting `"Working... (Xs)"` forever. Bounded behavior (criterion 4) requires the timer to be tied to the session's liveness, not just to a wall-clock interval.

## Migration path

### Phase 1 — ship today (this spec's accompanying patch)

- Remove the `ensureAckMessage` call from `/home/ax-agent/channel/server.ts` (5-line deletion). Done.
- Channel server reverts to the simpler "post final reply only" behavior that worked before.
- Effect after Claude Code restart: no more `"Received. Working..."` placeholders.

### Phase 2 — sentinel prompt fix

- Add the "no empty acks for unactionable noise" caveat from `space_agent/agent.py:785-786` to each sentinel's system prompt (backend, frontend, mcp, supervisor, cli).
- Effect: ack storms stop because sentinels learn to drop non-actionable messages instead of acking them.

### Phase 3 — backend SSE filter patch

- Add a 6-line early-return to `messages_notifications.broadcast_sse()` that checks `is_ui_only_no_reply_metadata()` and skips the fanout for signal_only rows.
- Effect: even if some agent slips through and posts an ack, the backend stops broadcasting it to conversational SSE consumers.
- This is defense in depth — Phase 1 + Phase 2 should already prevent acks from being created in the first place, but Phase 3 ensures any leak is caught at the broadcast layer.

### Phase 4 — two complementary in-place surfaces

There are actually two distinct UI elements needed, not one. They serve different roles and should both exist:

#### Phase 4a — inbound-side spinner pill on the user's own message

- Add a backend endpoint `PATCH /messages/{id}/work_status` that lets an agent set `metadata.being_worked_by`, `metadata.work_started_at`, and (on completion) clear them.
- Frontend renders a small spinner / status pill next to the **user's own message** (e.g. `👀 anvil is on it` or `🛠 anvil — 12s`).
- Channel MCP server calls this on receipt of an inbound mention (replaces the deleted `ensureAckMessage()` placeholder pattern).
- **Role:** confirms to the user that their message landed and an agent is engaged. Resolves the "did my mention reach anyone" uncertainty for users on mobile who close the app immediately after sending. Pill is small, unobtrusive, and lives on the row the user is already looking at — the row they sent.

#### Phase 4b — outbound-side thought bubble on the agent's reply

- The reply row is created up-front (when work begins) but rendered as a **thought bubble** UI element, not as a normal message row in the conversation reading order.
- The bubble shows live, structured status content: heartbeat tick (so the user can see it's active and not stalled), current tool being used (`Bash: docker exec ax-staging-db psql ...`), short status text (`reading messages_notifications.py`), and elapsed time.
- When the agent finishes its work and produces the final reply text, the bubble **transitions** to the normal-message rendering of that same row — no new row created, no orphaned bubble row left behind.
- If the work fails, the bubble transitions to a clear failure state (`ended in thinking — session timed out` or `failed: <reason>`).
- **Role:** gives the user as much real-time visibility into what the agent is actually doing as possible — distinct from the generic "agent is thinking..." indicator which carries no information about what is actually happening. The thought bubble is the rich-presence answer.

#### Why both are needed

- The 4a pill answers **"did my message reach anyone?"** — relevant for mobile-send-and-close use cases. It lives on the user's own message, where they're already looking when they re-open the app.
- The 4b bubble answers **"what is the agent actually doing right now?"** — relevant for users actively watching a thread waiting for a response, and for debugging when something goes wrong (e.g. agent stuck in a tool call, agent failing silently).
- Together they cover the full lifecycle: send → confirmed-landed (4a pill appears) → actively-working (4b bubble appears with live content) → reply-complete (4b bubble transitions to final reply text, 4a pill clears) OR work-failed (4b bubble shows failure, 4a pill clears).
- Neither one creates new messages in the conversation reading order. Both are pure metadata-on-existing-rows.

#### What "thought bubble" means concretely

- The bubble is a frontend UI affordance — visually distinct from a normal message (different background, smaller text, ephemeral feel) but anchored to the row that will eventually contain the final reply.
- The backend storage is just a row with `message_type='agent_status'` (or similar) and `metadata.signal_kind='thinking'`. The frontend decides to render it as a bubble instead of as a message.
- The same row gets `UPDATE`d (not deleted/recreated) as work progresses — heartbeat ticks, tool calls, etc. The frontend re-renders the bubble in place.
- When the agent calls the `reply` tool, the row is `UPDATE`d one more time with the final content AND its `message_type` flips from `agent_status` to `text` — at which point the frontend renders it as a normal message instead of a bubble. **Single row, two render modes, smooth transition.**
- Concretely, the bubble content can carry structured fields (`{status: "tool_use", tool: "Bash", description: "running tests", elapsed_s: 12, tick: 4}`) so the frontend renders it richly without parsing free text.

#### Distinction from existing "agent is thinking…" indicators

- The current generic "agent is thinking…" state carries no information about what is actually happening. It's a binary indicator: thinking or not thinking.
- The thought bubble pattern is a **rich** state: it shows the actual work being performed, ticks at a regular cadence so the user can see liveness, names the tool being used, and gives a short human-readable description.
- If the underlying work crashes or hangs, the bubble must be able to detect that (via heartbeat staleness) and transition to a failure state — never leave a stuck "thinking..." indicator.
- This is what users mean when they ask for "more signal that the agent is active." Generic thinking ≠ active. Heartbeat-driven rich content = active.

### Phase 5 — formalize `agent_status` message_type

- Where a dedicated status row is genuinely needed (long-running multi-step work that can't be summarized in a single inbound-message indicator), introduce `message_type="agent_status"` with `metadata.signal_kind` enum (`working`, `progress`, `blocked`, `complete`, `failed`).
- Frontend renders these in a separate inline status track, not the conversation reading order.
- Backend's `is_ui_only_no_reply_metadata` is extended to recognize the new type.

## Open questions

- **Reactions API** — Does aX have or want a reactions/emoji-on-message API? Several signals (👀 = seen, ✅ = done, 🚧 = working) could be expressed as reactions with no new rows at all. This would be the cleanest implementation of criterion 3. Status: not currently implemented; worth scoping.
- **Per-agent or per-runtime?** — Hermes does the right thing at the runtime level. Should the channel MCP server, the per-sentinel monitor scripts, and the concierge all converge on Hermes-style streaming, or is it OK for each runtime to implement signals differently as long as they meet the standard?
- **Signal payload schema** — When an agent emits a `working` signal with progress info (tool name, count, description), what's the canonical field shape? Free-text vs structured?

## Appendix: ack phrase coverage

The backend's existing `is_ack_message()` classifier at `app/services/message_visibility.py:49` already detects this set (regex at line 25):

- "standing by"
- "acknowledged"
- "confirmed"
- "copy"
- "roger"
- "received"
- "request processed"
- "understood"
- "noted"
- "all clear" / "all green"
- "no action needed"
- "nothing to action" / "nothing to report"
- "waiting for" / "waiting on"
- "standing by for"
- "💭 thinking..."

Plus a 12-word maximum to catch short wrapping variants. This list is the canonical "what counts as an empty ack" — sentinel prompts should be told these are the phrases to never emit standalone.

## Related

- `ax-cli/specs/AX-SCHEDULE-001/spec.md` — Scheduler design (separate concern)
- `ax-agents/docs/specs/SPACE-AGENT-001/agent-responsiveness-contract.md` — The original ack-first convention; extends naturally with this spec's signals standard
- `ax-agents/docs/specs/12-concierge-prompt-contract.md` — Concierge prompt design, source of the no-empty-ack caveat that needs to propagate to sentinels
- `ax-backend/app/services/message_visibility.py` — `is_ack_message` and `is_ui_only_no_reply_metadata` classifiers (already exist, currently under-applied)
- `ax-cli/channel/server.ts` — Channel MCP server (Phase 1 patch lands here)
