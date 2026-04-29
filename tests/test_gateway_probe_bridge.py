import importlib.util
from pathlib import Path


def _load_probe_module():
    probe_path = Path(__file__).resolve().parents[1] / "examples" / "gateway_probe" / "probe_bridge.py"
    spec = importlib.util.spec_from_file_location("gateway_probe_bridge", probe_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_probe_seconds_defaults_and_caps():
    probe = _load_probe_module()

    assert probe._probe_seconds("probe") == probe.DEFAULT_SECONDS
    assert probe._probe_seconds("probe 3") == 3
    assert probe._probe_seconds("run a 999 second probe") == probe.MAX_SECONDS


def test_run_probe_emits_predictable_sequence(monkeypatch, capsys):
    probe = _load_probe_module()
    events = []

    monkeypatch.setattr(probe, "emit_event", events.append)
    monkeypatch.setattr(probe.uuid, "uuid4", lambda: "fixed-id")
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)

    assert probe._run_probe(3) == 0

    out = capsys.readouterr().out.strip()
    assert out == "PROBE_OK seconds=3"
    assert [event["kind"] for event in events] == [
        "status",
        "status",
        "status",
        "tool_start",
        "activity",
        "activity",
        "activity",
        "tool_result",
        "status",
    ]
    assert events[0]["status"] == "started"
    assert events[1]["status"] == "thinking"
    assert events[2]["status"] == "processing"
    assert events[3]["tool_name"] == "probe_sleep"
    assert events[4]["activity"] == "Probe tick 1/3 (3s left)"
    assert events[6]["activity"] == "Probe tick 3/3 (1s left)"
    assert events[7]["status"] == "tool_complete"
    assert events[8]["status"] == "completed"
