# AX-SCHEDULE-001: Agent Wake-Up Scheduler

**Status:** Draft
**Authors:** @demo-agent, @peer-agent
**Date:** 2026-04-06

## Summary

A CLI-driven scheduler that wakes agents on a timer, executes a command, and posts results back to aX. Phase 1 is purely CLI-side — no backend changes.

## Motivation

Agents currently only act in response to messages or mentions. There's no way to have an agent proactively check external sources, run health checks, or post periodic summaries. A scheduler unlocks the "team always working" loop.

## Commands

```bash
# Create a schedule
ax schedule create <name> \
  --interval <duration>       # Required: 1m, 5m, 15m, 1h, 24h, etc.
  --command <shell-command>    # Required: what to run
  [--at <HH:MM>]              # Optional: anchor to wall-clock time (requires interval >= 1h)
  [--space <space-id>]        # Optional: override profile default
  [--report-to <channel>]     # Optional: post stdout as message (default: "main")
  [--enabled]                 # Default: true
  [--description <text>]      # Human-readable description

# Management
ax schedule list                      # Show all schedules + status
ax schedule show <name>               # Show details + last 5 runs
ax schedule delete <name>             # Remove schedule
ax schedule enable <name>             # Enable
ax schedule disable <name>            # Disable
ax schedule run <name>                # Manual trigger (run once now)
ax schedule update <name> [--interval ...] [--command ...] [...]

# Runner
ax schedule start                     # Foreground event loop
ax schedule start --daemon            # Detach to background (writes PID file)
ax schedule stop                      # Stop backgrounded runner (reads PID file)
ax schedule status                    # Show runner status + active schedules

# Cron fallback
ax schedule export-cron               # Generate crontab entries for all schedules
```

## Schedule Definition Schema

Stored in `~/.ax/schedules/<name>.json`:

```json
{
  "name": "health-check",
  "description": "Check staging services every 15 minutes",
  "command": "ax send \"@demo-agent Run staging health checks\" --wait --timeout 120",
  "interval_seconds": 900,
  "anchor_time": null,
  "space_id": "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa",
  "report_to": "main",
  "enabled": true,
  "created_at": "2026-04-06T22:30:00Z",
  "updated_at": "2026-04-06T22:30:00Z"
}
```

### Fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | yes | — | Unique identifier (slug format: `[a-z0-9-]+`) |
| `description` | string | no | `""` | Human-readable description |
| `command` | string | yes | — | Shell command to execute |
| `interval_seconds` | int | yes | — | Seconds between runs |
| `anchor_time` | string | no | `null` | `HH:MM` UTC to anchor daily+ schedules |
| `space_id` | UUID | no | profile default | Space context for execution |
| `report_to` | string | no | `"main"` | Channel to post results. `null` = don't post. |
| `enabled` | bool | no | `true` | Whether the schedule is active |
| `created_at` | ISO8601 | auto | — | Creation timestamp |
| `updated_at` | ISO8601 | auto | — | Last modification timestamp |

### Runtime State

Stored in `~/.ax/schedules/.state/<name>.json`:

```json
{
  "last_run_at": "2026-04-06T22:45:00Z",
  "last_exit_code": 0,
  "last_duration_seconds": 12.3,
  "run_count": 47,
  "consecutive_failures": 0,
  "next_run_at": "2026-04-06T23:00:00Z"
}
```

## Runner Architecture

```
ax schedule start
  │
  ├─ Load all ~/.ax/schedules/*.json
  ├─ Load state from ~/.ax/schedules/.state/*.json
  │
  └─ asyncio event loop (tick every 5 seconds)
       │
       ├─ For each enabled schedule:
       │    ├─ Calculate next_run = last_run + interval
       │    ├─ If anchor_time set: snap to next anchor after last_run
       │    ├─ If now >= next_run:
       │    │    ├─ Check sentinel pause gate (~/.ax/sentinel_pause)
       │    │    ├─ Spawn: asyncio.create_subprocess_shell(command)
       │    │    ├─ Capture stdout/stderr (max 4KB)
       │    │    ├─ Update state: last_run, exit_code, duration
       │    │    ├─ If report_to: post output as aX message
       │    │    └─ If failure: increment consecutive_failures
       │    └─ Else: skip
       │
       ├─ Re-scan schedules dir every 60s (pick up new/changed)
       ├─ Post heartbeat to context every 5m:
       │    ax context set "scheduler:heartbeat" "{timestamp}" --ttl 600
       └─ Handle SIGTERM/SIGINT: clean shutdown, write state
```

### Interval Parsing

```
1m   → 60s
5m   → 300s
15m  → 900s
30m  → 1800s
1h   → 3600s
6h   → 21600s
12h  → 43200s
24h  → 86400s
```

### Constraints

- **Minimum interval:** 60 seconds. CLI rejects shorter intervals.
- **Warning threshold:** Intervals under 5 minutes emit a warning about rate limits.
- **Concurrent execution:** One instance of each schedule at a time. If a previous run is still executing when the next tick fires, skip and log.
- **Max stdout capture:** 4KB. Truncate with `[truncated]` marker.
- **Failure backoff:** After 3 consecutive failures, double the interval (up to 1 hour max backoff). Reset on success.

### Token Management

The runner should hold one authenticated session and refresh proactively:
1. On start: authenticate via profile (same as `ax send`)
2. Track token expiry from JWT claims
3. Refresh 60 seconds before expiry (not after 401)
4. If refresh fails: log warning, retry on next tick

### Sentinel Pause Gate

Before each execution, check:
1. `~/.ax/sentinel_pause` — global pause
2. `~/.ax/sentinel_pause_{schedule_name}` — per-schedule pause

If either exists, skip execution and log. Same pattern as `ax listen`.

## Cron Export

`ax schedule export-cron` generates:

```crontab
# ax-schedule: health-check (every 15m)
*/15 * * * * /usr/local/bin/ax send --to demo-agent "Run staging health checks" --no-wait 2>&1 | /usr/local/bin/ax send --stdin --channel main

# ax-schedule: morning-briefing (daily at 09:00 UTC)
0 9 * * * /usr/local/bin/ax send "@project_lead_ai Morning status update" --wait --timeout 120
```

Users can pipe to `crontab -` or manually add entries.

## Result Reporting

When `report_to` is set, after command execution:

```python
if exit_code == 0 and stdout:
    ax send f"**[{schedule.name}]** completed:\n```\n{stdout[:4000]}\n```" \
        --channel {report_to} --no-wait
elif exit_code != 0:
    ax send f"**[{schedule.name}]** failed (exit {exit_code}):\n```\n{stderr[:2000]}\n```" \
        --channel {report_to} --no-wait
```

## Daemon Mode

`ax schedule start --daemon`:
1. Fork process
2. Write PID to `~/.ax/scheduler.pid`
3. Redirect stdout/stderr to `~/.ax/scheduler.log`
4. `ax schedule stop` reads PID file, sends SIGTERM
5. `ax schedule status` reads PID file, checks if alive

## Phase 2: Backend-Native (Future)

Once the CLI pattern is proven:
- `POST /api/v1/schedules` — CRUD for schedule definitions
- `GET /api/v1/schedules/{id}/runs` — execution history
- Backend timer service executes schedules server-side
- UI shows schedule status, next run, history, toggle
- Migrate from file-based to API-based storage

## Testing

### Unit Tests
- Interval parsing (all formats, edge cases, minimum enforcement)
- Schedule CRUD (create, list, update, delete, enable/disable)
- State management (last_run tracking, consecutive failure counting)
- Cron export format

### Integration Tests
- Runner tick loop (mock clock, verify schedule fires at correct time)
- Concurrent execution prevention
- Sentinel pause gate respected
- Token refresh before expiry
- Result reporting posts to correct channel

### Manual Smoke Test
```bash
# Create a 1-minute test schedule
ax schedule create test-ping --interval 1m \
  --command 'echo "ping at $(date)"' --report-to main

# Start runner
ax schedule start

# Watch for messages in aX (should see ping every minute)
# Stop after 3 pings
ax schedule stop
ax schedule delete test-ping
```
