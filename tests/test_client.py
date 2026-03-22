from ax_cli.client import AxClient


def test_client_uses_agent_id_when_both_are_provided():
    """After bind, both name and id exist — ID must win."""
    client = AxClient(
        "https://dev.paxai.app",
        "axp_u_test",
        agent_name="orion",
        agent_id="70c1b445-c733-44d8-8e75-9620452374a8",
    )

    assert client._headers["X-Agent-Id"] == "70c1b445-c733-44d8-8e75-9620452374a8"
    assert "X-Agent-Name" not in client._headers


def test_client_uses_agent_name_when_only_name_is_provided():
    """Bootstrap path: only name, no id yet."""
    client = AxClient(
        "https://dev.paxai.app",
        "axp_u_test",
        agent_name="orion",
    )

    assert client._headers["X-Agent-Name"] == "orion"
    assert "X-Agent-Id" not in client._headers


def test_client_uses_agent_id_when_only_id_is_provided():
    """Steady-state: id from config, no name needed."""
    client = AxClient(
        "https://dev.paxai.app",
        "axp_u_test",
        agent_id="70c1b445-c733-44d8-8e75-9620452374a8",
    )

    assert client._headers["X-Agent-Id"] == "70c1b445-c733-44d8-8e75-9620452374a8"
    assert "X-Agent-Name" not in client._headers


def test_explicit_agent_id_override_replaces_default_name_header():
    client = AxClient("https://dev.paxai.app", "axp_u_test", agent_name="orion")

    headers = client._with_agent("82d4765a-b2fc-4959-9765-d04d0b654fd0")

    assert headers["X-Agent-Id"] == "82d4765a-b2fc-4959-9765-d04d0b654fd0"
    assert "X-Agent-Name" not in headers


def test_explicit_agent_name_override_replaces_default_id_header():
    """Interactive --agent flag targets by name, overriding bound id."""
    client = AxClient(
        "https://dev.paxai.app",
        "axp_u_test",
        agent_id="70c1b445-c733-44d8-8e75-9620452374a8",
    )

    headers = client._with_agent(agent_name="relay")

    assert headers["X-Agent-Name"] == "relay"
    assert "X-Agent-Id" not in headers


def test_set_default_agent_switches_header():
    """set_default_agent replaces whichever header was set."""
    client = AxClient(
        "https://dev.paxai.app",
        "axp_u_test",
        agent_name="orion",
    )
    assert client._headers.get("X-Agent-Name") == "orion"

    client.set_default_agent(agent_id="70c1b445-c733-44d8-8e75-9620452374a8")

    assert client._headers["X-Agent-Id"] == "70c1b445-c733-44d8-8e75-9620452374a8"
    assert "X-Agent-Name" not in client._headers
