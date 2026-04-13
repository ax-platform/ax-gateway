"""Tests for ax handoff composed workflow helpers."""

from typer.testing import CliRunner

from ax_cli.commands.handoff import _is_handoff_progress, _matches_handoff_progress, _matches_handoff_reply
from ax_cli.main import app


def test_handoff_matches_thread_reply_from_target_agent():
    message = {
        "id": "reply-1",
        "content": "Reviewed and done.",
        "parent_id": "sent-1",
        "display_name": "orion",
        "created_at": "2026-04-13T04:31:00+00:00",
    }

    assert _matches_handoff_reply(
        message,
        agent_name="orion",
        sent_message_id="sent-1",
        token="handoff:abc123",
        current_agent_name="ChatGPT",
        started_at=0,
        require_completion=False,
    )


def test_handoff_matches_fast_top_level_reply_with_token_and_mention():
    message = {
        "id": "reply-1",
        "content": "@ChatGPT handoff:abc123 reviewed the spec.",
        "conversation_id": "reply-1",
        "display_name": "orion",
        "created_at": "2026-04-13T04:31:00+00:00",
    }

    assert _matches_handoff_reply(
        message,
        agent_name="@orion",
        sent_message_id="sent-1",
        token="handoff:abc123",
        current_agent_name="ChatGPT",
        started_at=0,
        require_completion=True,
    )


def test_handoff_does_not_match_other_agent():
    message = {
        "id": "reply-1",
        "content": "@ChatGPT handoff:abc123 done.",
        "display_name": "cipher",
    }

    assert not _matches_handoff_reply(
        message,
        agent_name="orion",
        sent_message_id="sent-1",
        token="handoff:abc123",
        current_agent_name="ChatGPT",
        started_at=0,
        require_completion=False,
    )


def test_handoff_progress_does_not_count_as_reply():
    message = {
        "id": "reply-1",
        "content": "Working... (12 tools)\n  > checking repo\n  > running tests",
        "parent_id": "sent-1",
        "display_name": "mcp_sentinel",
        "metadata": {"streaming_reply": {"enabled": True, "final": False}},
    }

    assert _is_handoff_progress(message)
    assert _matches_handoff_progress(
        message,
        agent_name="mcp_sentinel",
        sent_message_id="sent-1",
        token="handoff:abc123",
        current_agent_name="ChatGPT",
        started_at=0,
        require_completion=False,
    )
    assert not _matches_handoff_reply(
        message,
        agent_name="mcp_sentinel",
        sent_message_id="sent-1",
        token="handoff:abc123",
        current_agent_name="ChatGPT",
        started_at=0,
        require_completion=False,
    )


def test_handoff_progress_can_change_without_matching_completion():
    message = {
        "id": "reply-2",
        "content": "Working... (41 tools)\n  > ax context load\n  > ax messages list",
        "conversation_id": "sent-1",
        "display_name": "mcp_sentinel",
    }

    assert _is_handoff_progress(message)
    assert _matches_handoff_progress(
        message,
        agent_name="mcp_sentinel",
        sent_message_id="sent-1",
        token="handoff:abc123",
        current_agent_name="ChatGPT",
        started_at=0,
        require_completion=False,
    )
    assert not _matches_handoff_reply(
        message,
        agent_name="mcp_sentinel",
        sent_message_id="sent-1",
        token="handoff:abc123",
        current_agent_name="ChatGPT",
        started_at=0,
        require_completion=True,
    )


def test_handoff_streaming_reply_with_token_counts_as_reply():
    message = {
        "id": "reply-3",
        "content": "Received this. `handoff:abc123` Current state: smoke check acknowledged.",
        "parent_id": "sent-1",
        "display_name": "mcp_sentinel",
        "metadata": {"streaming_reply": {"enabled": True, "final": False}},
    }

    assert _is_handoff_progress(message)
    assert _matches_handoff_reply(
        message,
        agent_name="mcp_sentinel",
        sent_message_id="sent-1",
        token="handoff:abc123",
        current_agent_name="ChatGPT",
        started_at=0,
        require_completion=True,
    )


def test_handoff_is_registered_and_old_tone_verbs_are_removed():
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "handoff" in result.output
    assert "ship" not in result.output
    assert "boss" not in result.output

    handoff_help = runner.invoke(app, ["handoff", "--help"])
    assert handoff_help.exit_code == 0
    assert "follow-up" in handoff_help.output

    old_command = runner.invoke(app, ["ship", "--help"])
    assert old_command.exit_code != 0
    assert "No such command" in old_command.output
