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
  3. Verify by re-running `hermes_setup_status({"template_id": "hermes"})` → must return `ready: true`
- **Failure modes & UX:**
  - Network failure → toast "Couldn't reach github.com — check your network and retry."
  - Clone permission failure → toast "Can't write to ~/hermes-agent — check your filesystem permissions."
  - Install requirements failure → log full pip stderr, surface "Hermes installed but pip dependencies failed: <one-line>" with a "Show details" expand.

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

## Security model

The install endpoint executes `git clone` and `pip install`. That is code execution. The following constraints are mandatory:

1. **Auth scope: gateway operator only.** `POST /api/templates/{id}/install` is reachable only via the local gateway HTTP server (already bound to `127.0.0.1`). Agent PATs MUST NOT be allowed to call it — gate by checking the gateway has an active operator session (`load_gateway_session()` returns a user-PAT-backed session). Agent-only callers receive 403.
2. **Vetted-source allowlist for clones.** The clone URL is **never** taken from the request body. It comes from a hardcoded per-template recipe in `gateway.py`. Today's allowlist:
   - `hermes` → `https://github.com/NousResearch/hermes-agent`
   No other URLs are clone-able through this endpoint. Future runtimes require a code change to extend the allowlist (PR-reviewable).
3. **User-writable target only.** Targets must be under `Path.home()` or under an explicit `--target` argument that is also under home. Never `/usr/local`, never `/opt`, never `sys.prefix`. The endpoint refuses any path outside the user's home tree.
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
  { "kind": "verify" }
]
```

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

## Open questions

- Should `runtime install` happen automatically the first time someone picks a runtime in the wizard, or always require the explicit Install button click? Default: explicit (user consent for network operations).
- Where do we draw the line for what gateway will install? Public Apache/MIT repos = yes. Anything requiring credentials = no (those stay env-var setup).
- For air-gapped environments: a `--local <path>` flag on `runtime install` to register an existing local checkout without clone.
