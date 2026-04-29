# Vendored from ax-agents on 2026-04-25 — see ax_cli/runtimes/hermes/README.md
"""Agent runtime plugins.

Each runtime implements BaseRuntime and provides a way to execute agent work.
The SSE listener is runtime-agnostic — it detects mentions, queues them, and
delegates execution to whichever runtime is configured.

Available runtimes:
  - claude_cli:  Claude Code subprocess (claude -p), uses Max subscription
  - openai_sdk:  OpenAI Python SDK via ChatGPT OAuth, uses Plus/Pro subscription
  - codex_cli:   Codex CLI subprocess (codex exec), uses ChatGPT subscription

Adding a new runtime:
  1. Create a module in runtimes/ (e.g., gemini_sdk.py)
  2. Implement a class that extends BaseRuntime
  3. Register it in REGISTRY below
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger("runtime")


@dataclass
class RuntimeResult:
    """Result from a runtime execution."""
    text: str
    session_id: str | None = None
    history: list[dict] | None = None
    tool_count: int = 0
    files_written: list[str] = field(default_factory=list)
    exit_reason: str = "done"  # done | crashed | timeout
    elapsed_seconds: int = 0


class StreamCallback:
    """Callbacks the runtime fires to stream progress back to the SSE listener.

    The listener provides a concrete implementation that creates/edits aX messages.
    Runtimes call these methods — they don't know about the aX API.
    """

    def on_text_delta(self, text: str) -> None:
        """Incremental text content arrived."""

    def on_text_complete(self, text: str) -> None:
        """Full text content replaced (not a delta)."""

    def on_tool_start(self, tool_name: str, summary: str) -> None:
        """A tool invocation started."""

    def on_tool_end(self, tool_name: str, summary: str) -> None:
        """A tool invocation completed."""

    def on_status(self, status: str) -> None:
        """Status update (thinking, searching, etc.)."""


class BaseRuntime(abc.ABC):
    """Abstract base for all agent runtimes.

    Each runtime knows how to take a prompt and produce a response,
    optionally using tools and streaming output.
    """

    name: str  # e.g. "claude_cli", "openai_sdk"

    @abc.abstractmethod
    def execute(
        self,
        message: str,
        *,
        workdir: str,
        model: str | None = None,
        system_prompt: str | None = None,
        session_id: str | None = None,
        stream_cb: StreamCallback | None = None,
        timeout: int = 300,
        extra_args: dict | None = None,
    ) -> RuntimeResult:
        """Execute an agent turn.

        Args:
            message: The user/mention prompt.
            workdir: Working directory for file operations.
            model: Model name/id (runtime-specific).
            system_prompt: System instructions for the agent.
            session_id: For session continuity (resume previous context).
            stream_cb: Callbacks for streaming progress.
            timeout: Max seconds of silence before killing.
            extra_args: Runtime-specific options.

        Returns:
            RuntimeResult with the response text and metadata.
        """
        ...


# ── Runtime registry ────────────────────────────────────────────────────────

REGISTRY: dict[str, type[BaseRuntime]] = {}


def register(name: str):
    """Decorator to register a runtime plugin."""
    def decorator(cls: type[BaseRuntime]):
        cls.name = name
        REGISTRY[name] = cls
        return cls
    return decorator


def _auto_discover():
    """Import all runtime modules so they self-register via @register."""
    import importlib
    import pkgutil
    pkg_path = __path__
    for info in pkgutil.iter_modules(pkg_path):
        if not info.name.startswith("_"):
            importlib.import_module(f"{__name__}.{info.name}")


def get_runtime(name: str) -> BaseRuntime:
    """Instantiate a runtime by name."""
    if not REGISTRY:
        _auto_discover()
    if name not in REGISTRY:
        available = ", ".join(sorted(REGISTRY.keys()))
        raise ValueError(f"Unknown runtime '{name}'. Available: {available}")
    return REGISTRY[name]()


def list_runtimes() -> list[str]:
    """List registered runtime names."""
    return sorted(REGISTRY.keys())
