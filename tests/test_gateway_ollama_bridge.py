import importlib.util
from pathlib import Path


BRIDGE_PATH = Path(__file__).resolve().parents[1] / "examples" / "gateway_ollama" / "ollama_bridge.py"
spec = importlib.util.spec_from_file_location("gateway_ollama_bridge", BRIDGE_PATH)
ollama_bridge = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(ollama_bridge)


class _FakeHistoryClient:
    def __init__(self, messages):
        self.messages = messages
        self.requested_limit = None
        self.requested_space_id = None

    def list_messages(self, *, limit, space_id):
        self.requested_limit = limit
        self.requested_space_id = space_id
        return {"messages": self.messages[:limit]}


def test_shape_history_filters_to_agent_and_keeps_newest_context(monkeypatch):
    current = {
        "content": "@gemma4 What is my favorite color for this Gateway test? Reply with only the color token.",
        "display_name": "madtank",
        "metadata": {"mentions": ["gemma4"]},
    }
    prior_reply = {
        "content": "remembered aurora-teal-7429",
        "display_name": "gemma4",
        "agent_id": "agent-gemma",
    }
    prior_prompt = {
        "content": "@gemma4 My favorite color for this Gateway test is aurora-teal-7429.",
        "display_name": "madtank",
        "metadata": {"mentions": ["gemma4"]},
    }
    unrelated = {
        "content": "long unrelated team traffic " * 2000,
        "display_name": "backend_sentinel",
        "agent_id": "agent-backend",
    }
    fake = _FakeHistoryClient([current, prior_reply, prior_prompt, unrelated])

    monkeypatch.setenv("AX_GATEWAY_AGENT_NAME", "gemma4")
    monkeypatch.setenv("AX_GATEWAY_AGENT_ID", "agent-gemma")
    monkeypatch.setenv("AX_GATEWAY_SPACE_ID", "space-1")
    monkeypatch.setattr(ollama_bridge, "_build_client", lambda: fake)

    shaped = ollama_bridge._shape_history(
        "What is my favorite color for this Gateway test? Reply with only the color token."
    )

    assert fake.requested_space_id == "space-1"
    assert shaped == [
        {"role": "user", "content": "My favorite color for this Gateway test is aurora-teal-7429."},
        {"role": "assistant", "content": "remembered aurora-teal-7429"},
        {
            "role": "user",
            "content": "What is my favorite color for this Gateway test? Reply with only the color token.",
        },
    ]
