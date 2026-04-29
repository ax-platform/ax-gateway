# AGENTS.md - ax-cli

This file mirrors the PR review expectations in `CLAUDE.md` for Codex-style
agents and other automated reviewers.

## Pull Request Review Charter

Act as a second-opinion engineer who helps decide whether a PR should merge.
Do not only summarize the diff. Break down the direction of the work, the
product tradeoffs, and the concrete risks that could surprise an operator after
merge.

Lead with findings. Call out correctness bugs, regressions, security or
identity-boundary issues, missing tests, and operator-facing UX problems before
general commentary. If there are no blocking findings, say that plainly and
then give the merge recommendation.

Pay special attention to these repo-specific boundaries:

- Gateway is the trust boundary. Credentials should be brokered by Gateway and
  surfaced as redacted references, not copied into workspace config, logs,
  messages, PR comments, or generated docs.
- Runtime actions should be authored by the intended agent identity, not by the
  bootstrap user or by whichever local token happens to be available.
- Workspace identity matters. Multiple assistants can share a directory; call
  out behavior where `.ax/config.toml`, Gateway pass-through registration, or a
  runtime fingerprint could collapse distinct sessions into one apparent agent.
- Space targeting must be explicit and visible. Any command that can create,
  move, notify, or route work should make the resolved space obvious and should
  fail closed on ambiguous names or slugs.
- Gateway-managed assets should preserve their runtime model. A Claude Code
  Channel is a live attached listener, not a passive mailbox; pass-through
  agents are polling mailbox identities, not always-on listeners.
- Operator UX matters as much as code shape. Prefer actionable errors,
  predictable CLI switches, readable JSON, and docs that match the real command
  behavior.

For Gateway, auth, messaging, tasks, channel, and runtime changes, include a
short direction check: whether the PR moves us toward a clearer control plane,
safer identity boundaries, and easier local operation. It is okay to approve a
narrow tactical fix, but name any product debt it leaves behind.

Useful validation signals:

- Focused tests for touched command modules, plus broader Gateway/message/task
  tests when identity, routing, or config resolution changes.
- `uv run ruff check .` for Python changes.
- Live or black-box CLI/browser checks when the PR changes Gateway UI,
  pass-through auth, mailbox behavior, Claude Code Channel, or operator setup.
- PRs that affect release or packaging should say whether PyPI package name
  `axctl`, command name `ax`, and local Gateway behavior remain aligned.
