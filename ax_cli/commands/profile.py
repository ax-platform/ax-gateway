"""Profile management — named configs with token fingerprinting.

Profiles store connection settings (URL, agent, space) plus a SHA-256
fingerprint of the token file, the hostname, and the working directory
where the profile was created. On use, all three are verified before
the token is loaded.

Storage: ~/.ax/profiles/<name>/profile.toml
"""

import hashlib
import shlex
import socket
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import typer
from rich.table import Table

from ..config import _global_config_dir
from ..output import console

app = typer.Typer(help="Named profiles with credential fingerprinting")

PROFILES_DIR: Path | None = None


def _profiles_dir() -> Path:
    d = PROFILES_DIR or (_global_config_dir() / "profiles")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _profile_path(name: str) -> Path:
    return _profiles_dir() / name / "profile.toml"


def _load_profile(name: str) -> dict:
    p = _profile_path(name)
    if not p.exists():
        typer.echo(f"Profile '{name}' not found.", err=True)
        raise typer.Exit(1)
    return tomllib.loads(p.read_text())


def _token_sha256(token_file: str) -> str:
    content = Path(token_file).expanduser().read_text().strip()
    return hashlib.sha256(content.encode()).hexdigest()


def _workdir_hash(directory: str | None = None) -> str:
    """SHA-256 of the resolved working directory path."""
    d = Path(directory).resolve() if directory else Path.cwd().resolve()
    return hashlib.sha256(str(d).encode()).hexdigest()


def _write_toml(path: Path, data: dict) -> None:
    """Write a flat dict as TOML."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for k, v in data.items():
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, str):
            lines.append(f'{k} = "{v}"')
        else:
            lines.append(f"{k} = {v}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def _active_profile() -> str | None:
    marker = _profiles_dir() / ".active"
    if marker.exists():
        return marker.read_text().strip()
    return None


def _set_active(name: str) -> None:
    marker = _profiles_dir() / ".active"
    marker.write_text(name + "\n")


def _print_shell_export(name: str, value: str) -> None:
    print(f"export {name}={shlex.quote(value)}")


def _verify_profile(profile: dict) -> list[str]:
    """Return list of verification failures (empty = all good)."""
    failures = []
    token_file = profile.get("token_file", "")
    tf = Path(token_file).expanduser()

    if not tf.exists():
        failures.append(f"Token file missing: {token_file}")
        return failures

    current_sha = _token_sha256(token_file)
    if current_sha != profile.get("token_sha256", ""):
        failures.append(
            f"Token fingerprint mismatch — file has been modified. "
            f"Expected {profile.get('token_sha256', '?')[:12]}..., "
            f"got {current_sha[:12]}..."
        )

    current_host = socket.gethostname()
    expected_host = profile.get("host_binding", "")
    if expected_host and current_host != expected_host:
        failures.append(f"Host mismatch — expected {expected_host}, running on {current_host}")

    expected_workdir = profile.get("workdir_hash", "")
    if expected_workdir:
        current_workdir = _workdir_hash()
        if current_workdir != expected_workdir:
            failures.append(
                f"Working directory mismatch — expected {expected_workdir[:12]}..., "
                f"running from {current_workdir[:12]}... ({Path.cwd()})"
            )

    return failures


def _register_fingerprint(profile: dict) -> str | None:
    """Register fingerprint with backend. Returns violation info or None."""
    base_url = profile.get("base_url", "")
    agent_id = profile.get("agent_id", "")
    if not base_url or not agent_id:
        return None

    token_file = profile.get("token_file", "")
    tf = Path(token_file).expanduser()
    if not tf.exists():
        return None

    token = tf.read_text().strip()
    try:
        r = httpx.post(
            f"{base_url}/api/v1/credentials/fingerprint",
            json={
                "agent_id": agent_id,
                "token_sha256": profile.get("token_sha256", ""),
                "host_binding": socket.gethostname(),
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code in (200, 201):
            return None
        return f"Backend registration: {r.status_code}"
    except httpx.ConnectError:
        return None  # Backend unreachable, not a failure


@app.command("add")
def add(
    name: str = typer.Argument(..., help="Profile name (e.g. next-orion)"),
    url: str = typer.Option(..., "--url", help="Base URL (e.g. https://paxai.app)"),
    token_file: str = typer.Option(..., "--token-file", help="Path to token file"),
    agent_name: str = typer.Option(..., "--agent-name", help="Agent name"),
    agent_id: Optional[str] = typer.Option(None, "--agent-id", help="Agent UUID"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Default space UUID"),
):
    """Create or update a named profile with token fingerprinting."""
    tf = Path(token_file).expanduser()
    if not tf.exists():
        typer.echo(f"Token file not found: {token_file}", err=True)
        raise typer.Exit(1)

    sha = _token_sha256(token_file)
    hostname = socket.gethostname()
    wdir = _workdir_hash()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    data = {
        "name": name,
        "base_url": url,
        "agent_name": agent_name,
        "token_file": str(tf.resolve()),
        "token_sha256": sha,
        "host_binding": hostname,
        "workdir_hash": wdir,
        "workdir_path": str(Path.cwd().resolve()),
        "created_at": now,
    }
    if agent_id:
        data["agent_id"] = agent_id
    if space_id:
        data["space_id"] = space_id

    _write_toml(_profile_path(name), data)
    console.print(f"Profile [bold]{name}[/bold] saved.")
    console.print(f"  Token: {sha[:12]}...")
    console.print(f"  Host: {hostname}")
    console.print(f"  Workdir: {Path.cwd()} ({wdir[:12]}...)")


@app.command("use")
def use(
    name: str = typer.Argument(..., help="Profile to activate"),
):
    """Switch to a named profile (sets it as default for ax commands)."""
    profile = _load_profile(name)
    failures = _verify_profile(profile)
    if failures:
        console.print(f"[red]Cannot activate profile '{name}' — verification failed:[/red]")
        for f in failures:
            console.print(f"  [red]• {f}[/red]")
        raise typer.Exit(1)

    _set_active(name)
    console.print(f"Active profile: [bold]{name}[/bold] → {profile.get('base_url')} as {profile.get('agent_name')}")

    # Register fingerprint with backend (best-effort)
    err = _register_fingerprint(profile)
    if err:
        console.print(f"  [yellow]Backend: {err}[/yellow]")
    elif profile.get("agent_id"):
        console.print("  [dim]Fingerprint registered with backend[/dim]")


@app.command("list")
def list_profiles():
    """Show all profiles. Active profile is marked."""
    pdir = _profiles_dir()
    active = _active_profile()
    profiles = sorted(d.name for d in pdir.iterdir() if d.is_dir() and (d / "profile.toml").exists())
    if not profiles:
        typer.echo("No profiles. Create one with: ax profile add <name> --url ... --token-file ... --agent-name ...")
        return

    table = Table(show_header=True)
    table.add_column("", width=2)
    table.add_column("Name")
    table.add_column("URL")
    table.add_column("Agent")
    table.add_column("Fingerprint")
    table.add_column("Status")

    for name in profiles:
        profile = tomllib.loads((_profile_path(name)).read_text())
        marker = "→" if name == active else ""
        failures = _verify_profile(profile)
        status = "[green]ok[/green]" if not failures else f"[red]{len(failures)} issue(s)[/red]"
        sha_short = profile.get("token_sha256", "?")[:12] + "..."
        table.add_row(
            marker,
            name,
            profile.get("base_url", ""),
            profile.get("agent_name", ""),
            sha_short,
            status,
        )
    console.print(table)


@app.command("verify")
def verify(
    name: Optional[str] = typer.Argument(None, help="Profile to verify (default: active)"),
):
    """Check token fingerprint and host binding for a profile."""
    if name is None:
        name = _active_profile()
        if not name:
            typer.echo("No active profile. Specify a name or run: ax profile use <name>", err=True)
            raise typer.Exit(1)

    profile = _load_profile(name)
    failures = _verify_profile(profile)

    if not failures:
        console.print(f"[green]Profile '{name}' verified.[/green]")
        console.print(f"  Token: {profile.get('token_sha256', '?')[:12]}... ✓")
        console.print(f"  Host: {profile.get('host_binding', '?')} ✓")
        if profile.get("workdir_hash"):
            console.print(
                f"  Workdir: {profile.get('workdir_path', '?')} ({profile.get('workdir_hash', '?')[:12]}...) ✓"
            )
    else:
        console.print(f"[red]Profile '{name}' failed verification:[/red]")
        for f in failures:
            console.print(f"  [red]• {f}[/red]")
        raise typer.Exit(1)


@app.command("remove")
def remove(
    name: str = typer.Argument(..., help="Profile to remove"),
):
    """Delete a profile."""
    p = _profile_path(name)
    if not p.exists():
        typer.echo(f"Profile '{name}' not found.", err=True)
        raise typer.Exit(1)

    p.unlink()
    p.parent.rmdir()

    active = _active_profile()
    if active == name:
        (_profiles_dir() / ".active").unlink(missing_ok=True)
        console.print(f"Removed profile [bold]{name}[/bold] (was active — no profile selected now)")
    else:
        console.print(f"Removed profile [bold]{name}[/bold]")


@app.command("env")
def show_env(
    name: Optional[str] = typer.Argument(None, help="Profile (default: active)"),
):
    """Print export statements for shell use (eval $(ax profile env))."""
    if name is None:
        name = _active_profile()
        if not name:
            typer.echo("No active profile.", err=True)
            raise typer.Exit(1)

    profile = _load_profile(name)
    failures = _verify_profile(profile)
    if failures:
        typer.echo(f"# Profile '{name}' failed verification:", err=True)
        for f in failures:
            typer.echo(f"#   {f}", err=True)
        # Command substitutions mask the child's exit code:
        # `eval "$(ax profile env bad)" && next` would otherwise run `next`
        # with whatever stale AX_* variables are already present.
        print("false # ax profile env failed verification")
        raise typer.Exit(1)

    tf = Path(profile["token_file"]).expanduser()
    token = tf.read_text().strip()

    _print_shell_export("AX_TOKEN", token)
    _print_shell_export("AX_BASE_URL", profile.get("base_url", ""))
    _print_shell_export("AX_AGENT_NAME", profile.get("agent_name", ""))
    if profile.get("agent_id"):
        _print_shell_export("AX_AGENT_ID", profile["agent_id"])
    else:
        _print_shell_export("AX_AGENT_ID", "none")
    if profile.get("space_id"):
        _print_shell_export("AX_SPACE_ID", profile["space_id"])
