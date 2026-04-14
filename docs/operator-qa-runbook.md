# Operator QA Runbook

This is the canonical operator path for `axctl` QA, MCP Jam, widget,
Playwright, and release-facing validation.

The rule is:

```text
doctor -> preflight -> matrix -> MCP/widget/UI/release work
```

Do not start MCP Jam, widget, Playwright, or promotion debugging until the
API/CLI contract is proven first.

## What Each Step Answers

| Step | Command | Answers | Calls API? |
| --- | --- | --- | --- |
| Identity/config explanation | `axctl auth doctor` | Who would this command run as, what config source wins, which host and space are selected, and why any local config was ignored? | No |
| Single-env gate | `axctl qa preflight` | Does this credential pass the required API contracts in this space for one target environment? | Yes |
| Cross-env drift check | `axctl qa matrix` | Do dev, next, prod, or customer envs resolve identity the same way and pass the same contract gate? | Yes |
| UI/MCP validation | MCP Jam, widget, Playwright | Does the MCP/tool/widget layer work after API and credential contracts are already proven? | Yes |

`doctor` is static. It explains what will happen. `preflight` and `matrix`
prove the runtime path by calling the API.

## JSON Envelope And Exit Codes

`axctl auth doctor`, `axctl qa preflight`, and `axctl qa matrix` use the same
top-level JSON envelope:

```json
{
  "version": 1,
  "ok": true,
  "skipped": false,
  "summary": {},
  "details": []
}
```

Legacy command-specific fields remain in the payload, but CI, MCP dashboards,
sentinel startup checks, and future UI panels should read the common envelope
first.

Exit codes:

- `0`: command ran and `ok` is true
- `2`: command ran and `ok` is false
- `3`: command skipped because required config was absent
- `1`: crash, unexpected exception, or command usage failure

## Safe Credential Rules

User setup credentials and agent runtime credentials must stay separate.

Safe:

- `axctl login` stores user setup credentials in `~/.ax/user.toml`.
- `axctl login --env dev --url https://dev.paxai.app` stores named user setup
  credentials in `~/.ax/users/dev/user.toml`.
- `--env <name>` selects that named user login and bypasses active agent
  profiles and project-local runtime config.
- Agent runtime uses an agent PAT profile or project-local config with
  `axp_a_...` and agent identity fields.
- User-authored QA, quick actions, context uploads, and user-requested checks
  may use a user login.

Unsafe:

- A local `.ax/config.toml` that combines a user PAT (`axp_u_...`) with
  `agent_id` or `agent_name`.
- A global `~/.ax/config.toml` containing reusable credentials instead of only
  defaults such as `base_url`.
- Using a user PAT to speak as an agent or to run headless agent runtime work.
- Assuming the active shell targets `next`, `dev`, or `prod` without checking
  `auth doctor` or `auth whoami`.

Warnings:

- `global_config_contains_credentials` means the global fallback has credential
  material. It is not always a hard failure, but it must be understood before a
  release or promotion.
- `unsafe_local_config_ignored` means `axctl` found a stale local user-token plus
  agent-identity mix and ignored it. This is protective, but the file should be
  cleaned up when practical so future operators do not inherit confusion.

## Dev Single-Env Gate

Use this before MCP Jam, widgets, or Playwright in dev:

```bash
axctl auth doctor \
  --env dev \
  --space-id <dev-space-id> \
  --json

axctl qa preflight \
  --env dev \
  --space-id <dev-space-id> \
  --for playwright \
  --artifact .ax/qa/dev-preflight.json \
  --json
```

Required success:

- `doctor.ok` is `true`.
- `effective.principal_intent` is expected for the test, usually `user` for
  viewer-private UI and quick-action checks.
- `effective.host` is `dev.paxai.app`.
- `effective.space_id` is the intended dev space.
- `preflight.ok` is `true`.

If preflight fails, fix API/auth/space routing before inspecting widgets.

## Next Single-Env Gate

Use this before debugging `next` UI behavior:

```bash
axctl auth doctor \
  --env next \
  --space-id <next-space-id> \
  --json

axctl qa preflight \
  --env next \
  --space-id <next-space-id> \
  --for playwright \
  --artifact .ax/qa/next-preflight.json \
  --json
```

If `next` is stored as the default user login, `axctl qa matrix --env next`
will resolve it through `user_login:default` and report `env: next` in the
matrix row.

## Promotion Drift Check

Use this before promoting dev/staging behavior or when comparing environments:

```bash
axctl qa matrix \
  --env dev \
  --env next \
  --space dev=<dev-space-id> \
  --space next=<next-space-id> \
  --for release \
  --artifact-dir .ax/qa/promotion \
  --json
```

For production comparison after a prod login exists:

```bash
axctl qa matrix \
  --env dev \
  --env next \
  --env prod \
  --space dev=<dev-space-id> \
  --space next=<next-space-id> \
  --space prod=<prod-space-id> \
  --for release \
  --artifact-dir .ax/qa/promotion \
  --json
```

Required success:

- Top-level `ok` is `true`.
- Every row has `doctor_ok: true`.
- Every row has `preflight_ok: true`.
- Every row shows the expected `principal_intent`, `auth_source`, `host`, and
  `space_id`.
- Any warnings are explained in the PR or release notes if they affect the
  operator environment.

## Release Checklist

Before a release or production-facing promotion:

- [ ] `uv run ruff check .`
- [ ] `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest`
- [ ] `python -m build && twine check dist/*` when package metadata or release
      behavior changed.
- [ ] `axctl auth doctor --env dev --space-id <dev-space-id> --json`
- [ ] `axctl qa preflight --env dev --space-id <dev-space-id> --for release --artifact .ax/qa/dev-preflight.json --json`
- [ ] `axctl qa matrix --env dev --env next --space dev=<dev-space-id> --space next=<next-space-id> --for release --artifact-dir .ax/qa/promotion --json`
- [ ] Attach or summarize the preflight/matrix artifacts in the promotion PR.
- [ ] Only then run MCP Jam, widget, Playwright, or manual UI QA.

## GitHub Actions Enforcement

The reusable workflow is:

```yaml
uses: ./.github/workflows/operator-qa.yml
with:
  envs: dev,next
  target: release
  require_matrix: false
secrets: inherit
```

Configure repository or environment variables:

- `AX_QA_DEV_BASE_URL`
- `AX_QA_DEV_SPACE_ID`
- `AX_QA_NEXT_BASE_URL`
- `AX_QA_NEXT_SPACE_ID`
- `AX_QA_PROD_BASE_URL`
- `AX_QA_PROD_SPACE_ID`

Configure matching secrets:

- `AX_QA_DEV_TOKEN`
- `AX_QA_NEXT_TOKEN`
- `AX_QA_PROD_TOKEN`

The workflow writes temporary user-login configs under a job-local
`AX_CONFIG_DIR`, runs `auth doctor`, runs `qa preflight`, then runs `qa matrix`.
It uploads all artifacts, including:

- `<env>-doctor.json`
- `<env>-preflight.json`
- `matrix/matrix.json`
- `operator-qa-summary.json`

If no complete `TOKEN`, `BASE_URL`, and `SPACE_ID` triple exists for any
requested environment, the workflow skips safely by default. Set
`require_matrix: true` for workflows that should fail closed when no QA
environment is configured.

Promotion PRs to `main` run this through CI with `require_matrix: false`. That
keeps normal repos safe when env config is absent, while still blocking the
promotion path whenever configured environments produce `matrix.ok = false`.
The underlying runner returns exit code `3` for no-config skips; the workflow
converts that to success only when `require_matrix` is false.

## Failure Triage

Use the first failing layer to decide where to work:

- `doctor.ok = false`: fix local config, env selection, profile selection, or
  credential separation.
- `preflight.ok = false`: fix API, auth exchange, space routing, or backend
  contract behavior.
- `matrix.ok = false`: compare row-level `host`, `space_id`, `auth_source`,
  and failed check names before promoting.
- CLI passes but MCP fails: inspect MCP tool routing, token exchange, and
  request headers.
- CLI and MCP pass but UI fails: inspect app panel boot, payload replay, frame
  bridge, or frontend rendering.

## MCP App Signal Smoke

After `doctor` and `preflight` pass, `axctl apps signal` can create a durable
folded app card that opens an MCP app panel from the transcript. This is useful
for widget smoke tests because it exercises the same API-backed metadata path
used by agent-authored tool results.

```bash
axctl auth whoami --json

axctl apps signal context \
  --context-key '<context-key>' \
  --title 'Context smoke' \
  --summary 'Open this in the Context Explorer panel.' \
  --to <handle> \
  --json
```

Required success:

- `display_name` in the returned message is the intended user or agent.
- `metadata.ui.widget.resource_uri` is the expected `ui://...` app resource.
- The transcript shows a folded signal card, not a full inline app.
- Clicking the card opens the app panel with viewer credentials.

If `display_name` is wrong, stop and fix profile/config resolution before
continuing. `axctl profile env <profile>` exits without exports when profile
verification fails, so a shell can silently keep its previous identity unless
`auth whoami` is checked.

## Sample Release Flow

```bash
git checkout dev/staging
git pull --ff-only

uv run ruff check .
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest

axctl auth doctor --env dev --space-id <dev-space-id> --json
axctl qa preflight --env dev --space-id <dev-space-id> --for release --artifact .ax/qa/dev-preflight.json --json
axctl qa matrix --env dev --env next --space dev=<dev-space-id> --space next=<next-space-id> --for release --artifact-dir .ax/qa/promotion --json

# Continue only if the commands above pass.
# Then run MCP Jam, widget, Playwright, or manual release QA.
```
