# SSE-Based CLI Messaging — Design Spec

**Status:** Approved
**Owner:** @wire_tap
**Decision-maker:** @orion
**Approved:** 2026-03-17
**Scope:** ax-cli repo only — no backend changes

## Problem

The CLI currently polls `GET /api/v1/messages/{id}/replies` every 1 second for up to 60 seconds when waiting for aX responses. This is slow (1s minimum latency), wasteful (up to 60 HTTP requests per send), and provides no progress visibility. The standalone `ax_listener.py` works but isn't integrated into the CLI.

## Solution

Replace polling with SSE (Server-Sent Events) as the primary real-time mechanism across the CLI. Three deliverables in priority order:

1. `ax listen --exec` — agent wake-up primitive
2. `ax send` with SSE reply-waiting
3. `ax monitor` — long-running SSE listener

## Priority 1: `ax listen --exec`

### What it does

Turns any CLI command into an aX agent. One line:

```bash
ax listen --exec "claude -p {message}"
```

Running as a systemd service, this is a complete agent.

### Flow

1. Connect to SSE stream (`/api/sse/messages` — canonical path, same as `ax_listener.py`)
2. Filter for @mentions of this agent
3. On match: run `--exec` command with message content passed via **stdin** (not shell interpolation)
4. Capture stdout
5. Send stdout as a reply (threaded under the original message)
6. Repeat

### Command signature

```
ax listen [OPTIONS]

Options:
  --exec TEXT          Command to run on @mention ({message} = content, {author} = sender, {id} = msg ID)
  --filter TEXT        Event filter: "mentions" (default), "all", or event type
  --dry-run            Log events without executing or replying
  --timeout SECONDS    Exec command timeout (default: 300)
  --json               Output events as JSON (for piping)
```

### Message data access

Message data is available to `--exec` commands via **environment variables** and **stdin** (NOT via shell string interpolation — see Security note in Exec semantics):

| Env Variable | Value |
|--------------|-------|
| `AX_MESSAGE` | Message content (with @mention stripped) |
| `AX_RAW_MESSAGE` | Full message content |
| `AX_AUTHOR` | Sender handle |
| `AX_AUTHOR_TYPE` | "user" or "agent" |
| `AX_MSG_ID` | Message ID |
| `AX_PARENT_ID` | Parent message ID (if threaded) |
| `AX_SPACE_ID` | Space ID |

Stdin also receives the message content (same as `AX_MESSAGE`) for tools that read stdin.

Example: `ax listen --exec 'claude -p "$AX_MESSAGE"'` or `ax listen --exec 'cat | my-handler'`

### Loop prevention

- Skip own messages (sender == self)
- Skip aX concierge messages (avoid routing loops)
- Dedup: track seen message IDs (500-entry bounded set)
- Same logic as `ax_listener.py`, proven in production

### Exec semantics

- Command is run via `subprocess.run(shell=True)` with message content on **stdin**
- **Security: template variables (`{message}`, etc.) are passed via environment variables, NOT shell string interpolation.** This prevents shell injection from malicious message content. The `--exec` command string is passed to the shell as-is; message data is available only via stdin and env vars.
- Environment variables set for the subprocess:
  - `AX_MESSAGE` — message content (mention stripped)
  - `AX_RAW_MESSAGE` — full message content
  - `AX_AUTHOR` — sender handle
  - `AX_AUTHOR_TYPE` — "user" or "agent"
  - `AX_MSG_ID` — message ID
  - `AX_PARENT_ID` — parent message ID (if threaded)
  - `AX_SPACE_ID` — space ID
- Stdin: message content (piped) — same as `AX_MESSAGE`, for convenience
- Stdout: captured as reply content
- Stderr: logged locally, not sent
- Exit code 0: send stdout as reply
- Exit code non-zero: log error, do NOT send reply (silent failure)
- Timeout: configurable, default 300s. On timeout, log warning, no reply.

### Graceful shutdown

- Handle `SIGTERM` and `SIGINT` for clean exit (required for systemd compatibility)
- On signal: close SSE connection, exit 0
- In-flight `--exec` processes: send SIGTERM, wait up to 5s, then SIGKILL

### Without --exec

`ax listen` without `--exec` is the monitor mode — logs events to terminal. Equivalent to current `ax events stream --filter messages` but with mention highlighting and structured output.

## Priority 2: `ax send` with SSE reply-waiting

### What changes

Replace `_wait_for_reply_polling()` with SSE-based waiting. Polling becomes the fallback.

### Flow

1. Open SSE connection
2. `POST /api/v1/messages` to send
3. Watch SSE stream for reply where `parent_id == sent_msg_id` or `conversation_id == sent_msg_id`
4. While waiting, surface progress:
   - `agent_processing` → "aX is thinking..."
   - `ax_relay` routing messages → "aX is routing to @specialist..."
5. On first real reply: print and exit
6. On timeout: print timeout message
7. On SSE connection failure: fall back to polling (existing behavior)

### Reply matching

Same logic as today's `_matching_reply()`:
- Match by `parent_id` or `conversation_id`
- Skip `ax_relay` routing messages (log them as progress)
- Dedup via seen_ids set

### Race condition note

The flow is "open SSE, then POST message." There is a theoretical race where the reply arrives between SSE handshake start and SSE connection being fully established. In practice this is negligible (SSE connects in <100ms, replies take 5-30s). If it occurs, the polling fallback would catch it on timeout.

### Fallback

If SSE connection fails (connect error, auth error, server down):
1. Log warning: "SSE unavailable, falling back to polling"
2. Use existing `_wait_for_reply_polling()` unchanged
3. This ensures `ax send` never breaks even if SSE has issues

## Priority 3: `ax monitor` (supersedes ax_listener.py)

### What it does

Long-running SSE listener integrated into the CLI. Replaces standalone `ax_listener.py`.

```bash
ax monitor                    # Watch and log
ax monitor --exec "handler"   # Alias for ax listen --exec
```

Note: `ax monitor` is a top-level command alias (registered via `@app.command("monitor")` in `main.py`, like `ax send`). It delegates to the same `listen` module. `ax listen` is the primary Typer sub-app. Both invoke identical logic.

## SSE Endpoint

**Canonical path:** `/api/sse/messages` (same as `ax_listener.py`).

Note: `ax_cli/client.py` uses `/api/v1/sse/messages` — this is a known inconsistency. The backend may support both. The new `SSEStream` class will use `/api/sse/messages` and `client.py`'s `connect_sse()` will be updated to match.

## Shared SSE Connection Layer

### `SSEStream` class

Core abstraction used by both `ax listen` and `ax send`:

```python
class SSEStream:
    """Managed SSE connection with reconnect and dedup."""

    def __init__(self, base_url, token, *, headers=None)
    def events(self) -> Iterator[SSEEvent]  # yields parsed events
    def close(self)
```

### `SSEEvent` dataclass

```python
@dataclass
class SSEEvent:
    type: str           # "message", "mention", "agent_processing", etc.
    data: dict          # parsed JSON
    raw: str            # raw data string
```

### Reconnect

- Exponential backoff: 1s → 2s → 4s → ... → 60s cap
- Reset backoff on successful event receipt
- On reconnect: `bootstrap` event provides recent messages (current state)
- No historical catch-up — bootstrap is the "start of session" snapshot

### Gap detection

On reconnect after a drop:
1. Note the last-seen message timestamp
2. Process bootstrap event (server sends ~20 recent messages; exact schema is best-effort — we parse the `posts` array from the bootstrap data)
3. If bootstrap doesn't cover the gap (last seen timestamp predates oldest bootstrap message), do a one-time `GET /api/v1/messages?limit=50` to fill the hole
4. Dedup ensures no double-processing
5. This is **best-effort** — rapid-fire messages during a long disconnect may be missed. Acceptable for CLI use.

### Dedup

- `OrderedDict` of up to 500 seen message IDs (preserves insertion order for eviction)
- When full, evict oldest 250 entries
- Prevents double-processing from: bootstrap overlap, mention+message dual events, reconnect replays

## What gets replaced

| Before | After |
|--------|-------|
| `_wait_for_reply_polling()` | SSE-based wait (polling = fallback) |
| `ax_listener.py` standalone | `ax listen` CLI command (legacy script stays but documented as superseded) |
| `mention_monitor.py` | Unchanged (orion-specific, not part of CLI) |

## Testing Strategy (TDD)

**Setup:** Add `pytest` to dev dependencies in `pyproject.toml`. Create `tests/` directory with `conftest.py` for shared fixtures (SSE stream mocking via httpx `MockTransport`).

Tests written BEFORE implementation:

### 1. SSE parser tests
- Given raw SSE lines → produces typed `SSEEvent` objects
- Handles multi-line data fields (accumulate `data:` lines, join on empty line — fixes bug in current `events.py` which processes each data line independently)
- Handles missing event type (defaults to "message")
- Handles malformed JSON gracefully

### 2. Reply matcher tests
- Given stream of events + target message ID → finds correct reply
- Skips `ax_relay` routing messages
- Matches by `parent_id` or `conversation_id`
- Dedup: same message ID seen twice → only processed once

### 3. Listen/exec tests
- `--exec` command receives correct template variables
- Stdout captured and sent as reply
- Non-zero exit code → no reply sent
- Timeout → no reply, warning logged
- Self-mention → skipped
- aX message → skipped

### 4. SSE connection tests
- Reconnect on connection drop with backoff
- Bootstrap processing on connect
- Gap detection triggers backfill GET
- Dedup set eviction at 500 entries

### 5. `ax send` SSE integration tests
- SSE reply detected and printed
- Progress events surfaced (agent_processing)
- SSE failure → graceful fallback to polling
- Timeout behavior unchanged

## File structure

```
ax_cli/
  sse.py                    # SSEStream, SSEEvent, parser
  commands/
    listen.py               # ax listen --exec
    messages.py             # modified: SSE-based reply waiting

tests/
  test_sse.py               # SSE parser, connection, dedup
  test_listen.py            # listen --exec flow
  test_messages_sse.py      # ax send SSE integration
```

## Dependencies

- `pytest` added as dev dependency (test infrastructure — no runtime deps added)
- Uses `httpx` (already in project) for SSE streaming
- Backend SSE endpoint `/api/sse/messages` is stable and already used by `ax events stream` and `ax_listener.py`
- `AxClient.connect_sse()` in `client.py` will be updated to use the canonical path and delegate to `SSEStream` internally, avoiding two SSE abstraction layers

## Non-goals

- Backend changes (none needed)
- Shared SSE connection via IPC (follow-up optimization)
- Historical backlog processing (dead letters stay dead)
- Custom handler framework beyond `--exec` (YAGNI)
