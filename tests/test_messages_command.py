from ax_cli.client import AxClient
from ax_cli.commands.messages import _configure_send_identity


def test_configure_send_identity_keeps_default_when_no_flags():
    """No explicit flags → preserve whatever get_client() resolved.

    Agent-scoped PATs need their identity header to avoid 403.
    """
    client = AxClient(
        "https://dev.paxai.app",
        "axp_u_test",
        agent_id="70c1b445-c733-44d8-8e75-9620452374a8",
    )

    _configure_send_identity(client, agent_name=None, agent_id=None)

    # Default identity is preserved, not stripped
    assert client._headers["X-Agent-Id"] == "70c1b445-c733-44d8-8e75-9620452374a8"


def test_configure_send_identity_keeps_default_name_when_no_flags():
    """Bootstrap client with only name — preserved when no flags."""
    client = AxClient(
        "https://dev.paxai.app",
        "axp_u_test",
        agent_name="orion",
    )

    _configure_send_identity(client, agent_name=None, agent_id=None)

    assert client._headers["X-Agent-Name"] == "orion"


def test_configure_send_identity_overrides_with_explicit_agent_name():
    client = AxClient(
        "https://dev.paxai.app",
        "axp_u_test",
        agent_id="70c1b445-c733-44d8-8e75-9620452374a8",
    )

    _configure_send_identity(client, agent_name="canvas", agent_id=None)

    assert client._headers["X-Agent-Name"] == "canvas"
    assert "X-Agent-Id" not in client._headers


def test_configure_send_identity_overrides_with_explicit_agent_id():
    client = AxClient(
        "https://dev.paxai.app",
        "axp_u_test",
        agent_name="orion",
    )

    _configure_send_identity(client, agent_name=None, agent_id="new-id")

    assert client._headers["X-Agent-Id"] == "new-id"
    assert "X-Agent-Name" not in client._headers
