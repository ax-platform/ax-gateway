#!/usr/bin/env bash
# Smoke-test the dev aX space-agent read/navigation MCP app flow.
#
# This sends one user-authored QA prompt to @aX in dev Team Hub and validates
# the structured reply text. It intentionally does not create, update, or delete.
#
# Usage:
#   AX_PROFILE=dev-mcpjam-user ./examples/dev_ax_mcp_app_smoke.sh
#
# Optional env:
#   AX_PROFILE   axctl profile to load. Default: dev-mcpjam-user
#   AX_SPACE_ID  space to test. Default: Team Hub on dev
#   AX_SPACE_LABEL human-readable label for the prompt. Default: Team Hub
#   AX_TIMEOUT   seconds to wait for aX. Default: 120
#   AXCTL        axctl binary. Default: axctl
#   AX_PROFILE_CWD directory used to resolve the profile. Default: $HOME

set -euo pipefail

AX_PROFILE="${AX_PROFILE:-dev-mcpjam-user}"
AX_SPACE_ID="${AX_SPACE_ID:-da183d3f-77ae-4497-be93-b829562cf60a}"
AX_SPACE_LABEL="${AX_SPACE_LABEL:-Team Hub}"
AX_TIMEOUT="${AX_TIMEOUT:-120}"
AXCTL="${AXCTL:-axctl}"
AX_PROFILE_CWD="${AX_PROFILE_CWD:-$HOME}"
AX_EXPECTED_BASE_URL="${AX_EXPECTED_BASE_URL:-https://dev.paxai.app}"

PROMPT="@aX Dev UAT smoke: please run the MCP app read/navigation validation in ${AX_SPACE_LABEL}. Check: 1) identity/whoami resolves to aX as space agent, 2) tasks list opens, 3) task detail/read navigation works if available, 4) agents list opens, 5) context list opens, 6) search for \"task\" returns results. Do not create/update/delete. Reply with PASS/FAIL per item and exact errors."

if ! command -v "$AXCTL" >/dev/null 2>&1; then
  echo "axctl binary not found: $AXCTL" >&2
  exit 127
fi

echo "[dev-ax-smoke] loading axctl profile: $AX_PROFILE" >&2
profile_exports="$(cd "$AX_PROFILE_CWD" && "$AXCTL" profile env "$AX_PROFILE")"
eval "$profile_exports"
export AX_SPACE_ID

if [[ "${AX_BASE_URL:-}" != "$AX_EXPECTED_BASE_URL" ]]; then
  echo "[dev-ax-smoke] refusing to run against unexpected AX_BASE_URL=${AX_BASE_URL:-<unset>}" >&2
  echo "[dev-ax-smoke] expected AX_BASE_URL=$AX_EXPECTED_BASE_URL" >&2
  exit 2
fi

echo "[dev-ax-smoke] verifying identity in space: $AX_SPACE_ID" >&2
AX_SPACE_ID="$AX_SPACE_ID" "$AXCTL" auth whoami --json

reply_json="$(mktemp)"
trap 'rm -f "$reply_json"' EXIT

echo "[dev-ax-smoke] sending read-only smoke prompt to @aX" >&2
AX_SPACE_ID="$AX_SPACE_ID" "$AXCTL" send \
  --space-id "$AX_SPACE_ID" \
  --timeout "$AX_TIMEOUT" \
  --json \
  "$PROMPT" | tee "$reply_json"

python3 - "$reply_json" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
raw = path.read_text()
try:
    payload = json.loads(raw)
except json.JSONDecodeError as exc:
    decoder = json.JSONDecoder()
    payload = None
    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(raw[index:])
            break
        except json.JSONDecodeError:
            continue
    if payload is None:
        print(f"[dev-ax-smoke] invalid JSON from axctl send: {exc}", file=sys.stderr)
        sys.exit(1)

def walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_strings(item)

reply = payload.get("reply") if isinstance(payload, dict) else None
text = reply.get("content", "") if isinstance(reply, dict) else ""
if not text:
    text = "\n".join(walk_strings(payload))
checks = {
    "identity/whoami": r"(identity|whoami).*pass|pass.*(identity|whoami)",
    "tasks list": r"tasks?\s+list.*pass|pass.*tasks?\s+list",
    "task detail": r"task\s+detail.*pass|pass.*task\s+detail",
    "agents list": r"agents?\s+list.*pass|pass.*agents?\s+list",
    "context list": r"context\s+list.*pass|pass.*context\s+list",
    "search": r"search.*pass|pass.*search",
}

failures = []
if not re.search(r"\bPASS\b", text, flags=re.IGNORECASE):
    failures.append("reply did not include any PASS markers")
if re.search(r"\bFAIL\b", text, flags=re.IGNORECASE):
    failures.append("reply included a FAIL marker")

for name, pattern in checks.items():
    if not re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL):
        failures.append(f"missing PASS evidence for {name}")

if failures:
    print("[dev-ax-smoke] validation failed:", file=sys.stderr)
    for failure in failures:
        print(f"  - {failure}", file=sys.stderr)
    print("\n[dev-ax-smoke] reply text used for validation:\n", file=sys.stderr)
    print(text[-4000:], file=sys.stderr)
    sys.exit(1)

print("[dev-ax-smoke] validation passed: aX returned PASS evidence for all 6 read/navigation checks")
PY
