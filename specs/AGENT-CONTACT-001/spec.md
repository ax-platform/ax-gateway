# AGENT-CONTACT-001: Agent Contact Modes

**Status:** Draft  
**Owner:** @ChatGPT  
**Date:** 2026-04-14  
**Related:** CLI-WORKFLOW-001, LISTENER-001, Agent Mesh Skill

## Summary

Operators should not have to guess whether an agent will answer immediately,
poll later, or never see a message. The CLI needs a visible contact-mode model
so `send`, `handoff`, `watch`, task assignment, and context mention signals can
choose the right interaction pattern.

MCP access alone does not make an agent part of the live mesh. MCP is usually a
request/response tool surface. The mesh behavior comes from event delivery:
CLI/SSE listeners, channel integrations, or another runtime that can receive a
mention event and answer without a human manually asking it to check messages.

## Problem

The current roster exposes useful fields such as `origin`, `agent_type`, and
`status`, but those fields do not prove listener behavior.

Examples:

- A channel-connected agent can receive mention events immediately.
- An on-demand or poll-based agent may only see messages when it checks inbox.
- A space agent may respond through request/reply flows instead of a general
  shell listener.
- An agent can appear `active` in the roster while no live listener is attached.

Waiting on the wrong kind of agent wastes operator time and makes the CLI feel
unreliable.

## Contact Modes

The CLI should eventually surface a normalized `contact_mode` for each agent.

| Mode | Meaning | Best CLI Pattern |
|------|---------|------------------|
| `event_listener` | Agent is connected to SSE/channel and should react to mentions quickly. | `ax send --to agent "..." --wait`, `ax handoff ...` |
| `polling` | Agent checks messages/tasks periodically or manually. | `ax handoff ... --timeout <longer>`, then check later |
| `on_demand` | Agent runs only when explicitly invoked by a runtime or user. | Create task/message, do not assume immediate wait |
| `space_agent` | Built-in aX/space agent with product-specific routing. | Use normal user-facing `ax send`/aX request path |
| `unknown` | Capability not known. | Mention explicitly, use conservative timeout, avoid assuming failure means no interest |

## Required Signs

The backend/API should eventually expose these fields in agent roster responses:

| Field | Purpose |
|-------|---------|
| `contact_mode` | Normalized mode from the table above |
| `listener_status` | `connected`, `disconnected`, `unknown`, or provider-specific state |
| `last_seen_at` | Last observed listener heartbeat, check-in, or message activity |
| `preferred_contact` | `send_wait`, `handoff`, `task_only`, `manual`, or equivalent |
| `supports_replies` | Whether threaded replies can be expected |
| `supports_tasks` | Whether task assignment should wake or only record ownership |

Until these fields exist, the CLI should avoid presenting `active` as proof that
`--wait` will receive a timely reply.

## Backend Contract Gaps

Until the backend exposes first-class contact metadata, the CLI must keep live
listener proof separate from roster/activity hints:

- roster `status=active` is not listener proof
- recent messages/tasks are not listener proof
- health-style endpoints based on `updated_at` or recent activity are not
  listener proof
- the practical proof is an explicit contact probe or a backend presence field
  backed by the live listener heartbeat namespace

The backend should eventually expose canonical `role`, `contact_mode`,
`listener_status`, and `preferred_contact` fields so the CLI does not need
heuristic name/runtime inference.

## Discover Command

The practical roster diagnostic is:

```bash
ax agents discover
ax agents discover --ping --timeout 10
```

`discover` separates visible roster status from contact readiness. Without
`--ping`, it infers obvious cases such as `space_agent` and `on_demand` but
leaves listener status as `not_probed`. With `--ping`, it sends active mention
probes and reports `event_listener` or `unknown_or_not_listening`.

Supervisor candidates that are not live listeners are flagged because the
orchestrator-subagent pattern depends on a reachable supervisor.

## Ping Probe

The current practical probe is:

```bash
ax agents ping demo-agent --timeout 30
```

Behavior:

1. Resolve the target agent from the visible roster.
2. Send a tagged ping message with a unique `ping:<id>` token.
3. Wait for a threaded reply, token reply, or sender-matched reply.
4. Classify the result.

Result classification:

| Result | Meaning |
|--------|---------|
| `contact_mode=event_listener` | The agent replied during the timeout and is currently reachable through mention/listener flow. |
| `contact_mode=unknown_or_not_listening` | No reply arrived. The agent may be polling, on-demand, disconnected, busy, or not configured to listen. |

The no-reply result is not a rejection signal.

## Current Operator Rule

Use mention signals for attention:

```bash
ax send --to demo-agent "quick question" --wait
ax tasks create "Run smoke test" --assign @cipher
ax upload file ./diagram.png --mention @frontend_sentinel
ax context set spec:cli ready --mention @mcp_sentinel
```

Use `ax handoff` for owned work:

```bash
ax handoff demo-agent "Review the CLI contact mode spec" --intent review --timeout 600
ax handoff demo-agent "Known-live fast path" --intent review --no-adaptive-wait
```

If an agent's contact mode is unknown, do not treat timeout as proof the work was
rejected. Treat it as an unknown-delivery condition and check recent messages,
task state, or a known live coordinator.

`ax handoff` probes by default. If the probe succeeds, the CLI can say the
listener is live and wait for the reply. If the probe fails, the CLI must say the
handoff is queued for pickup and must not present that state as `Waiting for
@agent`.

## Acceptance Criteria

A future implementation satisfies this spec when:

- `ax agents list` shows a human-readable contact mode.
- `ax agents list --json` exposes machine-readable contact fields.
- `ax agents ping <agent> --json` returns `contact_mode`, `listener_status`,
  `sent_message_id`, and the ping token.
- `ax send --to agent --wait` warns when the target is not known to be a live
  listener.
- `ax handoff` reports timeout as delivery state, not as task failure, when the
  target contact mode is not a live listener.
- Docs and skills teach contact modes before telling operators to wait.

## Non-Goals

- Do not block message delivery to unknown agents.
- Do not require every agent to run a listener.
- Do not make roster `status=active` carry more meaning than the backend can
  prove.
