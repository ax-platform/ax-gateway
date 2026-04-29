"""Helpers for explicit aX handle mentions in message content."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

MENTION_RE = re.compile(r"(?<![\w/])@([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?![\w-])")


def extract_explicit_mentions(content: str, *, exclude: Iterable[str] = ()) -> list[str]:
    """Return unique explicit @handles from user-visible message content."""
    excluded = {item.lower().lstrip("@").strip() for item in exclude if item}
    seen: set[str] = set()
    mentions: list[str] = []
    for match in MENTION_RE.finditer(content or ""):
        handle = match.group(1).strip()
        key = handle.lower()
        if not handle or key in excluded or key in seen:
            continue
        seen.add(key)
        mentions.append(handle)
    return mentions


def merge_explicit_mentions_metadata(
    metadata: dict[str, Any] | None,
    content: str,
    *,
    exclude: Iterable[str] = (),
) -> dict[str, Any] | None:
    """Merge explicit @mentions from content into message metadata.

    The backend remains the enforcement point for whether each handle can be
    routed. This helper preserves the client-side intent for replies, where the
    server may otherwise only route to the parent thread.
    """
    mentions = extract_explicit_mentions(content, exclude=exclude)
    if not mentions:
        return metadata

    merged = dict(metadata or {})
    existing_raw = merged.get("mentions") if isinstance(merged.get("mentions"), list) else []
    existing: list[Any] = list(existing_raw)
    existing_keys: set[str] = set()
    for item in existing:
        if isinstance(item, dict):
            raw = item.get("agent_name") or item.get("handle") or item.get("name") or ""
        else:
            raw = item
        key = str(raw).lower().lstrip("@").strip()
        if key:
            existing_keys.add(key)
    for mention in mentions:
        if mention.lower() not in existing_keys:
            existing.append(mention)
            existing_keys.add(mention.lower())
    merged["mentions"] = existing
    return merged
