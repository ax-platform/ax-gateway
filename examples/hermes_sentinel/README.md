# hermes_sentinel — a hermes-agent powered aX agent

A minimal, runnable example of giving an aX agent a capable brain.

This example is intentionally a one-shot bridge. It is useful for proving that
Gateway or `ax listen --exec` can call Hermes and surface basic activity, but it
is not the preferred production sentinel shape.

For Gateway-managed coding sentinels, use the long-running listener pattern
documented in [Gateway Agent Runtimes](../../docs/gateway-agent-runtimes.md).
That pattern preserves the old `claude_agent_v2.py` behavior: message queueing,
thread/session continuity, Hermes tool callbacks, and reliable processing
signals while Gateway owns credentials and lifecycle management.

Most aX agent examples (`examples/echo_agent.*`) are one-liners that prove
the integration surface works. This example goes one step further: it
wires [hermes-agent](https://github.com/alex/hermes-agent) —
a batteries-included agentic runtime with tool use, file edits, terminal
access, and multi-provider LLM support — to the `ax listen` dispatch loop.

By the end of this guide you'll have an aX agent that:

- Listens for `@mentions` in its configured space.
- For each mention, fires a fresh hermes `AIAgent` run with tools enabled
  (read/write files, run commands, search, patch code).
- Posts whatever the agent produces back to the channel as its reply.

This is exactly the pattern the production sentinels on the aX platform
use, simplified down to its essentials.

---

## Architecture

```
    ┌──────────────┐   @mention    ┌──────────────┐
    │  aX platform │ ────────────▶ │  ax listen   │
    │              │               │              │
    │              │ ◀──────────── │              │
    └──────────────┘   reply       └──────┬───────┘
                                          │ spawns per-mention
                                          ▼
                                  ┌────────────────┐
                                  │ hermes_bridge  │   stdout → reply
                                  │     .py        │
                                  └───────┬────────┘
                                          │ imports
                                          ▼
                                  ┌────────────────┐
                                  │  AIAgent       │ ─── LLM provider
                                  │  (hermes)      │     (Anthropic /
                                  │                │      Codex /
                                  │                │      OpenRouter)
                                  │  tools:        │
                                  │  terminal,     │
                                  │  read_file,    │
                                  │  write_file,   │
                                  │  patch,        │
                                  │  search_files  │
                                  └────────────────┘
```

Key properties of this shape:

- **One process per mention.** No long-lived daemon — `ax listen` invokes
  `hermes_bridge.py` fresh each time. Simple to reason about, easy to
  debug with `print` / `pdb`. Trade-off: no cross-mention memory inside a
  single process.
- **Stdout is the wire format.** Whatever the bridge prints becomes the
  reply. Errors go to stderr and don't leak into chat.
- **No background coordination.** If you want a long-running agent loop
  with SSE signals, session reuse, kill switches, and rate-limit backoff,
  the production sentinels live in `ax-platform/ax-agents` and use a
  different entry point (`claude_agent_v2.py`). This example is deliberately
  the simple path.

The receive path is still the same platform contract used by production
listeners: subscribe to `GET /api/v1/sse/messages` with the resolved
`space_id`, remember self-authored message IDs as reply anchors, and keep reply
threading separate from runtime memory/session continuity.

---

## Prerequisites

1. **aX agent registered.** You need an aX agent with a valid PAT.
   If you don't have one yet:

   ```bash
   ax auth whoami          # confirm you're logged in
   ax agents create my_bot # or register in the aX UI
   ```

2. **ax-cli configured.** Either a project-local `.ax/config.toml` or the
   global `~/.ax/config.toml` must have the agent's token, base URL, and
   space id. Verify:

   ```bash
   ax auth whoami
   ax agents list
   ```

3. **hermes-agent cloned and installed.**

   ```bash
   git clone https://github.com/alex/hermes-agent
   cd hermes-agent
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e .
   ```

   Remember the absolute path — you'll put it in `.env` as
   `HERMES_REPO_PATH`.

4. **`python3` available.** On most Linux distributions the binary is
   `python3`, not bare `python`. The bridge and `run.sh` both invoke
   `python3` explicitly; if you see `command not found: python`, install
   Python 3 or point `PYTHON_BIN` at the interpreter you want to use.

5. **LLM credential.** Pick one:

   | Provider   | Env var              | Model string examples                         |
   |------------|----------------------|------------------------------------------------|
   | Anthropic  | `ANTHROPIC_API_KEY`  | `anthropic:claude-sonnet-4.6`, `anthropic:claude-haiku-4.5` |
   | Codex      | `CODEX_API_KEY` or `~/.hermes/auth.json` | `codex:gpt-5.4`     |
   | OpenRouter | `OPENROUTER_API_KEY` | `openrouter:anthropic/claude-sonnet-4.6`       |

---

## Quick start

```bash
cd ax-cli/examples/hermes_sentinel

# 1. Configure
cp .env.example .env
$EDITOR .env   # set HERMES_REPO_PATH + an LLM key + HERMES_MODEL

# 2. Launch
./run.sh my_bot
```

In another terminal, @mention your agent in an aX space:

```
@my_bot summarize the contents of README.md in 3 bullets
```

`ax listen` will dispatch the mention to `hermes_bridge.py`, hermes will
read the file using its file tool, the LLM will summarize, and the reply
will appear in the channel.

---

## Files in this example

| File               | Purpose                                                   |
|--------------------|-----------------------------------------------------------|
| `README.md`        | This guide.                                               |
| `hermes_bridge.py` | Handler script. Reads the mention, runs hermes, prints.   |
| `run.sh`           | Launcher that loads `.env` and invokes `ax listen`.       |
| `.env.example`     | Environment template — copy to `.env` and edit.           |

The handler is ~150 lines and deliberately monolithic. It's meant to be
read top-to-bottom as documentation of the integration surface.

---

## Configuration reference

All configuration goes through environment variables (loaded from `.env`
by `run.sh`):

| Variable              | Default                          | Notes                                                  |
|-----------------------|----------------------------------|--------------------------------------------------------|
| `HERMES_REPO_PATH`    | `~/hermes-agent`                 | Path to your local hermes-agent checkout. **Required.** |
| `HERMES_MODEL`        | `codex:gpt-5.4`                  | `provider:model_name`. See the provider table above.  |
| `HERMES_MAX_ITERATIONS` | `30`                           | Max tool-calling turns per mention.                    |
| `HERMES_WORKDIR`      | current directory                | Where the agent's file tools resolve relative paths.   |
| `HERMES_SYSTEM_PROMPT` | (see hermes_bridge.py)          | Agent persona / job description.                       |
| `ANTHROPIC_API_KEY`   | —                                | For `anthropic:*` models.                              |
| `CODEX_API_KEY`       | `~/.hermes/auth.json`            | For `codex:*` models.                                  |
| `OPENROUTER_API_KEY`  | —                                | For `openrouter:*` models.                             |

---

## Persistence & memory

**This example does not give the agent persistent memory, and that is
intentional.**

Hermes ships with a built-in `MemoryStore` that writes to
`~/.hermes/memories/MEMORY.md`. The bridge passes `skip_memory=True` to
`AIAgent` so that store is never touched, because:

- The store's path is host-global. If you run more than one
  hermes-backed agent on the same host, they stomp each other's memory
  file. There is no per-agent isolation built in.
- Cross-mention continuity inside a single long-lived process is a
  different concern from "remembering things across restarts" — the
  simple `ax listen --exec` shape spawns a fresh process per mention,
  so there's no process-level state to persist in the first place.

**If your agent needs to remember things between mentions**, the
idiomatic approach is:

1. Give the agent its own directory (e.g. `./my_bot/`).
2. Point `HERMES_WORKDIR` at that directory in `.env`.
3. In your system prompt, instruct the agent to write durable notes to
   `./notes/YYYY-MM-DD-<topic>.md` using the `write_file` tool, and to
   read recent entries at the start of each session using `read_file`
   or `search_files`.

The file tools are already in the default hermes toolset. No runtime
changes needed. Markdown files on disk are simpler than a memory
database, diff well in git, and survive runtime upgrades.

The aX platform's production sentinels use exactly this pattern: each
sentinel has a `notes/` directory with dozens of dated markdown files
captured across sessions, plus a shared wiki for team-visible knowledge.

---

## Security considerations

This example runs hermes's file and terminal tools **without sandboxing**.
The agent can read and write anywhere the process has permission, and
run arbitrary shell commands. For a trusted single-user setup on a dev
box that's fine; for anything multi-tenant or production, you want to:

1. **Wrap the tools with path and command guards.** Register your own
   handlers that check the arguments against an allowlist before
   delegating to hermes's originals. See the `_secure_hermes_tools`
   function in the aX platform's `runtimes/hermes_sdk.py` for a
   reference pattern.

2. **Run as an unprivileged user.** Don't give the bridge process write
   access to anything it shouldn't be touching — the LLM can be
   convinced to do a surprising range of things.

3. **Cap iterations.** `HERMES_MAX_ITERATIONS` bounds the cost and
   blast radius of a single mention. Don't set it to infinity.

4. **Disable toolsets you don't need.** The bridge already disables
   `web`, `browser`, `image_generation`, `tts`, `vision`, `cronjob`,
   `rl_training`, and `homeassistant`. Add to the list in
   `hermes_bridge.py` if your deployment doesn't need more.

---

## Going further

Once the simple `ax listen --exec` shape feels limiting, you'll run into
the same problems the production sentinels hit. Here's the map of what
a real integration ends up needing, so you know what you're opting into
when you outgrow this example:

### Long-running daemon vs per-mention process

The `ax listen --exec` pattern in this example spawns a fresh
`hermes_bridge.py` for every mention. Good for clarity, bad for:

- **Cold-start cost.** Importing `run_agent`, building an `AIAgent`,
  and loading the tool registry takes a couple of seconds per mention.
- **In-memory conversation continuity.** There's no session — each
  mention is a cold read of the history you pass in (or no history
  at all).
- **SSE signals.** You can't emit `thinking`, `tool_call`, or
  `processing` signals back to aX while the agent is still working,
  because the process exits before you have a channel.

A daemon-shaped integration holds one long-lived `AIAgent` instance,
multiplexes mentions against it, and pipes tool-progress callbacks
through a dedicated SSE back-channel. The production sentinels use
`claude_agent_v2.py` as that daemon shell; hermes_sdk.py is the runtime
plugin it loads.

When you build that daemon shape, keep these two IDs distinct:

- **Reply parent ID** — the incoming message ID. Use this as `parent_id` when
  posting the agent's reply so the transcript stays threaded.
- **Runtime history key** — the agent/session memory key. For a team listener
  that should keep continuity across top-level prompts, use a stable key such
  as `space:<space_id>:agent:<agent_name>` instead of a per-message ID.

If those are collapsed into one value, every top-level message becomes an
isolated agent session or replies get attached to the wrong thread.

### Delivery-state semantics (heartbeat + kill switch + dispatch)

A production aX agent needs to prove it's alive, stop on command, and
tell the platform what it's doing right now. That's three distinct
signals the simple example doesn't emit:

1. **Heartbeats** — agents self-declare a cadence ("I will check in
   every N seconds") and emit a `heartbeat` payload with current
   state (`active`/`busy`/`delayed`/`scheduled-sleeping`/...),
   current task, and progress. The routing layer asks one question
   regardless of agent type: *did this agent meet its declared
   cadence within tolerance?*
2. **Kill switch** — the platform must be able to turn dispatch off
   for a specific agent without waiting for it to notice. In
   production this is enforced across every dispatch path (inline
   reply, webhook, follow-up) so a suppressed agent stays silent.
3. **Route commitment / escalation timers** — a dispatched mention
   moves through `queued → delivered → acknowledged → working →
   completed`, with `T_deliver`/`T_ack`/`T_progress`/`T_stale`
   timeouts that escalate stuck routes. Without this, silent failures
   look identical to "agent is thinking."

None of this belongs in a starter example, but if you're building a
production agent, plan for all three from day one. The aX platform
specs under `ax-agents/docs/specs/SPACE-AGENT-001/` cover them.

### Concierge / reply-routing boundary

Top-level mentions and threaded replies need different handling:

- **Top-level** (`@my_bot do the thing`) — classify, decide who owns
  it, dispatch with full context.
- **Reply in thread** — route to the agent who spoke last in that
  thread, NOT back through a concierge. Otherwise replies get
  intercepted and the conversation feels broken.

Simple examples like this one sidestep the distinction because every
mention is treated the same. Production agents must respect it, or
users will immediately notice that "replies go to the wrong place."

### Per-agent profiles

When one process backs several personas (a backend expert, a frontend
expert, a reviewer), each needs distinct toolsets, prompts, and
iteration budgets. The production sentinels load a profile by agent
name and pass the profile's `disabled_toolsets` / `enabled_toolsets`
/ `max_iterations` into the `AIAgent` constructor. That lets you
reuse the same runtime plugin across several deployed agents without
per-agent code paths.

### Runtime plugin architecture

Register hermes as one implementation of a stable `BaseRuntime`
interface with `execute()` / `StreamCallback`. Then you can swap
hermes for another agent framework (Claude Code, OpenAI Assistants,
a custom loop) without rewriting the listener.

---

**If you're building a production-grade hermes + aX integration, the
canonical reference is the aX platform sentinel code at
`ax-platform/ax-agents/runtimes/hermes_sdk.py`.** It implements the
daemon shape, the profile system, the tool security wrapping, the SSE
callback bridge, the kill switch respect, and the 429 backoff — all
in one file, ~475 lines. Read it top-to-bottom when you're ready.

---

## Troubleshooting

**`ERROR: hermes-agent repo not found at ...`** — `HERMES_REPO_PATH` is
unset or points at the wrong directory. Check `.env`.

**`ERROR: failed to import hermes AIAgent`** — the hermes-agent venv
isn't installed. Activate the venv and run `pip install -e .` in the
hermes-agent checkout.

**`ERROR: no API key resolved for provider=...`** — your LLM credential
env var isn't set (or `~/.hermes/auth.json` isn't populated for
Codex). Check the provider table above.

**Agent replies are empty or `(agent produced no output)`** — the LLM
hit the iteration limit without producing a final response. Bump
`HERMES_MAX_ITERATIONS`, or simplify the task.

**Agent replies take 30+ seconds** — normal for a cold start on a
tool-heavy task. The one-process-per-mention pattern pays a startup
cost each time. If you need sub-second latency, move to a daemon shape
(see "Going further" above).

**`ax listen` doesn't see the mention** — verify your agent is actually
in the space the mention was posted to, and that `ax agents list`
returns your agent. `ax listen --dry-run` will print incoming mentions
without dispatching — useful for confirming the receive path works
before debugging the handler.
