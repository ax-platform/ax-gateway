"""gateway_dir() and friends must tolerate chmod failures on existing dirs.

Sandboxed environments (Codex, macOS sandbox-exec) raise PermissionError when
chmod runs against an already-correct existing directory. Every Gateway-touching
command calls gateway_dir() at least once, so an unconditional chmod makes the
whole CLI fragile in those environments.
"""

from pathlib import Path

import pytest

from ax_cli import gateway as gateway_core


@pytest.fixture
def gateway_root(tmp_path, monkeypatch):
    """Point gateway_dir() at a clean tmp location."""
    root = tmp_path / "ax_gateway"
    monkeypatch.setenv("AX_GATEWAY_DIR", str(root))
    return root


def _make_chmod_raise(monkeypatch):
    """Make Path.chmod raise PermissionError on every call."""
    real_chmod = Path.chmod

    def fake_chmod(self, mode):
        raise PermissionError(1, "Operation not permitted", str(self))

    monkeypatch.setattr(Path, "chmod", fake_chmod)
    return real_chmod


def test_gateway_dir_creates_with_0700_mode(gateway_root):
    path = gateway_core.gateway_dir()
    assert path == gateway_root
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o700


def test_gateway_dir_tolerates_chmod_failure_on_existing_dir(gateway_root, monkeypatch):
    gateway_root.mkdir(parents=True)
    gateway_root.chmod(0o700)
    _make_chmod_raise(monkeypatch)

    path = gateway_core.gateway_dir()
    assert path == gateway_root


def test_gateway_agents_dir_tolerates_chmod_failure_on_existing_dir(gateway_root, monkeypatch):
    gateway_root.mkdir(parents=True)
    gateway_root.chmod(0o700)
    (gateway_root / "agents").mkdir()
    (gateway_root / "agents").chmod(0o700)
    _make_chmod_raise(monkeypatch)

    path = gateway_core.gateway_agents_dir()
    assert path == gateway_root / "agents"


def test_agent_dir_tolerates_chmod_failure_on_existing_dir(gateway_root, monkeypatch):
    gateway_root.mkdir(parents=True)
    gateway_root.chmod(0o700)
    (gateway_root / "agents").mkdir()
    (gateway_root / "agents").chmod(0o700)
    (gateway_root / "agents" / "alpha").mkdir()
    (gateway_root / "agents" / "alpha").chmod(0o700)
    _make_chmod_raise(monkeypatch)

    path = gateway_core.agent_dir("alpha")
    assert path == gateway_root / "agents" / "alpha"


def test_chmod_quiet_reraises_when_mode_is_actually_wrong(gateway_root, monkeypatch):
    """Tolerance must NOT mask a real permission gap on a too-permissive dir."""
    gateway_root.mkdir(parents=True)
    gateway_root.chmod(0o755)
    _make_chmod_raise(monkeypatch)

    with pytest.raises(PermissionError):
        gateway_core.gateway_dir()


def test_gateway_dir_still_raises_when_mkdir_fails(gateway_root, monkeypatch):
    """We only swallow chmod EPERM; mkdir failures still propagate."""

    def fake_mkdir(self, *args, **kwargs):
        raise PermissionError(1, "Operation not permitted", str(self))

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)

    with pytest.raises(PermissionError):
        gateway_core.gateway_dir()
