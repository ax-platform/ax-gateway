# AGENT-MESH-PATTERNS-001: Shared-State Agent Mesh

**Status:** Draft  
**Owner:** @ChatGPT  
**Date:** 2026-04-14  
**Related:** AGENT-CONTACT-001, CLI-WORKFLOW-001, MESH-SPAWN-001, LISTENER-001

## Summary

aX should treat shared state as the primary multi-agent architecture.

Messages, tasks, context, specs, wiki pages, attachments, credentials, and audit
events are the durable collaboration layer. SSE, mentions, handoffs, and channel
events are the wake-up layer. Supervisors, sentinels, and loops are roles and
coordination patterns on top of that state.

This follows the shared-state pattern described in Anthropic's multi-agent
coordination guidance, while preserving aX-specific constraints around identity,
agent PATs, and human-controlled bootstrap.

Source inspiration:
<https://claude.com/blog/multi-agent-coordination-patterns>

## Chosen Hybrid

The aX mesh is:

```text
Shared state as the system of record
  + message bus for wakeups
  + supervisor role for orchestration
  + generator-verifier loops for bounded iteration
  + agent teams for persistent specialists
```

The shared state is the center. Everything else is a way to update it, react to
it, or summarize it.

## Pattern Mapping

| Pattern | aX Primitive | Use |
|---------|--------------|-----|
| Generator-verifier | `ax handoff --loop --completion-promise` | Bounded iteration where success criteria are explicit |
| Orchestrator-subagent | Supervisor agent dispatching `ax handoff` work | Multi-step work that needs decomposition and synthesis |
| Agent teams | Persistent sentinel/domain agents with agent PATs | Long-running specialized workers |
| Message bus | Messages, mentions, SSE, channel events | Wake agents and deliver events |
| Shared state | Tasks, context, wiki/specs, attachments, audit | Durable memory and coordination substrate |

## Current Probe Evidence

On 2026-04-14, `ax agents ping` showed:

| Agent | Result |
|-------|--------|
| `demo-agent` | `event_listener` |
| `backend_sentinel` | `event_listener` |
| `frontend_sentinel` | `event_listener` |
| `mcp_sentinel` | `event_listener` |
| `cli_sentinel` | `unknown_or_not_listening` |
| `supervisor_sentinel` | `unknown_or_not_listening` |

This is why roster `status=active` must not be treated as listener readiness.
Discovery must distinguish visible roster state from live contact mode.

## Supervisor Role Requirement

The orchestrator pattern only works when the supervisor is explicitly aware that
it is acting as supervisor.

A supervisor is not just an agent with a name containing `supervisor`. It must
have:

- a declared role in roster or configuration
- live listener status when expected to orchestrate
- authority to decompose work into sub-handoffs
- a structured evidence contract for sub-agent results
- timeout and escalation rules
- cycle and recursion limits
- resume semantics after restart

If no live supervisor exists, the operator can still coordinate manually, but
the system should label that as manual orchestration rather than supervisor
mode.

## Shared-State Rules

Agents coordinate by writing durable state, not by relying on transient chat
memory.

- Messages are the visible event log.
- Tasks are the ownership and progress ledger.
- Context is the shared artifact store.
- Specs and wiki pages are durable operating agreements.
- Attachments point to context-backed artifacts when possible.
- Handoff IDs and ping tokens are correlation handles.

If an agent uploads context, creates a task, creates a draft, or changes a
credential, there should be a corresponding message, task event, or audit event
that makes the change discoverable.

## User PAT To Agent PAT Mesh

The user PAT is a bootstrap credential. Its job is to establish user authority
and mint scoped agent credentials.

The safe chain is:

```text
user PAT -> user JWT -> agent PAT -> agent JWT -> runtime actions
```

Rules:

- The user should paste the user PAT only into trusted CLI setup.
- User PATs must not be handed to agents as runtime credentials.
- Agent PATs are scoped to one agent or narrow agent set.
- Agent PATs are runtime credentials, not bootstrap authority.
- Agents must not use agent PATs to self-replicate or mint unconstrained agents.
- Agent creation and credential minting should be auditable shared-state events.

This creates a mesh where the user can spawn agent identities without making
every agent equivalent to the user.

## Discovery Contract

Operators need one command that answers:

- who is in this space?
- what role do they appear to play?
- are they actually listening?
- how should I contact them?
- is a supervisor available?

Current command:

```bash
ax agents discover
ax agents discover --ping --timeout 10
ax agents discover demo-agent backend_sentinel --ping --json
```

`--ping` is an active probe. It sends a mention and waits for a reply. No reply
means `unknown_or_not_listening`, not refusal.

For owned work, `ax handoff` uses the same idea inline by default:

```bash
ax handoff supervisor_sentinel "Coordinate frontend and MCP QA"
ax handoff demo-agent "Known-live fast path" --no-adaptive-wait
```

If the probe succeeds, the CLI waits. If the probe fails, the CLI still creates
the task and message as shared state and returns `queued_not_listening`.

## UI Contract

The CLI state model must remain visible in product surfaces. A folded card or
handoff signal must answer three separate questions:

1. Is the target visible in the roster?
2. Was a live listener confirmed?
3. Was the work saved to shared state?

Minimum labels:

| State | Meaning | Product copy |
|-------|---------|--------------|
| `contact_mode=event_listener` | The target replied to the contact probe. | `Listener: live` |
| `status=queued_not_listening` | The task/message were saved, but no live listener replied. | `Queued for pickup` |
| no probe | The command did not prove live delivery. | `Listener: not probed` |

Do not show `Waiting for @agent` unless a live listener has been confirmed. If
the probe did not reply, use copy such as `Work saved to shared state; listener
not confirmed yet.` This prevents roster visibility from being mistaken for
message delivery.

## Acceptance Criteria

- Shared-state is documented as the primary aX mesh architecture.
- `ax agents discover` exposes role, roster status, listener status, contact
  mode, and recommended contact path.
- Supervisor candidates that are not live are flagged.
- `ax agents ping` remains the single-agent probe.
- `ax handoff` probes by default and uses `--no-adaptive-wait` as the explicit
  opt-out.
- UI surfaces distinguish live listener confirmation from queued shared-state
  work.
- Agent credential spawning is user-bootstrap-scoped and documented as distinct
  from runtime agent identity.
- Docs teach that SSE/mentions are the wake layer, not the state layer.

## Non-Goals

- Do not make every agent a supervisor.
- Do not require every visible agent to be a live listener.
- Do not let agent PATs mint arbitrary new agents.
- Do not replace explicit API permissions with naming conventions.
