---
name: ax-cli
description: Use when operating axctl or the ax-cli repo for aX identity verification, profile setup, user PAT to agent PAT credential flows, task/message/context workflows, MCP or channel runtime setup, and diagnosing CLI/profile confusion.
---

# ax-cli

Use `axctl` as the aX operating surface, with identity as the first
constraint. The CLI is useful because it makes shared work visible: messages
are the event log, tasks are the ownership ledger, context is the artifact
store, and profiles make authorship explicit.

This skill is intended to be shareable. Keep examples generic. Do not embed
real tokens, tenant IDs, private URLs, production hostnames, local workstation
paths, private agent names, customer names, or personal names.

## Core Rule

User credentials bootstrap the mesh. Agent credentials run the mesh.

```text
user PAT -> user JWT -> agent PAT -> agent JWT -> runtime actions
```

A user PAT may initialize auth, inspect user-owned state, and mint agent-bound
PATs. Runtime agent work must use an agent-bound profile or token. Do not use a
user PAT to impersonate an agent.

## First Checks

Before reads, sends, uploads, assignments, or credential changes:

```bash
axctl profile list
AX_SPACE_ID=<space-id> axctl auth whoami --json
```

Interpret the identity before acting:

- `bound_agent` present means the profile is agent-bound.
- `bound_agent: null` means the profile is a user session.
- `resolved_space_id` is the space that commands will target unless overridden.
- `resolved_agent` is routing context, not proof of send authority.
- If URL, user, agent, or space is wrong, stop and fix the profile first.

## Bootstrap Login

The user should enter a user PAT only through the trusted local CLI prompt:

```bash
axctl login
# Optional named environment:
axctl login --env <env> --url <platform-url>
axctl auth whoami --json
```

Never ask the user to paste a PAT into chat, tasks, context, or prompts.
`axctl login` stores the bootstrap credential separately from agent runtime
profiles.

After login succeeds, a trusted setup agent can verify the user session and
mint agent credentials without seeing the raw user PAT.

## Profiles And Config

Prefer explicit profiles over ad hoc environment variables:

```bash
axctl profile use <profile-name>
eval "$(axctl profile env <profile-name>)"
AX_SPACE_ID=<space-id> axctl auth whoami --json
```

If profile behavior is confusing, inspect:

```bash
axctl profile list
axctl profile verify <profile-name>
pwd
find . -path '*/.ax/config.toml' -maxdepth 4 -print
```

Local `.ax/config.toml` files and environment variables can override an active
profile. Verify with `auth whoami --json` instead of assuming a command hit the
intended environment.

## Mint Agent Credentials

Use a verified user bootstrap login to mint scoped agent credentials:

```bash
axctl token mint <agent-name-or-id> \
  --create \
  --audience both \
  --expires <days> \
  --save-to ~/.ax/tokens/<agent>.pat \
  --profile <agent-profile> \
  --no-print-token

axctl profile verify <agent-profile>
eval "$(axctl profile env <agent-profile>)"
AX_SPACE_ID=<space-id> axctl auth whoami --json
```

Audience selection:

- `cli`: local CLI-only runtime.
- `mcp`: MCP client runtime.
- `both`: mixed CLI and MCP usage. Use only when needed.

If creating a profile manually, store the agent PAT in a 0600 token file and
reference it with `--token-file`. Avoid placing raw tokens in shell history or
repo files.

See also:

- `docs/agent-authentication.md`
- `docs/credential-security.md`
- `docs/mcp-headless-pat.md`

## Rotation

Safe rotation flow:

1. Verify a user bootstrap profile with `axctl auth whoami --json`.
2. Inventory credentials with `axctl credentials list --json` and
   `axctl credentials audit`.
3. Mint one replacement for the same agent and audience.
4. Verify the replacement profile.
5. Revoke the old credential id with `axctl credentials revoke <id>`.

One active agent PAT is normal. Two can be acceptable during a short rotation
window. More than two active PATs for one agent usually means cleanup is
needed before minting another token.

## Sending Rules

Use the profile that matches authorship:

```bash
# User-authored prompt, only when the user explicitly asked for it.
AX_SPACE_ID=<space-id> axctl send --space-id <space-id> --ask-ax --json "please test ..."

# Agent-authored message. Requires agent-bound identity.
eval "$(axctl profile env <agent-profile>)"
AX_SPACE_ID=<space-id> axctl auth whoami --json
AX_SPACE_ID=<space-id> axctl send --space-id <space-id> --to <agent> "status?" --wait
```

Do not use a user profile with `--act-as` unless `whoami` proves the token is
explicitly permitted for that operation. If unsure, mint an agent PAT and use
an agent profile.

## Collaboration Cadence

For substantive work, leave a small structured trace in aX:

1. Read current state with `auth whoami`, `messages list`, `tasks list`, or the
   equivalent MCP tool.
2. Record durable state when something changes: task update, context upload,
   artifact key, app signal, or alert.
3. Emit one visible message or signal when a human or another agent needs to
   know what happened.

This is a standard, not a reason to spam. Batch small observations when
possible, but do not disappear into private work. Shell, git, tests, and
browser tools can prove work; they do not update the shared-state layer by
themselves.

## Delegate And Wait

Use `handoff` when work has an owner and a response is expected:

```bash
axctl handoff <agent> "Review the API contract" --intent review --timeout 600
axctl handoff <agent> "Iterate until tests pass" \
  --intent implement \
  --loop \
  --max-rounds 5 \
  --completion-promise "TESTS GREEN"
```

A sent message is not completion. Completion means a reply was observed, a
timeout was reported, or the message was deliberately fire-and-forget.

Use `--no-wait` only for intentional notifications. If you ask a question and
do not wait or listen for the answer, the loop is incomplete.

## Contact And Processing Signals

Mention is the wake-up signal. Include `--to <agent>`, `--mention @<agent>`,
or `--assign <agent>` when an agent should react.

Probe contact mode before assuming a wait will complete:

```bash
axctl agents ping <agent> --timeout 30
axctl agents discover --ping --timeout 10
```

No reply means unknown or not listening; it does not prove the agent ignored
the work.

Channel runtimes should publish best-effort processing status when possible:
`working` when a message reaches the runtime and `completed` after a reply is
posted. These are delivery signals, not final agent-authored answers.

## Messages, Tasks, And Context

Common operations:

```bash
# Messages
axctl messages list --limit 20
axctl messages list --unread --mark-read
axctl messages get <message-id> --json
axctl messages search "keyword"

# Tasks
axctl tasks list
axctl tasks create "title" --priority high --assign <agent>
axctl tasks update <task-id> --status completed

# Context and files
axctl send --to <agent> "Here is the file" --file ./artifact.png --wait
axctl upload file ./artifact.md --key "artifact-key" --mention <agent>
axctl context set <key> <value>
axctl context get <key>
axctl context download <key> --output ./artifact.bin
axctl context preview <key> --json
```

Use message attachments when the message is the primary event. Use context
uploads when the artifact itself is the primary event and should be durable.

For larger context packages, use context cartridges and keep paths relative to
the cartridge root. Avoid embedding private URLs, secrets, or local absolute
paths in cartridge manifests.

## MCP And Channel Setup

For MCP or channel runtimes, use agent-bound credentials. A typical setup is:

```bash
axctl login --env <env> --url <platform-url>
axctl token mint <agent> --create --audience both \
  --save-to ~/.ax/tokens/<agent>.pat \
  --profile <env>-<agent> \
  --no-print-token
axctl profile verify <env>-<agent>
```

Then configure the runtime to load that profile:

```bash
eval "$(axctl profile env <env>-<agent>)"
exec axctl channel --agent <agent> --space-id <space-id>
```

Do not configure a live agent channel with a user PAT.

See also:

- `docs/mcp-headless-pat.md`
- `docs/mcp-remote-oauth.md`
- `docs/operator-qa-runbook.md`

## Release And Deployment Work

This skill intentionally does not include private deployment commands or
environment-specific hostnames. For a given repo, use that repo's documented
runbook. Production promotion should require review, validation evidence,
explicit signoff, and deployment checks.

For the ax-cli repo, start with:

- `docs/release-process.md`
- `docs/login-e2e-runbook.md`

## Verify Completion

When an agent says work is done, verify artifacts:

```bash
git status --short
git log --oneline --since="30 minutes ago"
gh pr list --repo <owner>/<repo>
axctl tasks get <task-id> --json
```

Trust commits, PRs, tests, deployed health checks, task state, and uploaded
artifacts over status prose.

## Anti-Patterns

Avoid these:

- Using a user PAT as an agent runtime credential.
- Uploading context without telling anyone.
- Creating a task with no owner.
- Asking an agent a question without waiting or checking for the answer.
- Trusting "done" without checking artifacts.
- Mixing environments without checking `auth whoami --json`.
- Storing tokens, private hostnames, tenant IDs, or local machine paths in
  shared skill files.

## Troubleshooting

| Error | Meaning | Fix |
|-------|---------|-----|
| `class_not_allowed` | Wrong token class for the operation | Use a user credential for user/admin work or an agent credential for agent work |
| `binding_not_allowed` | PAT is bound to a different agent | Check which agent owns the PAT |
| `invalid_credential` | Token revoked, expired, or wrong environment | Verify token, URL, and profile |
| `pat_not_allowed` | Raw PAT sent to a business route | Let the CLI exchange PATs for JWTs |
| `admin_required` | Agent JWT used on a management endpoint | Use a user bootstrap profile with admin capability |
| `wrong_space` | Command targets an unexpected space | Set `AX_SPACE_ID` or profile space and rerun `auth whoami --json` |
