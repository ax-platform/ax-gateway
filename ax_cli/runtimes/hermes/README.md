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
```

Then commit + PR. Update the line counts table in this README to reflect the new state.

## Acceptance smoke

```bash
# After this branch merges and night_owl rewires _hermes_sentinel_script:
ax gateway agents add demo-hermes --template hermes
ax gateway agents test demo-hermes
# expect: agent launches via bundled sentinel.py, replies via Hermes
```

## Next steps

- [ ] Rewire `_hermes_sentinel_script` in `ax_cli/commands/gateway.py` to default to the bundled path with operator override (night_owl, post-merge).
- [ ] AUTOSETUP-001 spec update: `hermes` template no longer needs to clone NousResearch/hermes-agent for the sentinel — only for Hermes-the-agent's prompt assets if those are loaded at runtime. Verify with night_owl's round-trip CLI smoke.
- [ ] End-to-end CLI test on a fresh `pip install ax-cli` install path (Monday demo dry-run).
