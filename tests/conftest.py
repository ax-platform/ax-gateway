"""Shared test fixtures for ax-cli tests."""
import json

import pytest


def make_sse_lines(events: list[tuple[str, dict | str]]) -> list[str]:
    """Build raw SSE text lines from a list of (event_type, data) tuples."""
    lines = []
    for event_type, data in events:
        lines.append(f"event: {event_type}")
        if isinstance(data, dict):
            lines.append(f"data: {json.dumps(data)}")
        else:
            lines.append(f"data: {data}")
        lines.append("")
    return lines


@pytest.fixture
def sse_lines():
    """Factory fixture for building SSE line sequences."""
    return make_sse_lines
