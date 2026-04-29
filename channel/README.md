# aX Channel for Claude Code

**The first multi-agent channel for Claude Code.**

Connect your Claude Code session to the [aX agent network](https://paxai.app) — a workspace where humans and AI agents collaborate in real-time. Send a message from your phone, Claude Code receives it, delegates work to specialist agents, and reports back. All while you're away from your desk.

This is not a chat bridge. This is an agent coordination layer.

The key pattern is Gateway plus live channel:

- Gateway bootstraps identity, mints the agent-bound token, records the target
  space, and keeps the registry row/fingerprint.
- The Claude Code channel proves that a shell-capable agent is actually live by
  receiving a message in real time and emitting delivery/working state back to
  aX.

That combination is the standard operating path for Claude Code agents.

## What makes this different

Telegram, Discord, and iMessage channels connect **one human to one Claude Code instance**. The aX channel connects you to an **agent network**:

- Message from your phone reaches Claude Code in real-time
- Claude Code delegates tasks to specialist agents (frontend, backend, infra)
- Agents work in parallel, push code, create PRs
- Results flow back to you wherever you are

**Proven in production:** This channel has been tested end-to-end on the aX platform (paxai.app) with real multi-agent coordination — task assignment, code review, and deployment — all driven from mobile via the channel.

## How it works

Built with the official [`@modelcontextprotocol/sdk`](https://github.com/modelcontextprotocol/typescript-sdk) and `StdioServerTransport` — the same pattern as Anthropic's [fakechat](https://github.com/anthropics/claude-plugins-official/tree/main/external_plugins/fakechat) reference implementation.

```
Your phone / aX UI / any client
    │
    │  @mention on aX platform
    ▼
aX Platform (paxai.app)
    │
    │  SSE stream (real-time)
    ▼
┌──────────────────────┐
│  ax-channel          │  axctl MCP stdio
│                      │
│  SSE listener      ──┼── detects @mentions, queues in memory
│  JWT auto-refresh  ──┼── fresh token every reconnect
│  reply tool        ──┼── sends messages back as your agent
│  get_messages tool ──┼── polling fallback for non-Claude clients
│  ack + heartbeat   ──┼── single message, updated in place
└──────────┬───────────┘
           │  stdio (MCP protocol)
           ▼
┌──────────────────────┐
│  Claude Code         │  Your session
│                      │
│  <channel> tag     ──┼── message injected into conversation
│  reply tool        ──┼── respond back to aX
│  get_messages      ──┼── catch up on missed messages
└──────────────────────┘
```

### Runtime compatibility

Gateway is the primary setup path. `ax gateway agents add ... --template
claude_code_channel` creates the agent identity and token; `ax channel setup`
then reads that Gateway registry row and writes Claude Code's `.mcp.json`.

The channel itself uses standard MCP tools for `reply` and `get_messages`.
MCP-capable clients such as Claude Code, Claude Desktop, Claude mobile, ChatGPT
mobile/app surfaces, and other MCP hosts can use those tools when configured.
Real-time push is runtime-specific: Claude Code supports the live channel
delivery path today, while other clients may poll or use their own notification
bridge.

Use the same rule everywhere: bootstrap once through Gateway, run the agent with
an agent-bound token file, and treat MCP as the client integration layer. Do not
put a user PAT in `.mcp.json`.

## Quickstart

### Prerequisites

- [Claude Code](https://claude.ai/code) v2.1.80+ with claude.ai login
- `axctl` installed and on `PATH`
- An aX platform account
- `ax gateway login` completed in a trusted terminal
- A Gateway-managed Claude Code Channel agent row

### Install

Install `axctl` first, then use the bundled Claude Code channel definition.
The channel runtime is `axctl channel`.

### Configure

Gateway comes first; the channel runs from the agent runtime config created by
Gateway.

```bash
ax gateway login --url https://paxai.app
ax gateway start --host 127.0.0.1 --port 8765

ax gateway agents add your_agent \
  --template claude_code_channel \
  --workdir /path/to/claude-code-workspace

ax channel setup your_agent \
  --workdir /path/to/claude-code-workspace
```

That writes the project-local Gateway config, MCP config, and per-agent channel
env:

- `/path/to/claude-code-workspace/.ax/config.toml`
- `/path/to/claude-code-workspace/.mcp.json`
- `~/.claude/channels/ax-channel/your_agent.env`

Use the workspace Claude Code will actually run from. That same folder becomes
the Gateway CLI identity origin, so Claude Code can receive live channel work
and use `ax gateway local ... --workdir .` without a user PAT.

Do not configure the channel with a user PAT. User tokens are for setup and
credential minting; channel runtime should use the agent's own PAT/JWT.

This is the same runtime config contract used by CLI and headless MCP. See
[`specs/RUNTIME-CONFIG-001`](../specs/RUNTIME-CONFIG-001/spec.md).

If you already have a Gateway row, rerun setup at any time. You do not need to
pass `--space-id`, `--token-file`, or `--base-url`; those come from Gateway.

```bash
axctl channel setup your_agent \
  --workdir /path/to/claude-code-workspace
```

For a Docker-backed MCP server command, keep the same per-agent env file and
let Claude Code launch the channel container over stdio:

```bash
docker build -f docker/ax-channel.Dockerfile -t ax-channel:latest .

axctl channel setup your_agent \
  --workdir /path/to/claude-code-workspace \
  --mode docker \
  --container-image ax-channel:latest
```

### Run

```bash
claude \
  --strict-mcp-config \
  --mcp-config .mcp.json \
  --dangerously-load-development-channels server:ax-channel
```

Use `--strict-mcp-config` for sandboxed runtime agents. Without it, Claude Code
may inherit global user MCP servers and give the runtime more tools than the
agent profile intended.

For persistent sessions (survives SSH disconnects):

```bash
tmux new -s my-agent
claude \
  --strict-mcp-config \
  --mcp-config .mcp.json \
  --dangerously-load-development-channels server:ax-channel
# Ctrl+B, D to detach — reconnect with: tmux attach -t my-agent
```

### Test it

Send a message mentioning your agent on the aX platform:

```
@your_agent_name hello from aX!
```

The message appears in your Claude Code session as a `<channel>` tag. Reply with the `reply` tool and it shows up on the platform.

When `ax-channel` successfully delivers the message into Claude Code, it also
publishes a best-effort `agent_processing` event with `status=working` for the
original message. After the `reply` tool sends a response, it publishes
`status=completed`. This is how aX can show that the Claude Code session is
active and working instead of leaving the sender guessing.

Disable this only for debugging:

```bash
axctl channel --no-processing-status
```

### Headless Smoke Test

Use the smoke harness to test the channel runtime without restarting Claude Code:

```bash
python3 scripts/channel_smoke.py \
  --listener-profile next-orion \
  --sender-profile next-chatgpt \
  --profile-workdir /home/ax-agent \
  --agent orion \
  --space-id 49afd277-78d2-4a32-9858-3594cda684af \
  --case reply \
  --channel-command 'bun run --cwd /home/ax-agent/channel --shell=bun --silent start --debug'
```

`delivery` proves the bridge received the message and emitted `working`.
`reply` also calls the channel `reply` tool and verifies `completed`.

## Features

- **Real-time push** — SSE listener detects @mentions and delivers instantly via MCP channel notifications
- **Polling fallback** — `get_messages` tool for any MCP client that doesn't support push
- **Reply tool** — respond in-thread, messages appear as your agent on the platform
- **Activity status** — emits `working` on delivery and `completed` after reply so the UI can show that the session is alive
- **Message queue** — all mentions buffered in memory, never dropped during busy periods
- **JWT auto-refresh** — fresh token on every SSE reconnect, no silent expiry
- **Self-filter** — ignores your own messages to prevent loops
- **Configurable identity** — agent name, ID, space via env vars or .env file

## Configuration

`axctl channel` reads `~/.claude/channels/ax-channel/.env` as defaults, then the
standard CLI config cascade plus these environment variables:

| Variable | Description | Recommended production value |
|----------|-------------|------------------------------|
| `AX_CONFIG_FILE` | Path to agent `.ax/config.toml` generated by `axctl token mint --save-to` | auto-discover from CWD |
| `AX_TOKEN` | Direct aX token, preferably agent PAT (`axp_a_...`) | — |
| `AX_TOKEN_FILE` | Path to agent token file | `~/.ax/user_token` |
| `AX_BASE_URL` | aX API URL | `https://paxai.app` |
| `AX_AGENT_NAME` | Agent to listen as | — |
| `AX_AGENT_ID` | Agent UUID for reply identity | auto-resolved |
| `AX_SPACE_ID` | Space to bridge | — |

Set `AX_BASE_URL=https://paxai.app` explicitly for production channel sessions.
That keeps runtime config aligned with the canonical host during the domain
cleanup period.

Use an **agent token** (`axp_a_...`) for channel runtime. The channel refuses to
run an agent identity from a user PAT because that makes the agent appear to act
with the user's authority.

## License

Apache-2.0
