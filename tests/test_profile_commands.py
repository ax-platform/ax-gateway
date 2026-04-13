import hashlib
import socket

from typer.testing import CliRunner

from ax_cli.commands import profile
from ax_cli.main import app

runner = CliRunner()


def _write_profile(tmp_path, *, name="dev", agent_id=None):
    profiles_dir = tmp_path / "profiles"
    token_file = tmp_path / "token.pat"
    token_file.write_text("axp_a_TestKey.TestSecret")
    token_sha = hashlib.sha256(token_file.read_text().strip().encode()).hexdigest()
    workdir_hash = hashlib.sha256(str(tmp_path.resolve()).encode()).hexdigest()
    profile_dir = profiles_dir / name
    profile_dir.mkdir(parents=True)
    lines = [
        f'name = "{name}"',
        'base_url = "https://dev.paxai.app"',
        'agent_name = "chatgpt"',
        f'token_file = "{token_file}"',
        f'token_sha256 = "{token_sha}"',
        f'host_binding = "{socket.gethostname()}"',
        f'workdir_hash = "{workdir_hash}"',
        'space_id = "space-1"',
    ]
    if agent_id is not None:
        lines.append(f'agent_id = "{agent_id}"')
    (profile_dir / "profile.toml").write_text("\n".join(lines) + "\n")
    return profiles_dir


def test_profile_env_exports_agent_id_when_present(monkeypatch, tmp_path):
    profiles_dir = _write_profile(tmp_path, agent_id="agent-1")
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["profile", "env", "dev"])

    assert result.exit_code == 0
    assert 'export AX_AGENT_ID="agent-1"' in result.stdout


def test_profile_env_clears_stale_agent_id_when_missing(monkeypatch, tmp_path):
    profiles_dir = _write_profile(tmp_path, agent_id=None)
    monkeypatch.setattr(profile, "PROFILES_DIR", profiles_dir)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["profile", "env", "dev"])

    assert result.exit_code == 0
    assert 'export AX_AGENT_ID="none"' in result.stdout
