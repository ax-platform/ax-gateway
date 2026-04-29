# GATEWAY-SERVICE-ACCOUNTS-001: Gateway Service Accounts

**Status:** v1 draft
**Owner:** @pulse
**Date:** 2026-04-26
**Related:** GATEWAY-PASS-THROUGH-MAILBOX-001, GATEWAY-ACTIVITY-VISIBILITY-001, GATEWAY-IDENTITY-SPACE-001

## Why This Exists

Gateway needs named non-human senders for tests, alerts, reminders, logs,
security events, and future automations. These identities are useful, but they
must not look like live agents that can think, use tools, or reply.

The product rule:

> A service account is a named message source, not an agent runtime.

## Template Contract

```yaml
template_id: service_account
runtime_type: inbox
asset_class: service_account
intake_model: notification_source
worker_model: no_runtime
trigger_sources:
  - manual_message
  - automation
  - scheduled_job
return_paths:
  - outbound_message
telemetry_shape: basic
reply_mode: silent
```

Service accounts may send messages through Gateway, but they do not claim work
or produce model/tool activity. A message sent from a service account should
show a clear source such as `@notifications`, `@switchboard-<space>`, or
`@security-alerts`.

## Default Switchboard Accounts

Gateway may create a default switchboard/service account when it needs a sender
for operator tests or routing notifications. These defaults should be:

- clearly marked as `SYSTEM`;
- hidden from the main roster unless system agents are shown;
- scoped to a base URL and space;
- safe to remove or rotate when no messages depend on them;
- reconciled so repeated space changes do not create duplicate accounts.

Open question for backend alignment: whether the platform should own exactly one
default service account per Gateway, one per space, or one per notification
class. The UI should tolerate all three until the backend contract is final.

## User-Created Service Accounts

The Gateway dashboard should offer **Service Account** as a Connect-agent
choice. Creating one should ask for:

- name;
- space;
- optional description/purpose;
- optional source kind such as `notifications`, `reminders`, `logs`, or
  `security`.

Future follow-up: allow an operator to create service accounts from the drawer
or settings panel, then select one as the sender for manual messages, scheduled
messages, webhook-style automations, or log/security integrations.

## Message Composer Contract

When a user sends a manual message from an agent drawer:

- the UI must show `From @service-account To @target-agent`;
- the sender must be the selected service account, not the target agent and not
  the bootstrap user;
- if no custom service account exists, Gateway may use the default switchboard
  service account for the agent's current space;
- the activity row should say `Message sent` or `Manual message sent` and show
  the actual sender.

This avoids the confusing case where a test message appears to come from the
same agent being tested.

## Acceptance Tests

- `ax gateway templates --json` includes `service_account`.
- The dashboard Connect menu shows `Hermes`, `Ollama`, `Echo`, and
  `Service Account`.
- Creating a service account produces a non-live row with no green live dot.
- Sending a drawer message records the service-account sender.
- System switchboard rows stay hidden by default and do not duplicate for a
  single Gateway/space/source combination.
