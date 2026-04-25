# Hermes Agent Sentinel — Vendored

This package ships the Hermes Agent CLI sentinel that the gateway's `hermes` template launches.

## Origin

Copied verbatim from `~/claude_home/ax-agents-extract/cli_agents/` on 2026-04-25 per @madtank's directive — "we own both repositories... copy over the files on my local machine to this repository."

## What's here

| File | Source | Status |
|---|---|---|
| `sentinel.py` | `claude_agent_v2.py` (1153-line local copy) | vendored |
| `runtimes/__init__.py` | `runtimes/__init__.py` | vendored |
| `runtimes/openai_sdk.py` | `runtimes/openai_sdk.py` | vendored |
| `runtimes/hermes_sdk.py` | (lives on EC2 production host only) | **MISSING** |
| `runtimes/claude_cli.py` | (EC2 only) | missing |
| `runtimes/codex_cli.py` | (EC2 only) | missing |

## Monday demo gap

The gateway's hermes template launches `sentinel.py --runtime hermes_sdk`. **The local `claude_agent_v2.py` doesn't include `hermes_sdk` in its `--runtime` choices** (only `claude/codex/claude_cli/codex_cli/openai_sdk`). The EC2 host has a newer version (1641 lines) that includes it, plus the actual `hermes_sdk.py` runtime module.

To close the gap before Monday's demo, one of:

1. **Pull from EC2** — copy the four files (`claude_agent_v2.py` newer version + `runtimes/{claude_cli,codex_cli,hermes_sdk}.py`) into this directory. Replace `sentinel.py` and add the missing runtimes.
2. **Use openai_sdk for the demo** — rewire the gateway template to launch `--runtime openai_sdk` instead of `hermes_sdk`. Requires an `OPENAI_API_KEY` env var. Demo says "Hermes via OpenAI" rather than "native Hermes."
3. **Use Hermes-agent's own `cli.py`** — the public NousResearch/hermes-agent's `cli.py -q "<prompt>"` mode could be a drop-in alternate sentinel. Requires gateway template changes.

## License

Both the `ax-agents` source and `ax-cli` destination are owned by aX Platform / madtank. ax-cli is MIT (see ax-cli LICENSE). These vendored files inherit the ax-cli MIT license per the verbal license greenlight on 2026-04-25.

## Next steps

- [ ] Decide gap-closing path (1/2/3 above)
- [ ] If (1): copy live host files, replace `sentinel.py`, add missing runtimes, update this README
- [ ] Rewire `_hermes_sentinel_script` in `ax_cli/commands/gateway.py` to default to `Path(__file__).parent / "sentinel.py"`
- [ ] End-to-end CLI test: `ax gateway agents add demo-hermes --template hermes`, send test, verify reply
