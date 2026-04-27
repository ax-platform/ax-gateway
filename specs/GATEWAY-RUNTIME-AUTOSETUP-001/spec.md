# GATEWAY-RUNTIME-AUTOSETUP-001: Zero-Touch Runtime Setup

**Status:** v1 draft
**Owner:** @pulse, reviewer @orion
**Date:** 2026-04-25
**Source directives:**
- @madtank 2026-04-25: "we should make it real easy to do. We should just have it built into the repository… and if they enabled different agent versions, it's going to install the packages."
- @madtank: "We shouldn't make them have to do a bunch of set ups."
- @orion 2026-04-25: confirmed real repo at `https://github.com/NousResearch/hermes-agent`, recommends ax-cli `[hermes]` extras_require for opt-in install.

## Why this exists

Some runtimes (Hermes, anything that needs an external SDK) require an external checkout or pip install before they can start. Today the gateway raises a "Setup error" *after* the agent is registered, leaving the user with a half-broken agent and a manual fix. That's wrong for a CLI-driven, demo-grade product.

The contract: **picking a runtime in the wizard should make it work.** If something needs to be cloned or pip-installed, the gateway does it — visibly, with progress — before letting the agent register. No env var hunting.

## Scope

**In:**
- A pluggable preflight per template that knows: "this runtime needs X; here's how to obtain X; here's how to verify X."
- A `POST /api/templates/{id}/install` gateway endpoint that runs the preflight install (clone / pip install / verify).
- The Connect wizard surfaces an "Install <runtime>" button when preflight is not ready, replacing the env-var copy block.
- Streaming progress (clone %, pip install lines) back to the UI.
- After install: the wizard re-runs preflight, button becomes Connect.

**Out:**
- Backend changes (this is gateway-local install).
- Vendoring Hermes inside the ax-cli wheel (orion: "ax-cli[hermes] extras_require" is the right shape long-term).
- Runtimes that can't be installed without operator credentials (private repos, commercial licenses) — those keep the env-var setup path.

## Per-runtime contracts

### Hermes
- **Source:** `https://github.com/NousResearch/hermes-agent`
- **Install location:** `~/hermes-agent` (also honored: `HERMES_REPO_PATH` env var)
- **Steps:**
  1. `git clone https://github.com/NousResearch/hermes-agent ~/hermes-agent`
  2. `pip install -e ~/hermes-agent` *(if requirements.txt or pyproject exists)*
  3. **Provider auth** (mandatory — without this, Hermes runs but never calls tools):
     - Hermes maintains its own credential pool in `~/.hermes/auth.json`. Even when
       `~/.codex/auth.json` already exists from the Codex CLI, Hermes does NOT
       auto-import it on first run — its credential pool starts empty. `hermes auth
       status openai-codex` returns `logged out`, the runtime falls through every
       provider in its chain (`openrouter, nous, local/custom, openai-codex,
       api-key`), no provider has tool capability, and Hermes silently degrades to
       text-only replies. Symptom: sentinel log shows `done in Ns, 0 tools, K
       api_calls` for every message and the agent "lies" about tool use ("done"
       without ever running anything).
     - The wizard MUST detect `hermes auth status openai-codex` returning logged-out
       and treat the runtime as **not ready**, with `fix_steps` describing the
       provider-auth path (do NOT just call this "ready" because Hermes itself
       launched).
     - The fix is one of these (operator picks in the wizard):
       1. `hermes login --provider openai-codex` — interactive OAuth, registers
          the Codex provider in Hermes' pool. Reuses the same Codex account that
          backs `~/.codex/auth.json`.
       2. Set `OPENROUTER_API_KEY` in the sentinel's env — Hermes' provider chain
          tries openrouter first; tool-capable models (Claude, GPT-4) work
          immediately. Fastest path for demo machines that already have an
          OpenRouter key.
       3. `hermes auth add anthropic --type api-key` — direct Anthropic API key.
     - Verification: `hermes auth list` must show at least one credential for a
       tool-capable provider before the wizard advances to "Connect."
  4. Verify by re-running `hermes_setup_status({"template_id": "hermes"})` → must
     return `ready: true` AND `provider_ready: true` (new field — see preflight
     payload below).
- **Failure modes & UX:**
  - Network failure → toast "Couldn't reach github.com — check your network and retry."
  - Clone permission failure → toast "Can't write to ~/hermes-agent — check your filesystem permissions."
  - Install requirements failure → log full pip stderr, surface "Hermes installed but pip dependencies failed: <one-line>" with a "Show details" expand.
  - **Provider not authenticated** → wizard shows "Hermes installed, but no
    LLM provider is authenticated yet. Without one, Hermes can't call tools. Pick
    a provider:" with three buttons: "Sign in with Codex", "Use OpenRouter key",
    "Use Anthropic key". Surface the sentinel-log fingerprint of the broken-state
    (`0 tools, N api_calls`) so operators can recognize the symptom.

### Ollama
- **Source:** local `ollama serve` (assumed already installed; we don't bundle Ollama).
- **Install path:** check `OLLAMA_BASE_URL` reachability; if unreachable, surface "Start Ollama: `ollama serve`" with a copy button.
- **Models:** pre-pulled list comes from `ollama_setup_status()`. If recommended model not present, offer `ollama pull <model>` as a copyable command (we do NOT auto-pull — multi-GB downloads are operator decision).

### Echo
- No setup. Always ready.

## CLI parity (the primary path)

Every install that happens in the UI must be reproducible from CLI:

```bash
ax gateway runtime install hermes
# → progress lines, exit 0 on success
ax gateway runtime status hermes
# → ready: true | false, resolved_path, summary
```

(These commands need to be added — they don't exist today.)

## Current PR boundary

This spec is intentionally ahead of the current Gateway demo implementation.
The demo branch should not claim full zero-touch runtime setup until these
pieces land:

- `ax gateway runtime status/install` CLI commands;
- local API template preflight/install endpoints;
- explicit Hermes `install_ready` vs `provider_ready` fields;
- wizard controls for provider authentication.

For this PR, Hermes setup gaps should remain visible through Doctor/setup
errors rather than being described as automatically fixed.

## Security model

The install endpoint executes `git clone` and `pip install`. That is code execution. The following constraints are mandatory:

1. **Auth scope: gateway operator only.** `POST /api/templates/{id}/install` is reachable only via the local gateway HTTP server (already bound to `127.0.0.1`). Agent PATs MUST NOT be allowed to call it — gate by checking the gateway has an active operator session (`load_gateway_session()` returns a user-PAT-backed session). Agent-only callers receive 403.
2. **Vetted-source allowlist for clones.** The clone URL is **never** taken from the request body. It comes from a hardcoded per-template recipe in `gateway.py`. Today's allowlist:
   - `hermes` → `https://github.com/NousResearch/hermes-agent`
   No other URLs are clone-able through this endpoint. Future runtimes require a code change to extend the allowlist (PR-reviewable).
3. **User-writable target only.** Targets must be under `Path.home()` or under an explicit `--target` argument that is also under home. Never `/usr/local`, never `/opt`, never `sys.prefix`. The endpoint refuses any path outside the user's home tree.
   - **Symlink trap closed.** Resolve via `Path(target).resolve()` (follows symlinks) BEFORE the home-tree check, so `~/hermes-agent → /usr/local/...` does not slip through. Test: `ln -s /tmp/escape ~/hermes-agent && curl install` must fail with 400.
4. **No system-Python install.** `pip install` runs with `--user` flag, OR within a venv we create at `~/hermes-agent/.venv` (preferred). The system Python interpreter is never modified.
5. **Network failure is graceful.** Timeouts, permission failures, and network errors return structured errors — they never leave a half-extracted directory on disk. Cleanup on failure is part of the contract.
6. **No arbitrary command execution.** The endpoint takes a template id and zero other parameters that affect what runs. Anything else is a config option of the recipe (e.g. `--target` to override the target dir within the home-tree constraint).

## API surface (gateway local server)

```
GET  /api/templates                       # already exists; preflight fields exposed
GET  /api/templates/{id}/preflight        # NEW: { ready, summary, detail, fix_steps[] }
POST /api/templates/{id}/install          # NEW: streams progress (text/event-stream)
                                          #      returns final { ready: true } on done
                                          #      403 if no operator session
                                          #      400 if template id not on allowlist
```

`fix_steps[]` shape:
```json
[
  { "kind": "clone", "url": "https://github.com/NousResearch/hermes-agent", "target": "~/hermes-agent" },
  { "kind": "pip", "target": "~/hermes-agent" },
  { "kind": "provider_auth", "provider_options": ["openai-codex", "openrouter", "anthropic"] },
  { "kind": "verify" }
]
```

The `provider_auth` step is mandatory for Hermes — without it the runtime
launches but produces tool-less replies (silent demo killer). The wizard
surfaces this step distinctly because each option has different UX:
- `openai-codex` → triggers `hermes login --provider openai-codex` interactive
  OAuth (browser opens). Reuses the user's existing Codex account.
- `openrouter` → reads `OPENROUTER_API_KEY` from operator clipboard / form
  input, validates with a probe request, persists into the gateway's per-agent
  env so subsequent sentinel restarts pick it up.
- `anthropic` → similar to openrouter for an `ANTHROPIC_API_KEY`.

The preflight response distinguishes installation vs. provider readiness:
```json
{
  "ready": false,
  "install_ready": true,
  "provider_ready": false,
  "summary": "Hermes installed, but no LLM provider is authenticated.",
  "detail": "Without an authenticated provider Hermes can't call tools. Sign in with Codex (free, recommended) or paste an OpenRouter / Anthropic API key.",
  "fix_steps": [{"kind": "provider_auth", "provider_options": [...]}]
}
```

**SSE progress event shape** for the `POST .../install` stream:
```
data: {"phase":"clone","percent":42,"message":"Receiving objects: 42% (210/500)"}
data: {"phase":"pip","line":"Installing collected packages: hermes-agent"}
data: {"phase":"verify","ready":true}
data: {"phase":"done","ready":true,"resolved_path":"/Users/.../hermes-agent"}
```
One JSON object per `data:` line, terminated with the SSE `\n\n`. Phases: `clone | pip | verify | done | error`. The terminal event is always `done` or `error`. Clients close the stream on either.

## Acceptance smokes (CLI-driven)

```bash
# Hermes not yet installed
rm -rf ~/hermes-agent
ax gateway runtime status hermes              # ready: false
ax gateway runtime install hermes             # streams progress, exits 0
ax gateway runtime status hermes              # ready: true

# After install, can add a hermes agent without manual env vars
ax gateway agents add demo-hermes --template hermes
curl -sS http://127.0.0.1:8765/api/agents/demo-hermes | jq '.agent.last_error'
# expect: null  (no setup_blocked error)

# Cleanup
ax gateway agents remove demo-hermes
```

## Decisions

- **Install requires explicit user click.** Picking a runtime in the wizard does NOT auto-trigger install. The "Install <runtime>" button is the only path. Reason: a network operation that may pull hundreds of MB requires explicit consent; auto-install on selection is a UX trap. (Locked — do not flip.)
- **Allowlist eligibility:** public Apache/MIT/BSD repos are candidates. Anything requiring credentials (private GitHub, paid registries) stays out of the install endpoint and uses env-var/manual setup.

## Open questions

- For air-gapped environments: a `--local <path>` flag on `runtime install` to register an existing local checkout without clone. Spec'd but not yet implemented.
