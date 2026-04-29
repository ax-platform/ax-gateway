# Login and Agent Token E2E Runbook

Use this runbook to test the user-login-to-agent-token flow without deleting
real `~/.ax` or agent worktree config.

## Goal

Prove that a new user can:

1. paste a user PAT into `axctl login`
2. see masked paste confirmation
3. store user login under an isolated config directory
4. mint an agent PAT without printing the raw token
5. verify the generated agent profile
6. run as that agent with an agent-bound token

## Clean-Room Shell

Use a temporary config directory and temporary working directory.

```bash
export AX_E2E_ROOT="$(mktemp -d /tmp/axctl-e2e.XXXXXX)"
export AX_CONFIG_DIR="$AX_E2E_ROOT/config"
mkdir -p "$AX_E2E_ROOT/work"
cd "$AX_E2E_ROOT/work"
```

Run the repo version of the CLI without depending on the installed package:

```bash
export AX_REPO=/home/ax-agent/shared/repos/ax-cli
axdev() {
  PYTHONPATH="$AX_REPO" python3 -c 'from ax_cli.main import main; main()' "$@"
}
```

## User Login

The user should run this command and paste the user PAT directly into the hidden
prompt. Do not send the PAT through chat, tasks, context, or agent messages.

```bash
axdev login --url https://paxai.app
```

Expected:

```text
Paste your aX token. Input is hidden.
Token:
Token captured: axp_u_********

Connecting to https://paxai.app...
Token verified. Exchange successful.
Identity: madtank (...)

Saved user login: .../config/user.toml
```

## Mint Agent Credential

The setup agent may run this after the user login is complete.

```bash
axdev token mint orion-e2e \
  --create \
  --audience both \
  --expires 30 \
  --save-to "$AX_E2E_ROOT/agents/orion-e2e" \
  --profile prod-orion-e2e \
  --no-print-token
```

Expected:

- token file is created with mode `0600`
- `.ax/config.toml` is created under the agent directory
- profile `prod-orion-e2e` is created
- raw `axp_a_...` token is not printed

## Verify Agent Runtime

```bash
axdev profile verify prod-orion-e2e
eval "$(axdev profile env prod-orion-e2e)"
axdev auth whoami --json
```

Expected:

- `whoami` uses an agent-bound profile
- `bound_agent.agent_name` or resolved agent metadata points at `orion-e2e`
- runtime commands use the agent PAT/JWT, not the user PAT

## Cleanup

```bash
rm -rf "$AX_E2E_ROOT"
unset AX_CONFIG_DIR AX_E2E_ROOT AX_REPO
unset -f axdev 2>/dev/null || true
```

## Notes

- This runbook intentionally avoids deleting real `~/.ax` config.
- If `whoami` resolves an existing agent, inspect active profile and local
  `.ax/config.toml`; that is runtime config precedence, not user-login storage.
- Future device trust should replace the reusable user PAT with device-bound
  setup credentials, but this runbook validates the current compatibility path.
