# Hermes Agent Sentinel — Vendored

This package ships the Hermes Agent CLI sentinel that the gateway's `hermes` template launches.

## Origin

Vendored from the live ax-agents host (`/home/ax-agent/agents/` on the EC2 production host) on 2026-04-25 per @madtank's directive — "we own both repositories... copy over the files on my local machine to this repository."

## What's here

| File | Source (live host) | Lines |
|---|---|---|
| `sentinel.py` | `claude_agent_v2.py` | 1641 |
| `runtimes/__init__.py` | `runtimes/__init__.py` | 142 |
| `runtimes/hermes_sdk.py` | `runtimes/hermes_sdk.py` | 474 |
| `runtimes/claude_cli.py` | `runtimes/claude_cli.py` | 178 |
| `runtimes/codex_cli.py` | `runtimes/codex_cli.py` | 155 |
| `runtimes/openai_sdk.py` | `runtimes/openai_sdk.py` | 502 |
| `tools/__init__.py` | `agents/tools/__init__.py` | 294 |

Each file carries a `# Vendored from ax-agents on 2026-04-25 — see ax_cli/runtimes/hermes/README.md` line at the top for attribution.

## Runtime support

The vendored `sentinel.py` (live version) supports four runtimes via `--runtime`:

- `hermes_sdk` — native Hermes Agent integration (the demo path)
- `openai_sdk` — OpenAI Python SDK via ChatGPT OAuth
- `claude_cli` — Claude Code subprocess (`claude -p`)
- `codex_cli` — Codex CLI subprocess (`codex exec`)

The earlier 1153-line copy of `claude_agent_v2.py` only supported `claude/codex/claude_cli/codex_cli/openai_sdk` — `hermes_sdk` was the gap. This vendored version closes it.

## Wiring

`ax_cli/commands/gateway.py` `_hermes_sentinel_script(entry)` should resolve to:
- `Path(__file__).parent.parent / "runtimes" / "hermes" / "sentinel.py"` (the bundled path), OR
- An operator override at `/home/ax-agent/agents/claude_agent_v2.py` if it exists (preserves the dev-fleet workflow on the EC2 production host).

The override-then-bundle order means the existing dev fleet keeps using the live host copy while fresh `pip install ax-cli` users get the bundled one transparently.

### `tools/` shim — important

The `_secure_hermes_tools` function in `runtimes/hermes_sdk.py` does TWO imports that resolve to **different `tools` packages on the live host**:

```python
from tools.registry import registry          # → public hermes-agent's tools/registry.py
from tools import _check_read_path, ...      # → vendored tools/__init__.py (this dir)
```

On the EC2 production host, this works because PYTHONPATH puts `/home/ax-agent/agents` first (loads `tools/__init__.py` from there) and the public hermes-agent clone second (provides `tools.registry` via Python's namespace fall-through).

**For a `pip install ax-cli` user** wanting to launch a hermes agent, the wiring needs to:

1. Prepend `Path(__file__).parent` (i.e. `ax_cli/runtimes/hermes/`) to `sys.path` BEFORE the public hermes-agent clone, so `import tools` resolves to the vendored `tools/__init__.py` shim.
2. Ensure the public hermes-agent clone is also on `sys.path` (operators set this via `HERMES_REPO_PATH` or default `~/hermes-agent`) so `tools.registry` resolves correctly.

`_hermes_sentinel_script` (the launcher) is the right place to set this up, since it constructs the subprocess env. The vendored `sentinel.py` does not need to be modified — the path setup happens at launch time.

### Why the shim isn't a separate import name

Renaming to e.g. `from ax_cli.runtimes.hermes.security import _check_read_path` would be cleaner, BUT it would diverge the vendored `runtimes/hermes_sdk.py` from the live host's copy. That breaks the "re-vendor as a clean copy" property. Keeping `from tools import ...` means the vendored runtime is byte-identical to live (modulo the attribution header), and the import resolution is a deployment concern, not a code change.

## Lint

Vendored files are excluded from `ruff` checks via `extend-exclude` in `pyproject.toml`. They follow the upstream ax-agents style (which differs from ax-cli's `select = ["E","F","W","I"]` profile). Updating the vendored files means re-vendoring from the live host — see "Re-vendoring" below.

## License

Both the `ax-agents` source and `ax-cli` destination are owned by aX Platform / @madtank. ax-cli is MIT (see `/LICENSE` at repo root). These vendored files inherit the ax-cli MIT license per @madtank's verbal license greenlight on 2026-04-25.

## Re-vendoring

When the live host's `claude_agent_v2.py` or `runtimes/` evolve, re-sync into this directory by running (on the EC2 host):

```bash
HEADER="# Vendored from ax-agents on $(date +%Y-%m-%d) — see ax_cli/runtimes/hermes/README.md"
SRC=/home/ax-agent/agents
DEST=/path/to/ax-cli/ax_cli/runtimes/hermes
{ echo "$HEADER"; cat "$SRC/claude_agent_v2.py"; } > "$DEST/sentinel.py"
for r in __init__ hermes_sdk claude_cli codex_cli openai_sdk; do
  { echo "$HEADER"; cat "$SRC/runtimes/$r.py"; } > "$DEST/runtimes/$r.py"
done
{ echo "$HEADER"; cat "$SRC/tools/__init__.py"; } > "$DEST/tools/__init__.py"
```

Then commit + PR. Update the line counts table in this README to reflect the new state.

## End-user setup (the only steps a fresh user has to run)

The vendored sentinel is bundled with ax-cli, but Hermes-the-agent's own runtime dependencies (openai SDK, anthropic SDK, etc.) live in the `NousResearch/hermes-agent` repo. Set those up once:

```bash
git clone https://github.com/NousResearch/hermes-agent ~/hermes-agent
cd ~/hermes-agent
python3 -m venv .venv
.venv/bin/pip install -e .
```

The gateway auto-detects `~/hermes-agent` (or `$HERMES_REPO_PATH`) and uses its `.venv/bin/python3` when launching the sentinel.

## Acceptance smoke (verified 2026-04-25 on macOS)

```bash
ax gateway agents add demo-hermes --template hermes --space-id <space>
# Wait ~10s for Hermes to load on first run.
curl -sS -X POST -d '{"content":"Reply with: Hermes online"}' \
  -H 'Content-Type: application/json' \
  http://127.0.0.1:8765/api/agents/demo-hermes/test
# Expected: hermes_sdk runtime invokes Codex backend, replies in ~5–20s
# Verified: 13 char reply in 17s round-trip via openai-codex@gpt-5.5
```

## Next steps

- [x] Rewire `_hermes_sentinel_script` — done in `ax_cli/commands/gateway.py` (override-then-bundle order).
- [ ] AUTOSETUP-001 spec update: hermes-agent clone is still needed for the runtime SDK install (above), but the sentinel itself is bundled. The "fix command" should reflect a `git clone + venv + pip install -e` step, not just `export HERMES_REPO_PATH`.
- [ ] Activity bubbles: gateway logs `Disabling agent_processing signals after 401 from /auth/internal/agent-status` — that's the GATEWAY-ACTIVITY-VISIBILITY-001 silent-swallow fix landing as designed (it's now visible).
- [ ] End-to-end CLI test on a fresh `pip install ax-cli` install path (Monday demo dry-run).
