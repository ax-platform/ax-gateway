"""Tests for ``ax send`` post-send delivery_context rendering.

AVAIL-CONTRACT-001 §Post-send UX: when the send response carries
``delivery_context`` in its metadata, the CLI surfaces:
- ``delivery_path`` (live_session / warm_wake / inbox_queue / blocked_unroutable / failed_no_route)
- Disagreement signal when ``delivery_path != expected_response_at_send``
- ``warning`` (target_offline / target_stuck / target_quarantined / low_confidence)
"""

from __future__ import annotations

from ax_cli.commands.messages import (
    _delivery_context_chip,
    _delivery_matches_expectation,
    _extract_delivery_context,
)


def test_extract_finds_top_level_delivery_context():
    data = {"delivery_context": {"delivery_path": "live_session"}}
    assert _extract_delivery_context(data) == {"delivery_path": "live_session"}


def test_extract_finds_metadata_nested_delivery_context():
    data = {"metadata": {"delivery_context": {"delivery_path": "warm_wake"}}}
    assert _extract_delivery_context(data)["delivery_path"] == "warm_wake"


def test_extract_finds_message_metadata_nested_delivery_context():
    data = {"message": {"metadata": {"delivery_context": {"delivery_path": "inbox_queue"}}}}
    assert _extract_delivery_context(data)["delivery_path"] == "inbox_queue"


def test_extract_returns_none_when_absent():
    assert _extract_delivery_context({"id": "msg-1"}) is None
    assert _extract_delivery_context(None) is None
    assert _extract_delivery_context({"delivery_context": "not a dict"}) is None


def test_chip_renders_delivery_path_label():
    chip = _delivery_context_chip({"delivery_path": "live_session"})
    assert chip is not None
    assert "delivered live" in chip


def test_chip_renders_warm_wake_as_warming():
    chip = _delivery_context_chip({"delivery_path": "warm_wake"})
    assert "warming target" in chip


def test_chip_renders_blocked_path_with_warning():
    chip = _delivery_context_chip({
        "delivery_path": "blocked_unroutable",
        "warning": "target_quarantined",
    })
    assert "blocked (unroutable)" in chip
    assert "warning: target_quarantined" in chip


def test_chip_renders_disagreement_signal():
    """Predicted vs actual mismatch is surfaced explicitly (debugging gold)."""
    chip = _delivery_context_chip({
        "expected_response_at_send": "warming",
        "delivery_path": "live_session",
    })
    assert chip is not None
    assert "predicted Warming" in chip
    assert "actually delivered live" in chip


def test_chip_no_disagreement_when_paths_align():
    """When delivery_path matches expected_response_at_send, no predicted/actually banner."""
    chip = _delivery_context_chip({
        "expected_response_at_send": "immediate",
        "delivery_path": "live_session",
    })
    assert "predicted" not in chip
    assert "delivered live" in chip


def test_chip_renders_expected_alone_when_no_actual_path():
    """If only expected_response is present (offline send?), show the prediction."""
    chip = _delivery_context_chip({"expected_response_at_send": "queued"})
    assert chip is not None
    assert "Queued" in chip


def test_chip_returns_none_for_empty_context():
    assert _delivery_context_chip({}) is None
    assert _delivery_context_chip(None) is None


def test_chip_renders_dispatch_delayed_label():
    """The new v4 dispatch_delayed value renders distinctly from warming."""
    chip = _delivery_context_chip({
        "expected_response_at_send": "dispatch_delayed",
        "delivery_path": "warm_wake",
    })
    # dispatch_delayed maps to warm_wake → no disagreement
    assert "predicted" not in chip
    assert "warming target" in chip


def test_matches_expectation_table():
    """The expectation/path mapping covers AVAIL-CONTRACT v4 vocabulary."""
    # Direct matches
    assert _delivery_matches_expectation("live_session", "immediate")
    assert _delivery_matches_expectation("warm_wake", "warming")
    assert _delivery_matches_expectation("warm_wake", "dispatch_delayed")
    assert _delivery_matches_expectation("inbox_queue", "queued")
    assert _delivery_matches_expectation("blocked_unroutable", "unavailable")
    # Mismatches
    assert not _delivery_matches_expectation("live_session", "warming")
    assert not _delivery_matches_expectation("inbox_queue", "immediate")
    # Unknown is permissive (any path is acceptable)
    assert _delivery_matches_expectation("live_session", "unknown")
    assert _delivery_matches_expectation("blocked_unroutable", "unknown")


def test_chip_handles_unknown_enum_values_gracefully():
    """Unknown delivery_path / expected values pass through as raw strings, no crash."""
    chip = _delivery_context_chip({
        "delivery_path": "future_path_xyz",
        "expected_response_at_send": "future_value",
    })
    assert chip is not None
    assert "future_path_xyz" in chip
