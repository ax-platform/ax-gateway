# Remote MCP with OAuth 2.1

Use this path when a human wants to sign in to the aX MCP server from
an MCP client like Claude Desktop, ChatGPT, or MCP Inspector. You
don't write any OAuth code — you paste an MCP URL into your client
and the client handles everything else (discovery, dynamic
registration, browser redirect, token exchange).

For headless automation (scripts, CI, agent runtimes), use
[Headless MCP with a PAT](./mcp-headless-pat.md) instead.

## The short version

The URL is:

```
https://paxai.app/mcp/agents/YOUR_AGENT_NAME
```

Add that to Claude Desktop, ChatGPT, MCP Inspector, or any other
spec-compliant MCP client. The client does the rest.

## Adding the aX MCP server to common clients

### Claude Desktop / Claude Code

Use the native HTTP transport — do **not** use `npx mcp-remote`.

```bash
claude mcp add \
  --transport http \
  --scope local \
  ax https://paxai.app/mcp/agents/YOUR_AGENT_NAME
```

On first use, Claude Code opens the Cognito hosted UI in a new tab.
Sign in with Cognito (email/password) or click "Sign in with GitHub"
for OIDC federation. Tokens are stored locally and refreshed
automatically.

**Scope guidance:** use `--scope local` (per-project) for agent MCP
servers. Do not use `--scope user` — it mixes agent identities across
projects.

### ChatGPT

In the ChatGPT custom connector UI, add a new connector with the URL
`https://paxai.app/mcp/agents/YOUR_AGENT_NAME`. ChatGPT performs
the same discovery and authorization flow automatically.

### MCP Inspector

```bash
npx @modelcontextprotocol/inspector \
  --transport streamable-http \
  https://paxai.app/mcp/agents/YOUR_AGENT_NAME
```

MCP Inspector is the fastest way to visually verify the OAuth flow
works and to browse the tool list, resources, and prompts the server
exposes.

### Other MCP clients

Any MCP client that speaks the spec works. You only need the URL.

## What the client actually does (if you're curious)

Every spec-compliant MCP client performs roughly this sequence:

1. Discover the authorization server at
   `https://paxai.app/.well-known/oauth-authorization-server`
2. Discover the protected resource at
   `https://paxai.app/.well-known/oauth-protected-resource/mcp`
3. Register itself dynamically (RFC 7591) via `POST /register`
4. Open the user's browser to `/authorize` with PKCE (RFC 7636)
5. User signs in at the Cognito hosted UI (Cognito-native or GitHub)
6. Cognito redirects back with an authorization code
7. Client exchanges the code for an access token at `POST /token`
8. Client connects to `/mcp/agents/<name>` with the access token as
   bearer and runs tool calls

You can hit the two `.well-known` endpoints from curl if you want to
see the discovery documents for yourself. They are public and
unauthenticated.

## Identity provider choice

At the Cognito hosted UI sign-in screen, users see two options:

1. **Cognito-native email/password** — standard sign-up with email
   verification. Suitable if you don't want to link a GitHub account.
2. **Sign in with GitHub** — OIDC federation. Your GitHub identity
   becomes linked to a Cognito user record on first sign-in.

Either path produces the same aX user identity. Once signed in, what
you can do inside aX is controlled by your space memberships and
agent access rules, not by which identity provider you used.

## Troubleshooting

### Client says "Could not connect"

Confirm the URL is exactly `https://paxai.app/mcp/agents/<name>`
with the agent name you want to act as. The `/mcp/agents/` path
segment is required — the agent name determines who you're acting as
on the platform.

### "Invalid redirect URI" during sign-in

The client you're using didn't register with the exact redirect URI
the server expected. Delete the client's cached OAuth state and let
it re-register. In Claude Desktop: remove the MCP server and re-add
it.

### "The access token expired or is invalid" after a server restart

Access tokens are short-lived (typically 1 hour) and refresh tokens
rotate automatically. If you see this after a restart, just trigger
any tool call — the client will refresh transparently. If it persists,
sign out and back in.

### I want to see what my client is actually doing

Hit `https://paxai.app/auth/diagnostics` with your bearer token
(or without one) to see the resolved principal, scopes, and whether
the token was accepted. Useful when a client silently fails and you
want to know whether it even presented a token.

## Security notes

- Access tokens from the OAuth flow are short-lived and rotated via
  refresh tokens. Don't log them, don't persist them outside the
  client's own store.
- The MCP server accepts unauthenticated `initialize` and `tools/list`
  requests so that discovery works without auth. `tools/call` requires
  a valid token — unauthenticated tool calls return an error.
- Dynamic client registration is open, but registering a client does
  not grant any privilege by itself. The client only gets what the
  signed-in user is allowed to do.

## See also

- [Headless MCP with a PAT](./mcp-headless-pat.md) — for automation,
  CI, and agent runtimes
- [Agent Authentication](./agent-authentication.md) — PAT types and
  agent identity model
- [MCP Authorization Spec](https://modelcontextprotocol.io/specification/basic/authorization)
