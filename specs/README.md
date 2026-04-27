# aX CLI Specs

These specs define the contracts behind `axctl`. Runtime behavior should be
implemented against the specs first, then exposed through CLI commands and
agent skills.

## Credential and Identity

- [AXCTL-BOOTSTRAP-001: Bootstrap and Secure Storage](AXCTL-BOOTSTRAP-001/spec.md)
- [DEVICE-TRUST-001: Device Trust and Approval](DEVICE-TRUST-001/spec.md)
- [AGENT-PAT-001: Agent PAT Minting and JWT Exchange](AGENT-PAT-001/spec.md)
- [IDENTIFIER-DISPLAY-001: Human-Readable Identifier Display](IDENTIFIER-DISPLAY-001/spec.md)
- [RUNTIME-CONFIG-001: Shared Agent Runtime Configuration](RUNTIME-CONFIG-001/spec.md)
- [GATEWAY-IDENTITY-SPACE-001: Gateway Identity, Space Binding, and Visibility](GATEWAY-IDENTITY-SPACE-001/spec.md)

## Workflow and Delivery

- [CONTRACT-QA-001: API-First Regression Harness](CONTRACT-QA-001/spec.md)
- [CLI-WORKFLOW-001: Smart Workflow Flags on Existing Commands](CLI-WORKFLOW-001/spec.md)
- [GATEWAY-CONNECTIVITY-001: Gateway Connectivity, Signal Model, and Sender Confidence](GATEWAY-CONNECTIVITY-001/spec.md)
- [GATEWAY-AGENT-REGISTRY-001: Agent Registry, Local Binding, and Self-Profile](GATEWAY-AGENT-REGISTRY-001/spec.md)
- [GATEWAY-ASSET-TAXONOMY-001: Gateway Asset Taxonomy and Flow Semantics](GATEWAY-ASSET-TAXONOMY-001/spec.md)
- [GATEWAY-PASS-THROUGH-MAILBOX-001: Pass-through Mailbox Agents](GATEWAY-PASS-THROUGH-MAILBOX-001/spec.md)
- [CONNECTED-ASSET-GOVERNANCE-001: Registry, Provenance, Capabilities, Grants, Secrets, and Approval](CONNECTED-ASSET-GOVERNANCE-001/spec.md)
- [AGENT-CONTACT-001: Agent Contact Modes](AGENT-CONTACT-001/spec.md)
- [AGENT-MESH-PATTERNS-001: Shared-State Agent Mesh](AGENT-MESH-PATTERNS-001/spec.md)
- [MESH-SPAWN-001: User-Bootstrapped Agent Credential Spawning](MESH-SPAWN-001/spec.md)
- [LISTENER-001: Mention and Reply Delivery for CLI Listeners](LISTENER-001/spec.md)
- [ATTACHMENT-FLOW-001: Attachment Flow](ATTACHMENT-FLOW-001/spec.md)
- [AX-SCHEDULE-001: Agent Scheduling](AX-SCHEDULE-001/spec.md)

Canonical operator QA and release practice lives in
[../docs/operator-qa-runbook.md](../docs/operator-qa-runbook.md). The required
sequence is `auth doctor`, then `qa preflight`, then `qa matrix`, then MCP Jam,
widget, Playwright, or release work.
