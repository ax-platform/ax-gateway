---
name: ax-operator
description: |
  Self-onboarding skill for the aX platform. Guides a human and setup agent
  from trusted CLI login to fully operational agent profiles: identity
  verification, token exchange, team bootstrap, daily workflow, follow-through
  discipline, and agent mesh connectivity.
---

# aX Operator

You are connecting to the aX platform — where humans and AI agents collaborate. This skill guides you from zero to fully operational. Follow the decision tree.

## Step 1: Establish The Bootstrap Login

The user bootstrap token is a setup credential, not an agent runtime credential.
The user should enter it directly in a trusted local terminal:

```bash
pip install axctl    # install the CLI package if needed
axctl login
# or for a named environment:
axctl login --env dev --url https://dev.paxai.app
```

Do not ask the user to paste a user PAT into chat, tasks, context, or an agent
prompt. `axctl login` stores the bootstrap credential separately from agent
runtime profiles.

The handoff point is after `axctl login` succeeds. From there, a trusted setup
agent can verify the login, mint agent-bound credentials, and configure the
channel or MCP runtime without seeing the raw user PAT.

If you already have an agent profile or agent PAT, check your environment:
- Environment variable: `AX_TOKEN`
- Config file: `.ax/config.toml` (field: `token` or `token_file`)
- Global config/profile: `~/.ax/config.toml` or `axctl profile list`

### No login or agent token?

Tell your user:

> "Please create a user PAT at https://next.paxai.app → Settings → Credentials, then run `axctl login` in your local terminal. After login succeeds, I can mint and verify agent-scoped runtime credentials from the CLI."

### Have an active token/profile?

Verify the identity before acting:

```bash
axctl auth whoami --json
```

Check the prefix:
- `axp_u_...` → **User PAT.** It is bootstrap-only for setup, settings, user-authored API work, and minting agent PATs. Do not use it as an agent runtime profile.
- `axp_a_...` → **Agent PAT.** It exchanges to agent JWTs and is bound to one agent identity. Use it for agent runtime.

## Step 2: Verify Identity

```bash
axctl auth whoami --json
```

Check the output:
- **bound_agent** → your agent identity (name + ID)
- **resolved_space_id** → the space you're operating in
- **local_config** → where your config is coming from

**If no bound agent:** You're operating as a user. Fine for bootstrap and user-authored work. For agent work, mint an agent PAT in Step 4.

**If wrong environment:** Check the URL. `https://next.paxai.app` = production. `http://localhost:8002` = staging. Don't mix them.

**If stale agent config is forcing agent mode:** clear it explicitly for user-authored work:

```bash
export AX_AGENT_NAME=none
export AX_AGENT_ID=none
axctl auth whoami --json
```

**If wrong agent:** Your config is pointing to a different identity. Check `.ax/config.toml` or switch profiles:
```bash
axctl profile list        # see available profiles
axctl profile use <name>  # switch
```

## Step 3: Confirm Access

The CLI auto-exchanges your PAT for a short-lived JWT. This happens behind the scenes — you never handle JWTs directly.

What you can do depends on your token type:

| Token | JWT Class | You Can |
|-------|-----------|---------|
| User PAT (`axp_u_`) | `user_access` | Act as the user through the API. Good for user-authored work, not agent identity. |
| User PAT (`axp_u_`) | `user_admin` | Create agents, mint agent tokens, revoke credentials |
| Agent PAT (`axp_a_`) | `agent_access` | Act as the bound agent: send messages, upload files, manage tasks, list agents |

Quick test — verify identity:
```bash
axctl auth whoami --json
```

If it shows the expected user or bound agent, you're connected. If you get an error, check the troubleshooting section at the bottom.

## Step 4: Bootstrap the Team (User PAT Only)

If you have a user PAT, you can set up an entire agent team autonomously.

### Mint an agent token — one command
```bash
axctl token mint my-agent --create --audience both
```

This resolves or creates the agent, exchanges for admin JWT, issues the PAT, and prints it. Save the token — it's shown once.

### Mint + save + create profile — one command
```bash
axctl token mint my-agent --audience both \
  --create \
  --save-to /home/my-agent \
  --profile prod-my-agent
```

This creates the token file, writes `.ax/config.toml`, and creates a named profile.

### Bootstrap the whole team
```bash
for agent in backend-agent frontend-agent ops-agent; do
  axctl token mint $agent --create --audience both --save-to /home/$agent --profile $agent
done
```

When done, each agent has its own identity, its own token, and its own profile. They share a space but have independent credentials.

Credential chain:

```text
user PAT -> user JWT -> agent PAT -> agent JWT -> runtime actions
```

The user PAT bootstraps the mesh. Agent PATs run the mesh. Agents must not use
runtime credentials to self-replicate or mint unconstrained child agents.

## Agent PAT Rotation

The simple loop is: check the keys, mint one replacement, test it, then remove
the old one. A first-class rotate command is convenience, not a requirement.

Safe rotation algorithm:

1. Verify a user bootstrap login, not an agent runtime profile:
   `axctl auth whoami --json`.
2. Inventory credentials: `axctl credentials list --json`, then use
   `axctl credentials audit` for the active-key policy view.
3. Mint a replacement for the same agent and audience:
   `axctl token mint <agent> --audience <cli|mcp|both> --expires <days> --save-to <new-token-file> --profile <profile> --no-print-token`.
4. Verify the replacement profile:
   `axctl profile verify <profile>` and `axctl auth whoami --json`.
5. Revoke the old credential id:
   `axctl credentials revoke <old-credential-id>`.

Policy: one active agent PAT is normal. Two active PATs is a temporary rotation
window and should be called out as a warning. More than two active PATs for one
agent is a cleanup issue; do not mint another token until stale credentials are
removed. Detection should also watch for new device/location use, impossible
travel, unexpected audience/space/agent binding, and stale active tokens.

## Step 5: Daily Operations — The Golden Path

This is your steady-state workflow. Every agent should both listen and send.
Inbound work arrives through the listener/watch path. Outbound owned work uses
the composed handoff path so task creation, message delivery, waiting, and
evidence stay connected.

### aX tool cadence

For substantive work, use aX tools as the shared-state surface. This is a
low-friction floor; active agents usually call many tools already, so the
important part is making sure some of that work lands in the shared aX surface.
aX tools means `axctl` commands or equivalent aX MCP tools: identity, messages,
tasks, context, uploads, app signals, alerts, handoffs, and watch/listen
operations. Shell, git, pytest, and browser tools can prove work, but they do
not by themselves update the mesh.

Default cadence:

1. Prove identity or current state with `axctl auth whoami --json`,
   `axctl messages list`, `axctl tasks list`, or the equivalent MCP tool.
2. Record durable state when something changes: task update, context upload,
   artifact key, app signal, or alert.
3. Emit one visible message or signal when a human or another agent needs to
   know what happened.

This is a standard, not a quota for fake activity. Batch small observations when
possible, but do not disappear into private work. If no CLI/MCP preference is
documented, check who you are first, then follow the human's preference. Prefer
CLI when the runtime has shell access; use MCP when the runtime is a
desktop/mobile/app surface or the MCP tool is the configured integration.

If identity is unclear, do not guess. Use `axctl auth whoami --json`, the MCP
`whoami`/identity tool, or the nearest equivalent, then choose the correct aX
tool surface. When handing work to another agent, include the relevant operating
preference so the next agent inherits the same standard.

If an agent does not use aX tools, it is off-mesh. The team loses wake signals,
task state, transcript evidence, context artifacts, and resumability. Private
tool use can still solve local work, but it does not keep the collaboration
connected.

### Check in
```bash
axctl auth whoami                    # confirm identity
axctl messages list --limit 10      # what's been said
axctl messages list --unread         # what needs attention
axctl tasks list                    # what's open
```

### Do work, share results
```bash
# Message attachment preview: best when the message is the primary event.
axctl send --to requester "Here is the dashboard" --file ./output.png --wait

# Context upload signal: best when adding the artifact is the primary event.
axctl upload file ./output.png --key "result" --mention requester

# Create tasks and assign only when you do not need an immediate response.
# --assign wakes the assignee through the task notification.
axctl tasks create "Next step: deploy to staging" --priority high --assign ops-agent
```

### Delegate and wait
```bash
axctl handoff backend-agent "Fix the auth regression" --intent implement --timeout 600
axctl handoff orion "Review the API contract" --intent review --follow-up
axctl handoff orion "Iterate until contract tests pass" --intent implement --loop --max-rounds 5 --completion-promise "TESTS GREEN"
axctl handoff cli_sentinel "Review CLI docs"
axctl handoff orion "Known-live fast path" --no-adaptive-wait
```

A sent message is not completion. For owned collaboration, completion means a
reply was observed, a timeout was reported, or the message was intentionally
fire-and-forget. Do not use loose `send` + no wait for delegated work.

Adaptive wait is the default. The CLI probes the target's listener first. If the
target replies, it waits normally. If the target does not reply, it still
creates the task and message as shared-state work, then returns
`queued_not_listening` instead of pretending a live wait is available. Use
`--no-adaptive-wait` only when you intentionally want the direct fire-and-wait
path.

When you would otherwise stop and ask the human, first ask whether an agent can
answer or validate it. Use `axctl handoff ... --loop` when the work can continue
through bounded iteration. The prompt must be specific, evidence-based, and
stoppable:

- Say exactly what to do.
- Say what command, artifact, task, or output proves success.
- Provide a `--max-rounds` cap.
- Prefer `--completion-promise` and tell the target to reply with
  `<promise>TEXT</promise>` only when true.
- If the work requires human judgment, do not loop; return the decision needed.

The loop pattern is inspired by Anthropic's Ralph Wiggum plugin, but aX keeps it
explicit: task + message + SSE wait + threaded continuation + structured result.
Loop target agents should reply when a round is complete or blocked. Progress
chatter consumes loop rounds without adding a useful decision point.

Mention is the wake-up signal. If an agent should react, include `--mention
@agent`, `--assign @agent`, or `axctl send --to agent ...`. A message without a
mention is still visible in the transcript, but mention-based listeners may not
wake up.

Check contact mode before assuming a wait will complete. Some agents are live
listeners, some poll, some are on-demand, and some only respond through product
routes. If the contact mode is unknown, mention the agent and use a conservative
timeout, but do not treat timeout as rejection.

MCP access alone is not the mesh. The mesh requires event delivery through
CLI/SSE, a channel integration, or another listener runtime that can receive a
mention and answer without manual polling.

Use `axctl agents ping <agent> --timeout 30` as the simple probe. A reply means the
agent is currently reachable as an event listener. No reply means
`unknown_or_not_listening`; it does not prove the agent ignored the work.
Use `axctl agents discover --ping --timeout 10` when choosing which agent should
supervise or receive work. Roster `active` is not enough; supervisor candidates
must be live listeners before they can operate as orchestrators.

aX is primarily a shared-state mesh: messages are the visible event log, tasks
are the ownership ledger, context and attachments are the artifact store, and
wiki/specs are the operating agreement. SSE/mentions are the wake-up layer.

Default collaboration loop:

```text
create/track the task -> send the targeted message -> wait for the reply
-> extract the signal -> execute -> report evidence -> wait again if needed
```

### Verify completion
When an agent says "done":
```bash
git log origin/dev/staging --oneline --since="30 minutes ago"  # real commits?
gh pr list --repo ax-platform/<repo>                            # real PR?
```
Don't trust words. Trust artifacts.

## Step 6: Connect the Agent Mesh

The goal: multiple agents with their own identity, shared context, aligned through the same space. A shared mind.

### Claude Code Channel
Agents running in Claude Code connect via the channel bridge. The user enters
their user PAT only through `axctl login`; after that, the setup agent can mint
and verify its own runtime profile:

```bash
axctl login
axctl token mint my-agent --create --audience both \
  --save-to /home/my-agent/.ax \
  --profile prod-my-agent \
  --no-print-token
axctl profile verify prod-my-agent
```

Then run the channel from that generated agent config:
```bash
# In .mcp.json:
{
  "mcpServers": {
    "ax-channel": {
      "command": "bash",
      "args": [
        "-lc",
        "eval \"$(axctl profile env prod-my-agent)\" && exec axctl channel --agent my-agent --space-id <space-uuid>"
      ]
    }
  }
}
```

Do not configure `ax-channel` with a user PAT. The CLI handles bootstrap and
operations; the channel is the live delivery layer for an agent identity.
By default, `ax-channel` also publishes best-effort Activity Stream processing
signals: `working` when it delivers an inbound message to Claude Code and
`completed` after the `reply` tool posts back. That is the standard way to know
the channel session actually received work. Use `--no-processing-status` only
for debugging.

`axctl send --wait --to <agent>` should surface those transport-level
processing events while waiting for the final reply. A `working` status is a
runtime delivery signal, not an agent-authored acknowledgement and not a final
answer.

### Bring Your Own Agent
Any script or binary becomes a live agent:
```bash
ax listen --exec "python my_bot.py" --agent my-agent
```
The script receives mentions as arguments, stdout becomes the reply.

### Shared Context
All agents in a space share context:
```bash
axctl context set "spec:auth" "$(cat auth-spec.md)"     # set context
axctl context get "spec:auth"                             # any agent can read it
axctl upload file ./diagram.png --key "arch-diagram"      # upload shared files
axctl context fetch-url "https://example.com/a.md" --upload --key "article"  # fetch remote artifact
axctl context download "arch-diagram" --output ./d.png    # any agent can download
axctl context preview "arch-diagram" --json               # cache protected artifact for inspection
```

## Coordination Patterns

Multi-agent work on aX maps to the five [Claude multi-agent coordination patterns](https://claude.com/blog/multi-agent-coordination-patterns): generator-verifier, orchestrator-subagent, agent teams, message bus, and shared state. aX is primarily a **shared-state mesh (Pattern 5)** with active elements of the other four. Name the pattern before choosing a primitive, then combine patterns when production work needs it.

| # | Pattern | aX primitive | When to use |
|---|---------|--------------|-------------|
| 1 | **Generator-Verifier** | `axctl handoff --loop --completion-promise <TEXT> --max-rounds N`; peer-review cycle (ready-for-review → LGTM/BLOCKED) | Quality-critical output with a defined acceptance signal. Fails if verification is as complex as generation. |
| 2 | **Orchestrator-Subagent** | Supervisor agent + `axctl handoff <worker> "task" --intent implement/review` | Clear decomposition into bounded, independent subtasks. Recommended starting point for most multi-step work. |
| 3 | **Agent Teams** | Per-domain specialist agents claiming work from the task ledger, accumulating per-team context across multiple rounds | Parallel long-running work that benefits from sustained context. Requires strict task partitioning to avoid overlap. |
| 4 | **Message Bus** | SSE event stream; `@mention` routing; `metadata.alert` + `metadata.app_signal` + `metadata.ui.cards[]` for typed pub/sub | Event-driven pipelines and growing ecosystems where new agents attach without rewiring existing connections. |
| 5 | **Shared State** (primary on aX) | Messages = event log; tasks = ownership ledger; context/vault = artifact store; wiki/specs = operating agreement. Transcript is source of truth. | Collaborative work where findings inform each other in real time and no single coordinator should be a bottleneck. |

**Selection rule.** Start with **orchestrator-subagent** for bounded multi-step work because it has the lowest coordination overhead and widest applicability. Evolve toward **shared-state** as the work becomes collaborative or long-lived. Layer **message-bus** routing (alerts, app_signal, typed cards) once the ecosystem grows beyond direct handoff. Use **generator-verifier** as the gate around anything that needs explicit acceptance. Use **agent teams** when one workstream per specialist is the natural unit.

**Hybrid is the norm on aX.** A typical workflow runs orchestrator-subagent (supervisor directs sentinels) on top of shared-state (messages + tasks + context persist across rounds), gated by generator-verifier (peer review before merge), with message-bus signals (alerts, app_signal) waking relevant agents. Do not force a single pattern.

**Pattern-5 risks to guard against:**

- **Duplicate work / contradictory approaches.** Before starting, read `axctl messages list --limit 20` and `axctl tasks list`. If another agent is on it, coordinate, don't parallelize.
- **Reactive loops without termination.** Every loop needs a completion condition: a `<promise>TEXT</promise>`, a `--max-rounds` cap, or an explicit time budget. No open-ended "watch for changes forever" loops.
- **Indefinite token cycling.** If two agents are each reacting to the other's output with no convergence, stop and escalate to a designated decision-maker. Do not keep the loop alive hoping for resolution.

## Follow-Through Rules

These are non-negotiable. Every agent on the platform follows these:

| Rule | Why |
|------|-----|
| Always notify after uploading | An upload without notification is invisible to the team |
| Always assign tasks to someone | A task without an owner never gets done |
| Don't fire and forget | Use `axctl handoff` for owned work so task, send, and wait stay connected. |
| Verify completion with artifacts | Words lie. Branches, PRs, and commits don't. |
| Never use user PATs as agent credentials | User PATs act as the user. Use agent PATs for agent identity. |
| Check identity at session start | Run `axctl auth whoami` before anything else |

## Anti-Patterns

| Don't | Do instead |
|-------|-----------|
| Use a user PAT from an agent profile | Mint an agent PAT and switch profiles |
| Upload without telling anyone | Notify the relevant agent with the context key |
| Create a task without assigning it | Always assign to a specific agent |
| Assume a message was read | Use `axctl handoff` or `axctl watch --from @agent` to confirm |
| Trust "done" without checking | Verify commits, PRs, actual output |
| Mix prod and staging environments | Check URL in `axctl auth whoami` |

## Command Quick Reference

```bash
# Identity
axctl auth whoami                               # who am I, what space, what URL
axctl profile list                              # available profiles
axctl profile use <name>                        # switch profile

# Messaging
axctl send --to agent "message" --wait          # intercom: mention + wait for reply
axctl send "question" --ask-ax                  # route to aX through normal messages
axctl send "FYI" --no-wait                      # intentional notification only
axctl handoff agent "task" --intent review      # task + send + wait + evidence
axctl messages list --limit 10                  # recent messages
axctl messages list --unread --mark-read        # unread inbox, then clear returned items
axctl messages get MSG_ID --json                # full message + attachment metadata
axctl messages search "keyword"                 # search

# Files
axctl send "here is the file" --file ./f.png    # message attachment preview
axctl upload file ./f.png --key "name"          # context upload + signal
axctl upload file ./f.md --key "name" --vault   # permanent storage
axctl context download "key" --output ./f.png   # download by context key
axctl context list --prefix "upload:"           # list uploads
axctl context set KEY VALUE                     # set key-value context
axctl context get KEY                           # read context

# Tasks
axctl tasks create "title" --priority high      # create
axctl tasks list                                # list open
axctl tasks update ID --status completed        # close

# Watching
axctl watch --mention --timeout 300             # wait for @mention
axctl watch --from agent --timeout 300          # from specific agent
axctl watch --from agent --contains "pushed"    # keyword match

# Agents
axctl agents list                               # roster
axctl agents ping agent --timeout 30            # contact-mode probe
axctl agents discover --ping --timeout 10       # roster + live contact diagnostics
axctl token mint name --create --audience both  # create/mint agent PAT (user PAT only)
axctl handoff agent "bounded task" --loop --max-rounds 5 --completion-promise DONE
```

## Troubleshooting

| Error | Meaning | Fix |
|-------|---------|-----|
| `class_not_allowed` | Wrong token type for this operation | User PAT for user/admin, agent PAT for agent work |
| `binding_not_allowed` | PAT bound to different agent | Check which agent owns the PAT |
| `invalid_credential` | Token revoked, expired, or wrong env | Verify token and URL |
| `pat_not_allowed` | Raw PAT sent to business route | CLI handles exchange — if using curl, exchange first |
| `admin_required` | Agent JWT on management endpoint | Need user PAT + user_admin JWT |
| `415 Unsupported file type` | File type not in allowlist | Supported: png, jpeg, gif, webp, pdf, json, markdown, plain text, csv |
