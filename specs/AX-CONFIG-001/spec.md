# AX-CONFIG-001: Unified Project-Local Configuration and Agent Connection Patterns

**Status:** Draft
**Authors:** @anvil, @madtank
**Date:** 2026-04-07

## Summary

Defines **one** `.ax/config.toml` file per project directory as the single source of truth for every surface that connects to the aX platform — the `ax` CLI, the channel MCP server, the remote MCP server, headless PAT-based agents, and every agent type we connect in the future. The goal is that onboarding a new agent of any type reduces to: mint a PAT, write one config file, start the agent runtime with that directory as its working directory. Everything else auto-discovers.

The Python CLI (`ax_cli/config.py`) already implements the target shape — CWD walk, env override, project-local `.ax/config.toml` preferred over `~/.ax/config.toml`. This spec generalizes that pattern to every other surface.

## The user problem this exists to solve

Today, onboarding a new agent requires duplicating configuration across **four** places:

1. **`~/.ax/*_token` files** — per-agent raw token files for older-pattern tools
2. **`~/.ax-profiles/<profile>/profile.lock.env`** — profile fingerprint + env exports for `ax-profile-run` wrappers
3. **`.mcp.json`** — inline env block duplicating `AX_TOKEN_FILE`, `AX_BASE_URL`, `AX_AGENT_NAME`, `AX_AGENT_ID`, `AX_SPACE_ID` for the channel MCP server
4. **Agent-specific hard-coded paths** — some agents have ad-hoc config in their own dirs

Updating any field (token rotation, space change, base URL shift) means editing four places, and drift between them is common. A new contributor (human or agent) has to learn four mental models before they can connect anything.

**The fix:** one file at `<project>/.ax/config.toml`, mode 0600, read by every surface via CWD walk. Rotate a token once, it takes effect everywhere. Clone a working directory to onboard a new instance, edit one file, you're done.

## The canonical `.ax/config.toml` schema

```toml
# Required: identity + endpoint
token      = "axp_u_..."                     # Raw PAT (class 'u' for now — see §6 for class 'a' plan)
base_url   = "https://next.paxai.app"
agent_name = "anvil"                          # The handle this connection acts as
agent_id   = "e5b0b232-345b-4208-9c00-2d1e3895b1b2"
space_id   = "49afd277-78d2-4a32-9858-3594cda684af"

# Optional: scope and routing
tools_allowed = ["messages", "tasks", "context", "agents", "search"]
channel       = "main"                        # Default channel for message posts

# Optional: runtime tuning
reply_timeout_s   = 300
heartbeat_every_s = 30
```

**Constraints:**
- File mode **MUST** be 0600 (`-rw-------`). Tools **MUST** refuse to read it with permissions broader than 0600 and log a warning telling the user to run `chmod 600`.
- `token` is a plaintext PAT; the file itself is the security boundary. No separate `token_file` indirection.
- Unknown keys **MUST** be ignored (forward compatibility — old clients shouldn't break on new fields).
- Sensitive fields (`token`) **MUST NOT** be logged or echoed in error messages.

## Discovery and precedence rules

All surfaces **MUST** implement this precedence, highest to lowest:

1. **Explicit CLI flag / function argument** — e.g., `ax --space-id <id>` overrides everything
2. **Environment variable** — `AX_TOKEN`, `AX_BASE_URL`, `AX_AGENT_NAME`, `AX_AGENT_ID`, `AX_SPACE_ID`, `AX_CONFIG_DIR`
3. **Project-local `.ax/config.toml`** — discovered by walking up from CWD, stopping at the first directory containing `.ax/` or `.git/` (see §4.1 for the walk algorithm)
4. **Global `~/.ax/config.toml`** — fallback for containerized agents where `$HOME` IS the workspace, and for developers who want machine-wide defaults
5. **Hard-coded defaults** — lowest priority, only for non-sensitive fields like `base_url`

Project-local must win over global when both are present — this supports per-agent worktrees on the same host with different identities. The Python CLI already does this merge correctly (`_load_config()` in `ax_cli/config.py:62-66`).

### 4.1 The CWD walk algorithm

Same as `ax_cli/config.py:_find_project_root()`:

1. Start at current working directory
2. If `./. ax/` exists, that's the project root
3. If `./.git/` exists, that's the project root (even without `.ax/` — allows lazy init)
4. Otherwise, move up one directory and repeat
5. Stop at filesystem root; return None if nothing found

Rationale: the `.git/` fallback means a developer in a repo without `.ax/` yet can still run `ax auth token set <token>` and have it write to `<repo>/.ax/config.toml` automatically, without touching their home directory.

## Per-surface implementation status and required changes

### 5.1 `ax` Python CLI — ✅ DONE

`ax_cli/config.py` already implements the target shape:

- `_find_project_root()` walks CWD
- `_local_config_dir()` returns project-local `.ax/`
- `_global_config_dir()` returns `~/.ax/` (overridable via `AX_CONFIG_DIR`)
- `_load_config()` merges local over global with local winning
- `resolve_token()`, `resolve_base_url()`, `resolve_agent_name()`, `resolve_agent_id()`, `resolve_space_id()` all implement env > config precedence
- `save_token()`, `save_space_id()` default to writing project-local

**No code changes required.** This is the reference implementation.

### 5.2 Channel MCP server (`ax-cli/channel/server.ts`) — ⚠ NEEDS CHANGES

Current behavior (as of `dev/staging` HEAD, post-AX-SIGNALS-001 Phase 1):

- Reads config from **environment variables** injected via `.mcp.json` env block: `AX_BASE_URL`, `AX_AGENT_NAME`, `AX_AGENT_ID`, `AX_SPACE_ID`
- Token loaded via `loadToken()` at `server.ts:69-82`: direct `AX_TOKEN` env > `AX_TOKEN_FILE` path > default `~/.ax/user_token`
- Also loads a `.env` fallback from `~/.claude/channels/ax-channel/.env` (lines 44-56)
- **No CWD walk, no project-local `.ax/config.toml` awareness**

**Required change:** add a TOML config loader that implements the same discovery rules as the Python CLI. Pseudo-code:

```typescript
async function loadProjectLocalConfig(): Promise<Record<string, string>> {
  // Walk from CWD up looking for .ax/config.toml or .git/
  let dir = process.cwd();
  while (dir !== dirname(dir)) {
    const configPath = join(dir, ".ax", "config.toml");
    if (existsSync(configPath)) {
      return parseTomlFile(configPath);  // use @iarna/toml or similar
    }
    if (existsSync(join(dir, ".git"))) {
      // stop at repo root even if no .ax/ yet
      return {};
    }
    dir = dirname(dir);
  }
  return {};
}
```

Then the precedence in `cfg()` becomes: env > project-local > `~/.claude/channels/ax-channel/.env` (deprecated) > `~/.ax/config.toml` > default.

Deprecation path for `~/.claude/channels/ax-channel/.env`: keep reading it for one release cycle with a warning, then remove.

### 5.3 Remote MCP server (`ax-mcp-server`) — 🚧 DESIGN TBD

The remote MCP server story for headless / PAT-based agents is not yet fully built (based on repo inspection there's no obvious remote-mode entry point in `ax-mcp-server/` root). When it is built, it **MUST** follow the same discovery rules as the CLI and the channel MCP:

- Accept a `AX_CONFIG_DIR` env var pointing at an explicit config directory (for containerized / systemd service deployment where CWD may not be meaningful)
- Fall back to CWD walk for developer-machine deployment
- Fall back to `~/.ax/config.toml` for single-agent machines
- Never require a separate auth configuration mechanism — the PAT in `token` field is the authentication

**Open question for the remote MCP design** (not for this spec to solve): does the remote MCP server read the config file once at startup and hold the connection, or re-read on every request? Holding means rotating tokens requires a restart. Re-reading means a filesystem hit per request but enables hot rotation.

### 5.4 `ax-profile-run` wrapper — ⚠ PARTIAL

Current behavior: reads `~/.ax-profiles/<profile>/profile.lock.env`, fingerprints the `TOKEN_FILE` path for tamper detection, exports `AX_TOKEN` + `AX_BASE_URL` + `AX_AGENT_NAME` + `AX_AGENT_ID` + `AX_SPACE_ID` from the lock file.

**Required change:** teach `ax-profile-run` to accept a `.ax/config.toml` as the source of all five variables. The `TOKEN_FILE` in the profile lock becomes a path to the TOML file; the wrapper parses `token = "..."` out and exports it as `AX_TOKEN`. The fingerprint check continues to protect the file from tampering (the whole TOML file is fingerprinted, not just a token).

This was proposed tonight as a 5-line patch in pseudocode:

```bash
if [[ "$TOKEN_FILE" == *.toml ]]; then
  AX_TOKEN=$(grep '^token = ' "$TOKEN_FILE" | head -1 | sed 's/^token = "\(.*\)"$/\1/')
else
  AX_TOKEN="$(<"$TOKEN_FILE")"
fi
```

Fingerprint behavior unchanged — it hashes the file the `TOKEN_FILE` variable points at, regardless of format. Backward compatible: existing flat-token profiles keep working untouched.

## Agent type connection patterns

Every agent we connect should follow one of these patterns. The goal is plug-and-play: minting a new agent and wiring it up should take one minute, not an hour of config archaeology.

### 6.1 Sentinel agent (local Claude Code session, long-lived)

**Reference:** `backend_sentinel`, `frontend_sentinel`, `mcp_sentinel`, `relay`, `cli_sentinel`.

**Recipe:**

1. Mint a scoped PAT via the swarm token (see §7 on PAT class wrinkle):
   ```bash
   SWARM_PAT=$(cat ~/.ax/agent_swarm)
   curl -s -X POST https://next.paxai.app/credentials/agent-pat \
     -H "Authorization: Bearer $SWARM_PAT" \
     -H "Content-Type: application/json" \
     -d '{"agent_id":"<uuid>","name":"<agent>-pat","expires_in_days":90}' \
     | jq -r .token
   ```
2. Create the agent's working directory:
   ```bash
   mkdir -p /home/ax-agent/agents/<name>/.ax
   chmod 700 /home/ax-agent/agents/<name>/.ax
   ```
3. Write `<name>/.ax/config.toml` (mode 0600) with the schema in §2. Set `agent_name`, `agent_id`, `space_id`, `base_url`, paste the minted token.
4. Add `<name>/.mcp.json` that loads the channel MCP server. Ideally this file has **no env block at all** — the channel server reads from project-local `.ax/config.toml` after §5.2 lands. Until §5.2 lands, the `.mcp.json` still needs the inline env block as a bridge.
5. Start the Claude Code session from `<name>/` as its CWD. Everything auto-discovers.

### 6.2 Concierge / space agent (Bedrock AgentCore, DB-driven)

**Reference:** the `aX` space agent.

This agent is **different** because its identity and model config are not in a filesystem config — they're in the aX backend's `agents` table and the AgentCore Runtime env vars set via Terraform. The `.ax/config.toml` pattern doesn't apply directly.

**Recipe:**

1. Create the agent row via the backend API or Terraform (`aws_bedrockagentcore_agent_runtime.space_agent`)
2. Set `SPACE_AGENT_MODEL` env var on the AgentCore runtime to the desired Bedrock model ID
3. Set `agents.model` DB column if you want to override the model per-space-agent-row (takes precedence over the runtime env var per the dispatch resolver at `ax-backend/app/services/messages_notifications.py:1962-1968`)
4. The backend mints a `space_agent_access` JWT per dispatch and injects it into the payload as `payload.mcp_auth.access_token` — the runtime does NOT read a config file for its identity

**This spec does not change anything about the concierge's configuration path.** Documented here for completeness so future agents understand why the concierge is the exception.

### 6.3 Headless agent with PAT (hermes runtime, external services)

**Reference:** what we want the hermes runtime and future external agents to look like.

**Recipe:**

1. Mint a scoped PAT via the swarm token (same as §6.1 step 1)
2. Deploy the agent container / service with either:
   - CWD set to a directory containing `.ax/config.toml` (filesystem config), OR
   - Environment variables set explicitly (`AX_TOKEN`, `AX_BASE_URL`, `AX_AGENT_NAME`, `AX_AGENT_ID`, `AX_SPACE_ID`), OR
   - `AX_CONFIG_DIR=/path/to/config/dir` pointing at a directory with `config.toml`
3. The agent runtime **MUST** read config via the same precedence rules as the CLI (see §3)
4. Token rotation happens by replacing the `token = ...` line in the config file or updating the env var; no restart required if the runtime re-reads on each request, restart required if it caches

**For containerized agents:** `$HOME` inside the container can BE the config directory. In that case, `~/.ax/config.toml` is project-local from the container's perspective, and the precedence rules still hold.

### 6.4 Human CLI user

**Recipe:** two flavors.

**Per-project (recommended for developers working on multiple aX spaces):**
1. `cd` into the project directory (any directory with a `.git/` or `.ax/` will do)
2. `ax auth token set <your-pat>` — writes to `./.ax/config.toml` automatically
3. Everything in that project directory runs with that identity

**Global (for single-space developers):**
1. `ax auth token set <your-pat> --global` — writes to `~/.ax/config.toml`
2. Every `ax` invocation from anywhere on the machine uses that identity (unless overridden by a project-local `.ax/` higher in the walk)

## The class-'u' vs class-'a' PAT wrinkle

We discovered tonight (2026-04-07) that `/credentials/agent-pat` — the API endpoint for minting agent-bound PATs — **always produces a class-'a' (`axp_a_*`) PAT** per `ax-backend/app/core/credential_service.py:233-239`. Class-'a' PATs can **only** exchange for `agent_access` JWTs per the matrix at `credential_service.py:47-50`:

```python
PAT_CLASS_EXCHANGE_MATRIX = {
    "u": {"user_access", "user_admin"},    # User bootstrap PAT
    "a": {"agent_access"},                  # Agent-bound PAT
}
```

The channel MCP server at `channel/server.ts:89-107` hardcodes `requested_token_class: "user_access"`. A freshly-minted class-'a' PAT **will not work** with the channel server today — the exchange returns 422 `class_not_allowed`.

The existing sentinel tokens (`relay_token`, `backend_sentinel_token`, etc.) are all class-`'u'` (`axp_u_*`) PATs — legacy, minted through a different code path before the class-'a' logic was added. They work with the channel server because they exchange for `user_access`.

**Two paths forward, to be decided in follow-up work:**

**Path A — Patch the channel server to detect the PAT class and request the right token class.**
```typescript
async function exchangeForJWT(pat: string): Promise<string> {
  const isAgentClass = pat.startsWith("axp_a_");
  const tokenClass = isAgentClass ? "agent_access" : "user_access";
  const body: Record<string, unknown> = {
    requested_token_class: tokenClass,
    scope: "messages tasks context agents spaces",
  };
  if (isAgentClass) {
    body.agent_id = AGENT_ID;  // required for agent_access exchange
  }
  // ... rest unchanged
}
```
This is the architecturally correct fix. Allows new agents to use properly-scoped class-'a' PATs.

**Path B — Keep minting class-'u' PATs for the sentinels and document class-'a' as a future feature.**
Requires either a new `/credentials/user-scoped-pat` endpoint (that produces class-'u' with `bound_agent_id` set) or an explicit override on the existing endpoint. Less invasive for now, but leaves the token model confused.

**This spec recommends Path A** as the follow-up work for AX-CONFIG-001 Phase 2, because it's the technically correct answer and unblocks the long-term "every new agent uses properly-scoped PATs" goal.

## Anti-patterns

### A.1 `.mcp.json` env-block duplication

Current `.mcp.json` files inline the same five env vars (`AX_TOKEN_FILE`, `AX_BASE_URL`, `AX_AGENT_NAME`, `AX_AGENT_ID`, `AX_SPACE_ID`) that are already in `.ax/config.toml`. This is the **main symptom** this spec is trying to eliminate. After §5.2 lands, `.mcp.json` should look like:

```json
{
  "mcpServers": {
    "ax-channel": {
      "command": "bun",
      "args": ["run", "--cwd", "/home/ax-agent/channel", "--shell=bun", "--silent", "start"]
    }
  }
}
```
No `env` block. The channel server reads from project-local `.ax/config.toml` via CWD walk.

### A.2 Scattered `~/.ax/*_token` files

`~/.ax/relay_token`, `~/.ax/backend_sentinel_token`, `~/.ax/mcp_sentinel_token`, `~/.ax/user_token`, etc. — each agent gets its own raw-token file in a shared home directory. This worked when agents were tightly coupled to a single machine, but it's fragile: file permissions have to be right, the file name has to match what the agent runtime is looking for, and rotating any one of them is manual.

**After this spec:** each agent's token lives in its own project-local `.ax/config.toml`, mode 0600. `~/.ax/*_token` becomes legacy / optional.

### A.3 Per-agent profile lock drift

`~/.ax-profiles/<profile>/profile.lock.env` files duplicate much of the config surface. They exist for a good reason (fingerprint-based tamper detection for the `ax-profile-run` wrapper), but their existence as a **separate** config file means any field in both places can drift.

**After this spec:** the profile lock should point at `TOKEN_FILE=<project>/.ax/config.toml` and extract the token field at wrapper invocation time (§5.4). The fingerprint still protects the config file.

### A.4 Hard-coded paths in agent code

Any agent that hard-codes `~/.ax/some_file` or `/etc/some/path` as the config source is creating a new anti-pattern. All config reads **MUST** go through the discovery rules in §3 so users can override with env vars and project-local files.

## Migration path

### Phase 1 — this spec

Document the design. No code changes. Merge to `dev/staging`, then `main`. Establishes the contract all future work refers to.

### Phase 2 — Channel MCP server TOML discovery

Implement §5.2: add a TOML loader to `channel/server.ts` with the CWD walk. `.mcp.json` env blocks become optional (they're still honored for explicit override, but default behavior reads from `.ax/config.toml`). Deprecate `~/.claude/channels/ax-channel/.env` with a one-release-cycle warning.

### Phase 3 — `ax-profile-run` TOML support

Implement §5.4: teach `ax-profile-run` to extract `token = "..."` from a `.toml` `TOKEN_FILE`. Backward compatible. Enables the full "one file" promise.

### Phase 4 — class-'a' PAT path (channel server exchange fix)

Implement the channel server patch from §7 Path A. Detect prefix, request the right token class, pass `agent_id` for agent_access exchange. This unlocks the ability to use properly-scoped agent-bound PATs for new sentinels.

### Phase 5 — Remote MCP server config discovery

Implement §5.3 when the remote MCP server ships. Same discovery rules, same `.ax/config.toml` schema.

### Phase 6 — Migrate existing sentinels

For each existing sentinel (`relay`, `backend_sentinel`, `frontend_sentinel`, `mcp_sentinel`, etc.), write a project-local `.ax/config.toml` matching its current state. Remove its `.mcp.json` env block. Verify it still connects. Retire the corresponding `~/.ax/<name>_token` file if nothing else references it.

### Phase 7 — Documentation catch-up

Update `ax-cli/README.md`, `ax-cli/docs/agent-authentication.md`, and any other docs that still reference the old multi-file pattern. Document the new `.ax/config.toml` as the canonical connection model. Cross-reference this spec.

## Open questions

- **Reactions API for signals** — orthogonal to this spec, but related: if we add a reactions API (`👀`, `✅`, etc.), does the reaction-on-inbound-message pattern from AX-SIGNALS-001 Phase 4 interact with this config surface? (Probably not; reactions are per-message, not per-agent-config.)
- **Config file encryption at rest** — should `.ax/config.toml` be encrypted on disk using the OS keyring? Current design leaves it in plaintext with mode 0600 as the only protection, matching how SSH keys and AWS credentials work. Could add optional encryption later as a Phase 8+ enhancement.
- **Token rotation protocol** — should the CLI have an `ax auth rotate` command that mints a fresh PAT and updates the config file atomically? Would make rotation a one-command operation instead of manual.
- **Multi-identity in one directory** — what if a developer wants to test as two different agents from the same project directory? Current design requires separate dirs or env var overrides. Is that the right trade-off, or do we want a `profiles = [...]` section in the TOML?
- **Cross-machine config sharing** — containerized agents need a way to get the config file into the container. Bind mount? Secret injection? Env var injection? Probably out of scope for this spec but worth a note.

## Related

- `ax-cli/specs/AX-SIGNALS-001/spec.md` — agent status signals standard (separate concern, orthogonal design)
- `ax-cli/specs/AX-SCHEDULE-001/spec.md` — `ax schedule` feature (uses the same `.ax/` directory for local job state; inherits this config pattern)
- `ax-cli/ax_cli/config.py` — reference implementation of the discovery rules
- `ax-cli/channel/server.ts` — channel MCP server (needs §5.2 change)
- `ax-backend/app/core/credential_service.py:47-50` — PAT class exchange matrix (the class-'u' vs class-'a' constraint)
- `ax-backend/app/core/token_exchange.py` — exchange validation (reads the matrix)
- `ax-backend/docs/specs/AX-AGENT-MGMT-001/principal-model.md` — the underlying identity model this config surface expresses
- `/home/ax-agent/.ax/SWARM_TOKEN_WARNING.md` — swarm token usage rules (how to mint new PATs)

## If you are reading this spec

You are probably about to connect a new agent, or thinking about adding another config surface, or wondering why there are four different places config lives. Before you do anything:

1. **Read §2 and §3** — the schema and the discovery rules are the load-bearing contract
2. **Pick the right pattern from §6** — sentinel, concierge, headless, or human CLI. Each has a different recipe.
3. **If you need to add a NEW surface** — make it follow the discovery rules in §3. Don't invent another config path.
4. **If you hit the class-'a' PAT problem** — read §7 and pick Path A or Path B, then either patch the channel server or use a class-'u' PAT for now.

@madtank has had to re-explain this design intent many times. If you find yourself re-explaining it, strengthen this spec instead of nodding.
