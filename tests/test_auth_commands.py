from typer.testing import CliRunner

from ax_cli.commands import auth
from ax_cli.main import app

runner = CliRunner()


def test_login_alias_calls_auth_init(monkeypatch):
    """`ax login` is a top-level alias that forwards to `ax auth init` with identical kwargs."""
    called = {}

    def fake_init(token, base_url, agent, space_id):
        called.update(
            {
                "token": token,
                "base_url": base_url,
                "agent": agent,
                "space_id": space_id,
            }
        )

    monkeypatch.setattr(auth, "init", fake_init)

    result = runner.invoke(
        app,
        [
            "login",
            "--token",
            "axp_u_test.token",
            "--url",
            "https://next.paxai.app",
            "--agent",
            "anvil",
            "--space-id",
            "space-123",
        ],
    )

    assert result.exit_code == 0
    assert called == {
        "token": "axp_u_test.token",
        "base_url": "https://next.paxai.app",
        "agent": "anvil",
        "space_id": "space-123",
    }


def test_login_defaults_to_next_without_space_requirement(monkeypatch):
    """`ax login` is the user path: next URL by default, no space required."""
    called = {}

    def fake_init(token, base_url, agent, space_id):
        called.update(
            {
                "token": token,
                "base_url": base_url,
                "agent": agent,
                "space_id": space_id,
            }
        )

    monkeypatch.setattr(auth, "init", fake_init)

    result = runner.invoke(app, ["login", "--token", "axp_u_test.token"])

    assert result.exit_code == 0
    assert called == {
        "token": "axp_u_test.token",
        "base_url": "https://next.paxai.app",
        "agent": None,
        "space_id": None,
    }


def test_login_token_prompt_is_masked(monkeypatch):
    """Omitting --token prompts via Typer's hidden input path."""
    prompt_calls = []

    def fake_prompt(label, *, hide_input):
        prompt_calls.append({"label": label, "hide_input": hide_input})
        return " axp_u_prompt.token "

    monkeypatch.setattr(auth.typer, "prompt", fake_prompt)

    assert auth._resolve_login_token(None) == "axp_u_prompt.token"
    assert prompt_calls == [{"label": "Token", "hide_input": True}]


def test_login_space_selection_uses_only_unambiguous_space():
    assert auth._select_login_space([{"id": "space-1", "name": "Only"}]) == {"id": "space-1", "name": "Only"}
    assert auth._select_login_space(
        [
            {"id": "space-1", "name": "Team"},
            {"id": "space-2", "name": "Personal", "is_personal": True},
        ]
    ) == {"id": "space-2", "name": "Personal", "is_personal": True}
    assert (
        auth._select_login_space(
            [
                {"id": "space-1", "name": "Team A"},
                {"id": "space-2", "name": "Team B"},
            ]
        )
        is None
    )
