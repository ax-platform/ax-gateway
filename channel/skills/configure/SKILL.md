---
name: configure
description: Set up the aX channel from an axctl-created agent profile/config. Use when the user wants to configure the aX channel or asks "how do I set this up."
user-invocable: true
allowed-tools:
  - Read
  - Write
  - Bash(ls *)
  - Bash(mkdir *)
  - Bash(chmod *)
---

# /ax-channel:configure — aX Channel Setup

Writes aX channel runtime settings to `~/.claude/channels/ax-channel/.env`.
The standard flow is CLI first, channel second: the user runs `axctl login`,
`axctl token mint` creates an agent profile/config, then the channel uses that
agent runtime config.

Arguments passed: `$ARGUMENTS`

---

## Dispatch on arguments

### No args — status and guidance

Read `~/.claude/channels/ax-channel/.env` and show the user their current config:

1. **Config** — `AX_CONFIG_FILE` or `AX_TOKEN_FILE`. Prefer `AX_CONFIG_FILE`
   from `axctl token mint --save-to`.
2. **API URL** — `AX_BASE_URL` (default: `https://paxai.app`)
3. **Agent** — `AX_AGENT_NAME` (who the channel listens as)
4. **Agent ID** — `AX_AGENT_ID` (for reply identity)
5. **Space** — `AX_SPACE_ID` (which space to bridge)

**What next** based on state:
- No config/token file → *"Run `axctl login`, mint an agent token with `axctl token mint --save-to ... --profile ...`, then configure AX_CONFIG_FILE."*
- Token set but no agent → *"Set your agent: `/ax-channel:configure agent <name>`"*
- Everything set → *"Ready. Restart with `claude --dangerously-load-development-channels server:ax-channel`"*

### `<path>` — save config/token path

1. Treat `$ARGUMENTS` as a config/token file path unless it starts with a known subcommand.
2. `mkdir -p ~/.claude/channels/ax-channel`
3. Read existing `.env` if present; update/add `AX_CONFIG_FILE=` for `.toml`
   paths or `AX_TOKEN_FILE=` for token-file paths.
4. `chmod 600 ~/.claude/channels/ax-channel/.env`
5. Confirm, then show status.

Do not accept or save a raw user PAT in channel config. If a user has only a
user PAT, tell them to run `axctl login` directly in their trusted terminal.

### `agent <name> <id>` — set agent identity

Update `AX_AGENT_NAME` and optionally `AX_AGENT_ID` in `.env`.

### `space <space_id>` — set space

Update `AX_SPACE_ID` in `.env`.

### `url <base_url>` — set API URL

Update `AX_BASE_URL` in `.env`. Default is `https://paxai.app`.

### `clear` — remove all config

Delete `~/.claude/channels/ax-channel/.env`.

---

## Implementation notes

- The server reads `.env` at boot. Config changes need a session restart.
  Say so after saving.
- Channel runtime should use an agent PAT (`axp_a_...`) through
  `AX_CONFIG_FILE` or `AX_TOKEN_FILE` for the configured channel agent.
  Do not run a channel agent from a user PAT; user tokens are bootstrap/admin
  credentials and make attribution ambiguous.
- The `.env` file format is simple KEY=VALUE, one per line, no quotes.
