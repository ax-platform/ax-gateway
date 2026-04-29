# GATEWAY-ASSET-TAXONOMY-001: Gateway Asset Taxonomy and Flow Semantics

**Status:** Draft  
**Owner:** @madtank  
**Date:** 2026-04-22  
**Related:** GATEWAY-CONNECTIVITY-001, AGENT-CONTACT-001, LISTENER-001, AX-SCHEDULE-001, ATTACHMENT-FLOW-001

## Purpose

Define the taxonomy for **connected runtime assets** managed by Gateway.

Gateway manages more than interactive chat agents. A connected asset may be:

- an interactive agent,
- a background worker,
- a scheduled job,
- an alert listener, or
- a service/tool proxy.

This spec answers a different question than
[GATEWAY-CONNECTIVITY-001](../GATEWAY-CONNECTIVITY-001/spec.md):

- **Asset taxonomy** explains what kind of thing this is and how work flows
  through it.
- **Gateway connectivity** explains whether the current path is safe, live,
  stale, queued, blocked, or expected to reply.

The two specs are complementary and must not be collapsed into one overloaded
status model.

## Goals

- Give the product a stable language for connected assets that are not all
  "agents" in the same sense.
- Make intake, trigger, return, and observability semantics explicit.
- Keep the user mental model simple enough that aX can explain whether an
  asset listens live, launches on send, drains a queue, runs on a schedule, or
  waits for external alerts.
- Provide a canonical mapping from taxonomy fields into the connectivity model
  defined by GATEWAY-CONNECTIVITY-001.
- Support current starter templates and future runtime classes without forcing
  them all into the word `agent`.

## Non-goals

- Replacing the canonical status model from GATEWAY-CONNECTIVITY-001.
- Introducing new v1 primary status chips beyond `Mode`, `Presence`, `Reply`,
  and `Confidence`.
- Forcing every asset into a live-listener model.
- Finalizing the future v2 user-facing `SCHEDULED` or `EVENT` mode values.
- Defining exact transport payloads for alerts or schedules. Those belong in
  source-specific specs.

## Relationship to GATEWAY-CONNECTIVITY-001

This taxonomy spec sits one layer above the connectivity contract.

### Taxonomy tells the user and operator:

- what kind of connected thing this is,
- how work enters it,
- what wakes it up,
- where results go, and
- how much Gateway can observe while it works.

### Connectivity tells the user and operator:

- whether Gateway can safely route work to it right now,
- whether the asset is live, stale, offline, blocked, or queued,
- what kind of outcome to expect, and
- how much operational confidence the sender should have right now.

### Important invariant

**Do not infer asset class from live status.**

Examples:

- A stale live listener is still a live-listener asset.
- An inbox-backed worker with no queued jobs is still an inbox worker.
- A scheduled job between runs is not offline just because it is idle.
- An alert listener with no recent alerts is not disconnected by default.
- A service proxy may be healthy even if it never emits replies.

## Definitions

- **Asset**: anything Gateway can register, supervise, invoke, queue work for,
  or expose through a predictable local contract.
- **Asset taxonomy**: the stable identity and flow semantics of an asset.
- **Connectivity state**: the current health and trust state of the path
  between Gateway, the asset, and aX.
- **Intake model**: how work enters the asset from Gateway.
- **Trigger source**: what kind of external or internal event caused an
  invocation.
- **Return path**: where a user-visible or operator-visible outcome lands.
- **Telemetry shape**: how much runtime activity Gateway can observe.

## Canonical Asset Axes

### `asset_class`

What kind of thing this is.

| Value | Meaning |
| --- | --- |
| `interactive_agent` | Message-oriented runtime that is expected to do work and usually reply |
| `background_worker` | Queue-backed worker that processes jobs and may summarize later |
| `scheduled_job` | Asset that runs because of a timer, cron, or schedule |
| `alert_listener` | Asset woken by external alerts or event sources |
| `service_proxy` | Capability or tool surface that may not reply like a normal agent |

### `intake_model`

How Gateway gets work into the asset.

| Value | Meaning |
| --- | --- |
| `live_listener` | Asset is already listening and can claim work now |
| `launch_on_send` | Gateway launches or invokes the asset when work arrives |
| `polling_mailbox` | Gateway holds work until an approved local agent checks its mailbox |
| `queue_accept` | Gateway can durably accept work for later handling |
| `queue_drain` | Worker process drains already-queued work |
| `scheduled_run` | Gateway invokes the asset because of a schedule |
| `event_triggered` | Gateway invokes the asset because of an external event |
| `manual_only` | Asset only runs when an operator explicitly triggers it |

### `trigger_source`

What started a particular invocation.

| Value | Meaning |
| --- | --- |
| `direct_message` | User or agent sent a normal message |
| `mailbox_poll` | A pass-through agent checked its Gateway mailbox |
| `manual_check` | Operator or local process explicitly checked for work |
| `queued_job` | Work item was pulled from a queue |
| `scheduled_invocation` | Scheduler fired |
| `external_alert` | External system event or alert fired |
| `manual_trigger` | Human or operator manually started it |
| `tool_call` | Another asset invoked it as a capability |

### `return_path`

Where the outcome is expected to go.

| Value | Meaning |
| --- | --- |
| `inline_reply` | Normal reply in the current conversation or thread |
| `manual_reply` | Pass-through agent replies when it next checks in |
| `sender_inbox` | Result lands in the sender's inbox or notification stream |
| `summary_post` | Background result is summarized later |
| `task_update` | Outcome updates a task/job record rather than posting a chat reply |
| `event_log` | Outcome is recorded operationally but not posted as chat output |
| `silent` | No user-visible output is expected unless there is an error |

### `telemetry_shape`

How observable the asset is while it works.

| Value | Meaning |
| --- | --- |
| `rich` | Progress, tool events, and intermediate activity are available |
| `basic` | Pickup and coarse progress are available |
| `heartbeat_only` | Gateway can prove liveness/freshness but not much activity |
| `opaque` | Gateway sees only invocation boundaries or errors |

### Optional `worker_model`

Queue-backed assets may also declare how queued work is later processed.

| Value | Meaning |
| --- | --- |
| `agent_check_in` | An approved local agent checks/polls Gateway for mailbox work |
| `queue_drain` | One or more workers later claim work from the durable queue |

This field is relevant when work is held by Gateway and later claimed by a
separate process, including `queue_accept` and `polling_mailbox`.

## Queue-backed Semantics

Queue-backed assets need two separate concepts:

- `queue_accept`
  - Gateway can durably accept work for the asset.
- `queue_drain`
  - a worker process later drains queued work.

These must not be collapsed into a single `queued` concept.

The connectivity contract already distinguishes:

- agent-level queue capability, and
- invocation-level queued state.

This taxonomy spec must preserve that distinction.

## User-facing Categories

The product should present these simple starter categories in setup and fleet
UX:

- `Live Listener`
- `On-Demand Agent`
- `Pass-through Agent`
- `Inbox Worker`
- `Scheduled Job`
- `Alert Listener`

`Service / Tool Proxy` should exist as an advanced/internal category in v1.

### Category descriptions

#### `Live Listener`

Already listening now. Messages can be picked up immediately.

#### `On-Demand Agent`

Starts or attaches when work arrives. Cold start may apply.

#### `Pass-through Agent`

Mailbox-backed identity for a local agent that checks Gateway when available.
Gateway can accept messages for it, but the UI must not imply it is listening
live.

#### `Inbox Worker`

Queue-backed. Work can be accepted safely even when no live worker is attached.

#### `Scheduled Job`

Runs because of a timer or schedule. It is not expected to behave like a live
listener between runs.

#### `Alert Listener`

Runs because an external event source wakes it up. It is not expected to
receive normal direct messages in the same way as a chat agent.

## Mapping to Gateway Connectivity Fields

The taxonomy does not replace the connectivity model. It maps into it.

| Taxonomy field | Connectivity implication |
| --- | --- |
| `asset_class` | template identity and UX category |
| `intake_model` | hints for `placement`, `activation`, and `mode` |
| `trigger_source` | invocation source and event story |
| `return_path` | maps into `reply_mode` and output expectations |
| `telemetry_shape` | maps into `telemetry_level` and observability guarantees |

### Canonical mapping examples

#### Interactive live listener

```text
asset_class=interactive_agent
intake_model=live_listener
-> placement=hosted or attached
-> activation=persistent or attach_only
-> mode=LIVE
```

#### Interactive on-demand agent

```text
asset_class=interactive_agent
intake_model=launch_on_send
-> placement=hosted or brokered
-> activation=on_demand
-> mode=ON-DEMAND
```

#### Inbox-backed background worker

```text
asset_class=background_worker
intake_model=queue_accept
worker_model=queue_drain
-> placement=mailbox
-> activation=queue_worker
-> mode=INBOX
```

#### Pass-through mailbox agent

```text
asset_class=interactive_agent
intake_model=polling_mailbox
worker_model=agent_check_in
-> placement=mailbox
-> activation=attach_only
-> mode=INBOX
```

#### Scheduled job

```text
asset_class=scheduled_job
intake_model=scheduled_run
-> trigger_source=scheduled_invocation
-> mode remains ON-DEMAND in v1 UI unless a future SCHEDULED mode is added
```

#### Alert listener

```text
asset_class=alert_listener
intake_model=event_triggered
-> trigger_source=external_alert
-> mode remains ON-DEMAND in v1 UI unless a future EVENT mode is added
```

## Asset Descriptor Schema

Each registered asset should have a stable descriptor beside, not inside, its
current status snapshot.

### `AssetDescriptor`

```json
{
  "asset_id": "asset_123",
  "gateway_id": "gw_123",
  "display_name": "Docs Worker",
  "asset_class": "background_worker",
  "intake_model": "queue_accept",
  "worker_model": "queue_drain",
  "trigger_sources": ["queued_job", "manual_trigger"],
  "return_paths": ["summary_post", "task_update"],
  "telemetry_shape": "basic",
  "addressable": true,
  "messageable": true,
  "schedulable": false,
  "externally_triggered": false,
  "tags": ["queue-backed", "summary-later", "repo-bound"],
  "capabilities": ["summarize", "update_task"],
  "constraints": ["requires-repo"]
}
```

### Descriptor rules

- `AssetDescriptor` tells the product what the asset is.
- `AgentStatusSnapshot` tells the product whether Gateway can safely route to
  it right now.
- `InvocationStatusSnapshot` tells the product what is happening to one piece
  of work.

These objects must stay separate even when the UI renders them together.

### Descriptor additions

The following extensibility fields are part of the taxonomy layer:

- `tags`
- `capabilities`
- `constraints`

They explain and filter assets, but they must never replace the canonical
connectivity fields from GATEWAY-CONNECTIVITY-001.

## Template Examples

### Hermes

```yaml
asset_class: interactive_agent
intake_model: live_listener
trigger_source: direct_message
return_path: inline_reply
telemetry_shape: rich
mode: LIVE
reply: REPLY
```

### Codex through Gateway

```yaml
asset_class: interactive_agent
intake_model: launch_on_send
trigger_source: direct_message
return_path: inline_reply
telemetry_shape: basic
mode: ON-DEMAND
reply: REPLY
```

If Codex later keeps a live attached session instead of launching on send, the
taxonomy changes to `intake_model=live_listener` but it remains an
`interactive_agent`.

### Codex pass-through

```yaml
asset_class: interactive_agent
intake_model: polling_mailbox
worker_model: agent_check_in
trigger_source:
  - mailbox_poll
  - manual_check
return_path:
  - manual_reply
  - summary_post
telemetry_shape: basic
mode: INBOX
reply: SUMMARY
```

The user-facing category is `Pass-through Agent`. See
**GATEWAY-PASS-THROUGH-MAILBOX-001** for approval, fingerprinting, and mailbox
count semantics.

### Inbox docs worker

```yaml
asset_class: background_worker
intake_model: queue_accept
worker_model: queue_drain
trigger_source: queued_job
return_path: summary_post
telemetry_shape: basic
mode: INBOX
reply: SUMMARY
```

### Reminder bot

```yaml
asset_class: scheduled_job
intake_model: scheduled_run
trigger_source: scheduled_invocation
return_path: task_update
telemetry_shape: basic
reply: SUMMARY
```

### PagerDuty or Datadog bridge

```yaml
asset_class: alert_listener
intake_model: event_triggered
trigger_source: external_alert
return_path: sender_inbox
telemetry_shape: heartbeat_only
reply: SUMMARY
```

### Tool proxy

```yaml
asset_class: service_proxy
intake_model: manual_only
trigger_source: tool_call
return_path: event_log
telemetry_shape: opaque
reply: SILENT
```

## UI and Onboarding Implications

### Fleet view

Fleet UX should evolve from:

```text
Agent | Mode | Presence | Reply | Confidence
```

to:

```text
Asset | Type | Mode | Presence | Output | Confidence
```

Example:

```text
@hermes-bot      Live Listener   LIVE       WORKING  Reply    HIGH
@ollama-bot      On-Demand Agent ON-DEMAND  IDLE     Reply    MEDIUM
@docs-worker     Inbox Worker    INBOX      IDLE     Summary  HIGH
@reminder-bot    Scheduled Job   ON-DEMAND  IDLE     Task     HIGH
@datadog-bridge  Alert Listener  ON-DEMAND  IDLE     Inbox    HIGH
```

For v1, `Type` carries the richer mental model while `Mode` remains one of:

- `LIVE`
- `ON-DEMAND`
- `INBOX`

### Connect wizard

Setup should lead with asset categories rather than low-level backends.

The first visible cards should be:

- `Live Listener`
- `On-Demand Agent`
- `Inbox Worker`
- `Scheduled Job`
- `Alert Listener`

The template-specific form can then specialize:

- Hermes under `Live Listener`
- Ollama under `On-Demand Agent`
- Inbox docs worker under `Inbox Worker`
- Reminder bot under `Scheduled Job`
- PagerDuty bridge under `Alert Listener`

### Composer expectations

Pre-send UX should combine the taxonomy layer with the connectivity layer.

Example:

```text
@docs-worker
Inbox Worker · INBOX · IDLE · SUMMARY · HIGH
Queue-backed. Work can be accepted now and summarized later.
```

### Custom Bridge

Custom Bridge should become the advanced escape hatch for arbitrary local
assets. In addition to the connectivity contract, setup should ask:

- what class of asset this is,
- how work enters it,
- what triggers it,
- where results go, and
- how much telemetry Gateway should expect.

## Gateway Doctor Requirements by Asset Class

Gateway Doctor must become asset-class aware.

### Live Listener

- identity
- Gateway auth
- runtime launch or attach
- heartbeat
- test claim
- inline reply

### On-Demand Agent

- identity
- launch preflight
- cold-start test
- test claim
- return path validation

### Inbox Worker

- queue writable
- worker config valid
- optional worker attached
- test job queued
- summary viability

### Scheduled Job

- schedule registered
- next run computed
- dry run succeeds
- return path valid

### Alert Listener

- webhook or event source configured
- signing secret valid
- test event accepted
- task or inbox return path valid

### Service Proxy

- capability exposed
- auth boundary valid
- test tool call succeeds
- output path valid

## Acceptance Tests

Minimum acceptance tests for this taxonomy:

- Hermes renders as `Live Listener` and maps to `LIVE + REPLY + rich`.
- Ollama renders as `On-Demand Agent` and maps to `ON-DEMAND + REPLY`.
- Inbox docs worker renders as `Inbox Worker` even with `queue_depth=0`.
- Inbox docs worker with `queue_depth>0` changes presence to `QUEUED` but not
  asset class.
- Scheduled job with `next_run_at` in the future is healthy and not considered
  offline just because it is waiting.
- Scheduled job dry-run failure surfaces as blocked or error according to the
  connectivity contract.
- Alert listener with valid webhook config but no recent alerts is healthy.
- Alert listener failed signature validation surfaces as a security/setup
  problem, not as a missing reply.
- Service proxy can be `SILENT` and still healthy.
- Changing `telemetry_shape` from `rich` to `heartbeat_only` changes
  observability copy, not asset class.

## Roadmap

### v1

- Keep taxonomy as a descriptor layer beside the connectivity model.
- Use `Type` in setup and fleet UX before introducing new primary mode values.
- Keep `Mode` user-facing values limited to `LIVE`, `ON-DEMAND`, and `INBOX`.

### Later

- Add explicit user-facing `SCHEDULED` and `EVENT` modes if those asset classes
  become common enough to deserve their own top-level chips.
- Add source-specific specs for alert/webhook contracts.
- Add richer service/tool proxy and MCP-facing asset classes once Gateway MCP
  mode is further along.

## Key Product Rule

Gateway should make this promise:

> I can connect many kinds of runtime assets, and I will tell you honestly what
> kind of connection this is, how work gets in, what wakes it up, where results
> go, and how observable it is.

That honesty is the point of the taxonomy layer. It lets aX stay trustworthy
even when not every asset is a live chat agent.
