"""gateway.log lines must carry an ISO-8601 UTC timestamp matching activity.jsonl's `ts` shape.

Without timestamps, daemon-side forensics requires guessing causal order
across `gateway.log` and `activity.jsonl`. The two should be eyeball-correlatable
by their leading column.

Format target: `2026-05-02T01:30:00.123456+00:00 taskforge_backend: started hermes_sentinel pid=12661`
"""

import re
from datetime import datetime, timedelta, timezone
from io import StringIO

from rich.console import Console

from ax_cli.gateway import _format_daemon_log_line

_ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+\+00:00$")


def test_format_prepends_iso8601_utc_timestamp_to_message():
    line = _format_daemon_log_line("taskforge_backend: started hermes_sentinel pid=12661")
    ts, _, body = line.partition(" ")
    assert _ISO_UTC_RE.match(ts), f"timestamp shape wrong: {ts!r}"
    assert body == "taskforge_backend: started hermes_sentinel pid=12661"


def test_format_emits_utc_within_one_second_of_now():
    before = datetime.now(timezone.utc)
    line = _format_daemon_log_line("anything")
    after = datetime.now(timezone.utc)
    ts_str, _, _ = line.partition(" ")
    ts = datetime.fromisoformat(ts_str)
    assert ts.tzinfo is not None
    assert ts.utcoffset() == timedelta(0), "must be UTC, not local time"
    # Inclusive bounds — the format call is sandwiched between before/after
    assert before <= ts <= after


def test_format_matches_activity_jsonl_ts_shape():
    """If a future change drifts the format away from activity.jsonl's `ts`,
    correlation breaks. Pin both shapes to the same regex."""
    line = _format_daemon_log_line("x")
    daemon_ts, _, _ = line.partition(" ")
    # activity.jsonl ts samples (from the live log):
    activity_samples = [
        "2026-05-02T00:41:09.225408+00:00",
        "2026-05-02T00:41:10.230990+00:00",
        "2026-05-02T01:12:57.246824+00:00",
    ]
    for sample in activity_samples:
        assert _ISO_UTC_RE.match(sample), f"sample broke: {sample!r}"
    assert _ISO_UTC_RE.match(daemon_ts)


def test_format_passes_through_message_with_rich_markup_chars():
    """Don't accidentally interpret/strip Rich markup inside the message body."""
    msg = "agent_with_brackets: [info] running [pid=42]"
    line = _format_daemon_log_line(msg)
    assert line.endswith(msg)


def test_format_preserves_trailing_whitespace_in_message():
    msg = "padded message   "
    line = _format_daemon_log_line(msg)
    assert line.endswith(msg)


def test_format_handles_empty_message():
    line = _format_daemon_log_line("")
    ts, _, body = line.partition(" ")
    assert _ISO_UTC_RE.match(ts)
    assert body == ""


# --- end-to-end: the daemon's emit callable should write timestamped lines ----


def test_emit_daemon_log_writes_timestamped_line_to_console():
    """`_emit_daemon_log(msg)` is what GatewayDaemon's logger callable invokes.
    Confirm the rendered output (no Rich markup, no color, daemon-style) is
    a single line `<iso-ts> <msg>` so a tail of gateway.log gives clean text."""
    from ax_cli.commands.gateway import _emit_daemon_log

    buf = StringIO()
    capture = Console(
        file=buf,
        force_terminal=False,
        no_color=True,
        soft_wrap=True,
        width=240,
    )
    import ax_cli.commands.gateway as gateway_cmd

    original_console = gateway_cmd.err_console
    gateway_cmd.err_console = capture
    try:
        _emit_daemon_log("taskforge_backend: started hermes_sentinel pid=12661")
    finally:
        gateway_cmd.err_console = original_console

    output = buf.getvalue().strip()
    ts, _, body = output.partition(" ")
    assert _ISO_UTC_RE.match(ts), f"timestamp missing or malformed: {output!r}"
    assert body == "taskforge_backend: started hermes_sentinel pid=12661"
