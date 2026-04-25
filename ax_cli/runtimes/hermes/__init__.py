"""Vendored Hermes Agent sentinel runtime for the gateway.

Origin: madtank's local copy of `ax-agents-extract/cli_agents/` —
copied verbatim per @madtank 2026-04-25 directive ("we own both
repositories... copy over the files on my local machine to this
repository").

Layout:
    sentinel.py             - claude_agent_v2.py runner (entrypoint)
    runtimes/__init__.py    - runtime registry + BaseRuntime
    runtimes/openai_sdk.py  - OpenAI SDK runtime
    runtimes/hermes_sdk.py  - PENDING (lives on EC2 only — see README.md)

Launching:
    python -m ax_cli.runtimes.hermes.sentinel --runtime <name>

Gateway integration:
    `ax gateway agents add … --template hermes` should resolve the sentinel
    script via `_hermes_sentinel_script(entry)` to this package's `sentinel.py`.
"""
