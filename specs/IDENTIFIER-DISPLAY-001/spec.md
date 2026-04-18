# IDENTIFIER-DISPLAY-001: Human-Readable Identifier Display

**Status:** Draft  
**Owner:** @alex / @ChatGPT  
**Date:** 2026-04-13  
**Related:** AXCTL-BOOTSTRAP-001, AGENT-PAT-001, LISTENER-001

## Summary

`axctl` should present account, space, and agent identifiers in a way humans can
understand. UUIDs remain the backend authority, but ordinary CLI output and
config-facing guidance should prefer stable slugs, handles, and names when the
API provides them.

## Product Rule

Human-facing surfaces should show:

1. account slug or username
2. space slug or space name
3. agent handle or agent name
4. UUID only as a secondary machine identifier

Machine-facing surfaces should keep UUIDs:

- API requests
- `--json` output
- profile verification metadata
- debug output
- logs and audit trails

## Why

Raw UUIDs are hard to recognize, compare, and communicate. They are useful for
stable backend references, but they should not be the primary thing a user sees
when they are selecting an account, choosing a space, checking the current
context, or reading setup instructions.

## CLI Behavior

Default text output should prefer readable fields.

Examples:

```text
Account: alex
Space: team-hub
Agent: demo-agent
```

If the UUID is useful, show it as supporting detail:

```text
Space: team-hub (12d6eafd...)
```

`--json` output should include both when available:

```json
{
  "space_id": "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa",
  "space_slug": "team-hub",
  "space_name": "Team Hub"
}
```

## Config Behavior

Runtime config may keep UUIDs because backend calls require stable identifiers.
When supported by the API, config commands should also store readable companion
fields such as:

- `account_slug`
- `space_slug`
- `space_name`
- `agent_name`

Commands should not require users to memorize UUIDs if a slug or handle is
available.

## Fallback

If the API does not return a slug or name, CLI output may fall back to the UUID.
That fallback should be treated as a data contract gap to address upstream, not
as the preferred UX.

## Acceptance Criteria

- `axctl login` prints readable identity and space names when available.
- `axctl profile list` and `axctl profile verify` show readable names first.
- Commands that accept a space should prefer slug/name input when the API can
  resolve it.
- `--json` output preserves UUIDs for scripts.
- Docs and examples avoid raw UUIDs unless the UUID is specifically relevant.
