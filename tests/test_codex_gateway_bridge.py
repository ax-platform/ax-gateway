import importlib.util
from pathlib import Path


def _load_bridge_module():
    bridge_path = Path(__file__).resolve().parents[1] / "examples" / "codex_gateway" / "codex_bridge.py"
    spec = importlib.util.spec_from_file_location("codex_gateway_bridge", bridge_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_sleep_demo_matches_timer_phrases():
    bridge = _load_bridge_module()

    assert bridge._sleep_demo_seconds("pause for 30 seconds") == 30
    assert bridge._sleep_demo_seconds("do a 30 second timer") == 30
    assert bridge._sleep_demo_seconds("start a 12 second countdown") == 12
    assert bridge._sleep_demo_seconds("timer for 9 seconds") == 9
