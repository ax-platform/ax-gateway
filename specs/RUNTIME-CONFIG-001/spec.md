# RUNTIME-CONFIG-001: Shared Agent Runtime Configuration

**Status:** Draft  
**Owner:** @madtank / @ChatGPT  
**Date:** 2026-04-13  
**Related:** AGENT-PAT-001, AXCTL-BOOTSTRAP-001, IDENTIFIER-DISPLAY-001

## Summary

aX is API-first. The CLI, Claude Code channel, headless MCP clients, MCP apps,
and frontend are all clients of the same platform API and must share the same
identity rules.

An agent runtime should have one portable config shape that every headless
client can consume:

```toml
token_file = "/home/ax-agent/agents/orion/.ax/orion_token"
base_url = "https://paxai.app"
agent_name = "orion"
agent_id = "agent-uuid"
space_id = "optional-default-space-uuid"
```

This config represents an agent runtime. It is not a user login.

## Core Rules

- User PATs are bootstrap credentials.
- User PATs may authenticate user-authored work and mint scoped agent PATs.
- User PATs must not be accepted as agent runtime authority.
- Agent runtimes use agent PATs exchanged for short-lived agent JWTs.
- Supplying an agent route, header, profile field, or MCP URL must not convert a
  user token into an agent principal.
- The UI, API, CLI, channel, and MCP must enforce the same identity boundary.

The UI already follows this model: a user session cannot send messages as an
agent. The API and headless tooling must not allow that either.

## Consumers

The same agent config should be usable by:

- `axctl` commands from the agent directory
- `axctl profile env`
- Claude Code `ax-channel`
- headless remote MCP tests using MCPJam SDK
- future MCP headless client wrappers

## Config Resolution

Recommended precedence:

1. explicit environment variables such as `AX_TOKEN`, `AX_TOKEN_FILE`,
   `AX_CONFIG_FILE`
2. explicit `AX_CONFIG_FILE`
3. local `.ax/config.toml` discovered from the current working directory
4. named profile selected by the user
5. fallback global defaults

Fallback `.env` files may be supported for compatibility, but they must not
override an explicit `AX_CONFIG_FILE` or explicit token file.

## User Login Separation

User login lives outside agent runtime config and is used for setup and
user-authored operations.

Default login:

```text
~/.ax/user.toml
```

Named environment logins for admins and customer VPC installs:

```text
~/.ax/users/dev/user.toml
~/.ax/users/next/user.toml
~/.ax/users/customer-a/user.toml
```

`axctl login --env dev --url https://dev.paxai.app` writes the named login and
marks it active. Commands can also select one explicitly with `AX_ENV=dev`,
`AX_USER_ENV=dev`, or command-specific `--env` flags.

Agent runtime config lives in the agent work directory or in a named profile.

This allows:

- user rotates setup token without breaking agent runtime
- admins keep dev, next, prod, and customer VPC bootstrap logins side by side
- setup agent mints scoped PATs without reading the raw user token
- agent process runs only with its own scoped credential

## Headless MCP Contract

Headless MCP uses the same config:

1. read `token_file`, `base_url`, `agent_name`, `agent_id`, and `space_id`
2. exchange the agent PAT for `agent_access` with audience `ax-mcp`
3. connect to `${base_url}/mcp/agents/${agent_name}`
4. pass `Authorization: Bearer <agent JWT>`
5. assert `whoami` resolves the same agent identity

If the token starts with `axp_u_`, the client must fail before connecting as an
agent.

## Acceptance Criteria

- `axctl token mint --save-to` writes a config usable by CLI and channel.
- Channel can run from `AX_CONFIG_FILE=/path/to/.ax/config.toml`.
- Headless MCP smoke can run from the same `AX_CONFIG_FILE`.
- User PAT plus agent identity config is rejected.
- `axctl login --env <name>` stores a named user bootstrap login without
  overwriting other environment logins.
- Agent PAT plus matching `agent_id` exchanges to an agent principal.
- Human-facing output prefers names/slugs while JSON preserves UUIDs.
