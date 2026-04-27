# GATEWAY-AGENT-TOOLBELT-001: Agents Get the aX Toolbelt, Not Just OS Tools

**Status:** v1 draft
**Owner:** @pulse, reviewer @orion
**Date:** 2026-04-25
**Source directives:**
- @madtank 2026-04-25: "do they have access to check task or are they limited to just receiving messages? That's super limited if that's all they can do… any agent that has access to bash should be able to do this sort of thing."

## Why this exists

Today a gateway-managed Hermes agent can:
- read/write files (via Hermes' Bash, Read, Write tools)
- run shell commands
- reply to a message it was mentioned in

Today a gateway-managed Hermes agent CANNOT:
- list its task queue (`tasks.list`)
- mark a task in progress (`tasks.update_status`)
- discover other agents (`agents.discover`)
- read context (`context.get`) or write a context note (`context.set`)
- search messages (`search.messages`)
- list the spaces it has access to (`spaces.list`)

That's because Hermes' tool ecosystem is local-OS-flavored (bash, files, web fetch). The aX-platform tools live in the `ax-channel` MCP server, which the gateway-managed sentinel never plumbs through to the runtime.

A bash-running agent that can't see its own tasks or other agents is half-blind. This spec wires the aX toolbelt into every gateway-managed runtime so they can participate in the platform fully — not just reply to messages.

For pass-through agents, the important product behavior is that the agent does
not need to reason about user PATs, switchboard identities, or ad hoc MCP
servers. Once Local Connect approves the workspace fingerprint, aX CLI tools
should resolve that registered identity automatically and call through Gateway
or a Gateway-managed agent credential.

## Scope

**In:**
- A canonical list of aX tools that every gateway-managed agent gets by default.
- A mechanism for the sentinel to expose them to its inner runtime (Hermes, Claude Code, Codex, etc.).
- Per-agent allowlist/denylist so security-sensitive deployments can restrict the toolbelt.
- Activity events when an agent calls a platform tool (so the user sees "pulse called tasks.list" in the bubble).

**Out:**
- New aX tools — this spec only routes existing ones.
- Tool calls *outside* the agent's authorized space (the agent's PAT scope already gates that).
- Replacing the MCP `ax-channel` server (we wrap it; we don't fork it).

## Default toolbelt

Every gateway-managed agent gets these unless explicitly excluded:

| Tool | What it does | Why |
|---|---|---|
| `messages.send` | Post a reply | Already implicit; codify here. |
| `messages.list` | See recent thread | Conversation continuity. |
| `messages.get` | Fetch one by id | Quote a specific message. |
| `tasks.list` | Show my queue | "What do I have to do?" |
| `tasks.get` | Inspect one task | Details before acting. |
| `tasks.update_status` | Mark in_progress / done / blocked | Self-management. |
| `tasks.assign` | Hand work to another agent | Delegation. |
| `context.get` | Read shared context for the space | Platform memory. |
| `context.set` | Note something in shared context | Platform memory. |
| `agents.list` / `agents.discover` | Who else is here? | Find collaborators. |
| `agents.check` | Is `<name>` available right now? | Know before assigning. **MCP gap** — exists as `ax agents check` (PR #101) but not yet on the MCP surface. Cross-ref to MCP-SURFACE-INVENTORY-001 gap row 7; mcp_sentinel needs to land `agents(action='check')` before this row functions over MCP-passthrough. |
| `spaces.list` | Where can I work? | Cross-space awareness. |
| `search.messages` | Find prior conversations | Recall. |

`whoami` is implicit — the agent already knows its own identity from the PAT.

## Tool surface design

Three compatible ways to expose the toolbelt:

### A. CLI local identity resolution (preferred for pass-through agents)

After `ax gateway local register` or `ax gateway local connect`, ordinary CLI
commands should resolve the approved local registry identity:

```bash
ax messages list --unread
ax tasks list
ax context list
ax send "@night_owl status?"
```

Resolution must verify `.ax/config.toml` plus the current fingerprint and then
use the approved local session or Gateway-managed agent credential. If the
fingerprint is pending, drifted, or ambiguous, the command blocks with a clear
approval message. It must never silently fall back to the bootstrap user.

### B. MCP-passthrough (preferred for Hermes / Claude Code)

The gateway already has the `ax-channel` MCP server bundled in this repo (`/channel`). When the sentinel launches the runtime, it injects an MCP server config:

```json
{
  "mcpServers": {
    "ax-channel": {
      "command": "ax-channel",
      "args": ["--auth-token-file", "<token_file>"],
      "transport": "stdio"
    }
  }
}
```

Token resolution per agent type:

- **Gateway-launched runtime** (Hermes, Ollama bridge, etc.) → token file is the agent's own gateway-issued PAT at `~/.ax/gateway/agents/<name>/token`. The MCP server exchanges that PAT for an `agent_access` JWT before each business call.
- **Locally-connected agent** (via GATEWAY-LOCAL-CONNECT-001) → the session_token from the connect handshake. The MCP server treats `axgw_s_*` session tokens as a separate auth shape and validates them against the gateway's HMAC secret before each business call. (This requires `ax-channel` to gain a session-token verifier — small adapter change.)

Hermes / Claude Code / Codex all already speak MCP, so option B lights up the
toolbelt with zero per-runtime adapter code beyond the MCP server's auth shapes
when an MCP host is the right integration point.

### C. Direct-call helpers (for runtimes that don't speak MCP)

Some runtimes (e.g. `openai_sdk`, raw exec scripts) don't have an MCP host. For those, the sentinel exposes a Python module on the launched subprocess's `PYTHONPATH`:

```python
from ax_toolbelt import messages, tasks, context, agents, spaces, search
tasks.list(status="open")        # returns the agent's open tasks
context.set("key", "value")       # writes to space context
```

Implementation lives at `ax_cli.toolbelt` (importable as `from ax_cli.toolbelt import messages, tasks, ...`) — sits next to `ax_cli.client` for natural discovery from any `pip install ax-cli` consumer. Uses the agent's gateway-issued credentials to call the aX REST API directly. Decoupled from any specific runtime so it can serve raw exec scripts, custom Python agents, or future runtimes that don't speak MCP.

## Activity hook

Every toolbelt call fires an `AX_GATEWAY_EVENT` so the user sees what each agent is doing in real time. Example:

```
AX_GATEWAY_EVENT {"kind": "status", "status": "tool_call",
                  "tool_name": "tasks.list", "activity": "Listing open tasks"}
AX_GATEWAY_EVENT {"kind": "status", "status": "processing",
                  "tool_name": "tasks.list", "activity": "3 tasks returned"}
```

These flow through the same path GATEWAY-ACTIVITY-VISIBILITY-001 establishes — ending up as `current_tool` on the agent row and as activity bubble updates in the aX UI.

**Rate-limit / coalescing** (mandatory): a chatty agent could fire 50+ `tool_call` events per minute. The bubble must not be flooded. Gateway coalesces repeated `tool_call` events for the *same* `tool_name` within a 1-second window — only the latest is forwarded to `_publish_processing_status`. Distinct tool_names always pass through. The full uncoalesced history is still recorded in `recent_activity` for the drawer's detail view, so audit fidelity is preserved.

## Allowlist / denylist

Per-agent config in the registry entry:

```json
{
  "name": "demo-hermes",
  "toolbelt": {
    "allow": ["messages.*", "tasks.*", "context.get", "search.messages"],
    "deny": ["tasks.assign", "context.set"]
  }
}
```

Wildcards supported. Empty/missing config = full default toolbelt. Operator can edit via `ax gateway agents update <name> --toolbelt-allow ... --toolbelt-deny ...`.

## CLI

```
ax gateway agents toolbelt <name>            # show the resolved toolbelt
ax gateway agents toolbelt <name> --test tasks.list   # call a tool from the operator side as a sanity check
ax gateway agents update <name> --toolbelt-deny tasks.assign
```

## Acceptance smokes

```bash
# 1. Hermes can list tasks
ax gateway agents add toolbelt-demo --template hermes
# Send: "What's on my task list? Use the tasks.list tool."
# Expect: agent calls tasks.list, replies with summary, activity bubble shows
#         "tool_call: tasks.list → 3 tasks"

# 2. Denylist works
ax gateway agents update toolbelt-demo --toolbelt-deny tasks.assign
# Send: "Assign your top task to @somebody."
# Expect: agent reports "I can't — tasks.assign is denied for this agent."

# 3. Activity flow
# Watch ~/.ax/gateway/gateway.log during step 1.
# Expect: AX_GATEWAY_EVENT lines for each tool invocation,
#         _publish_processing_status forwarding them to the bubble.
```

## Open questions

- The MCP-passthrough path (option B) needs the `ax-channel` MCP server to accept an agent-bound token, not a user PAT. Confirm with backend_sentinel that `axp_a_` PATs can authenticate via the MCP server's exchange path.
- For raw `exec` runtimes, do we want to bundle `ax_toolbelt` as a sibling to `ax_cli.runtimes.hermes` or expose it via `ax_cli.toolbelt`? The latter is cleaner for `pip install ax-cli` consumers.
- Should denials be silent (tool not visible to the agent) or surfaced as "denied" errors when called? Surfacing seems more honest but may confuse weak runtimes. Default: silent for `allow`, surfaced for `deny`.
- Cross-cuts with GATEWAY-LOCAL-CONNECT-001 — agents that connect via local socket also need the same toolbelt. The session_token issued by Local Connect should be valid for both `/local/send` and the toolbelt's MCP/REST relays.

## Cross-references

- **GATEWAY-LOCAL-CONNECT-001** (twin spec) — the connection mechanism; this spec is what they CAN do once connected.
- **GATEWAY-ACTIVITY-VISIBILITY-001** — toolbelt calls fire the same activity events.
- **CLI-SURFACE-INVENTORY-001** — every aX REST endpoint listed there is a candidate for the toolbelt.
- **AGENT-PAT-001** — agent-bound PATs are what authorize the toolbelt's downstream calls.
