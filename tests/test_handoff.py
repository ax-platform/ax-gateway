"""Tests for ax handoff composed workflow helpers."""

from typer.testing import CliRunner

from ax_cli.commands.handoff import _matches_handoff_reply
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


def test_handoff_is_registered_and_old_tone_verbs_are_removed():
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "handoff" in result.output
    assert "ship" not in result.output
    assert "boss" not in result.output

    old_command = runner.invoke(app, ["ship", "--help"])
    assert old_command.exit_code != 0
    assert "No such command" in old_command.output
