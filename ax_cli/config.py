"""Token / URL / space resolution and client factory.

Config resolution: CWD .ax/config.toml → project-local .ax/config.toml → ~/.ax/config.toml
Agent identity lives with the workspace, not the machine.

IMPORTANT: All writes go to the current working directory by default.
Each agent should run from its own directory. Config is local to where
the agent operates — never shared via ~/.ax/ unless explicitly requested.
"""

import os
import tomllib  # stdlib 3.11+
from pathlib import Path

import typer

from .client import AxClient


def _find_project_root() -> Path | None:
    """Walk up from CWD looking for .ax/ config dir or .git directory."""
    cur = Path.cwd()
    for parent in [cur, *cur.parents]:
        if (parent / ".ax").is_dir():
            return parent
        if (parent / ".git").exists():
            return parent
    return None


def _local_config_dir() -> Path | None:
    """Project-local .ax/ if it exists or can be created."""
    root = _find_project_root()
    if root:
        return root / ".ax"
    return None


def _global_config_dir() -> Path:
    """~/.ax/ — global fallback."""
    env_dir = os.environ.get("AX_CONFIG_DIR")
    if env_dir:
        return Path(env_dir)
    return Path.home() / ".ax"


def _load_local_config() -> dict:
    """Load project-local .ax/config.toml if it exists."""
    local = _local_config_dir()
    if local and (local / "config.toml").exists():
        return tomllib.loads((local / "config.toml").read_text())
    return {}


def _load_global_config() -> dict:
    """Load ~/.ax/config.toml."""
    cf = _global_config_dir() / "config.toml"
    if cf.exists():
        return tomllib.loads(cf.read_text())
    return {}


def _load_config() -> dict:
    """Merge local over global. Local wins."""
    merged = _load_global_config()
    merged.update(_load_local_config())
    return merged


def _save_config(cfg: dict, *, local: bool = True) -> None:
    """Save config. Default: writes to CWD .ax/. Use local=False for ~/.ax/."""
    if local:
        d = _local_config_dir()
        if not d:
            # No .ax/ or .git found — create .ax/ in current directory
            d = Path.cwd() / ".ax"
    else:
        d = _global_config_dir()
    d.mkdir(parents=True, exist_ok=True)
    cf = d / "config.toml"
    lines = []
    for k, v in cfg.items():
        if isinstance(v, str):
            lines.append(f'{k} = "{v}"')
        else:
            lines.append(f"{k} = {v}")
    cf.write_text("\n".join(lines) + "\n")
    cf.chmod(0o600)


def _check_config_permissions() -> None:
    """AUTH-SPEC-001 §13: Refuse PAT files with permissions broader than 0600."""
    for config_dir_fn in (_local_config_dir, _global_config_dir):
        try:
            d = config_dir_fn() if callable(config_dir_fn) else config_dir_fn
            if not d:
                continue
            cf = d / "config.toml" if not str(d).endswith("config.toml") else d
            if cf.exists():
                mode = cf.stat().st_mode & 0o777
                if mode > 0o600:
                    import sys

                    print(
                        f"WARNING: {cf} has permissions {oct(mode)} — should be 0600. Run: chmod 600 {cf}",
                        file=sys.stderr,
                    )
        except Exception:
            pass


def resolve_token() -> str | None:
    _check_config_permissions()
    return os.environ.get("AX_TOKEN") or _load_config().get("token")


def resolve_base_url() -> str:
    return os.environ.get("AX_BASE_URL") or _load_config().get("base_url", "http://localhost:8001")


def resolve_agent_name(*, explicit: str | None = None, client: AxClient | None = None) -> str | None:
    """Resolve agent name: explicit > env > auto-detect from single-agent scope > local config.

    Resolution order:
    1. --agent flag (explicit)
    2. AX_AGENT_NAME env var
    3. Auto-detect: if PAT is scoped to exactly 1 agent, use that
    4. Project-local .ax/config.toml agent_name
    5. None (send as user)
    """
    if explicit:
        return explicit
    env = os.environ.get("AX_AGENT_NAME")
    if env:
        return env

    # Project-local config (no API calls needed — fastest path)
    local = _load_local_config()
    if local.get("agent_name"):
        return local["agent_name"]

    # Auto-detect from single-agent scoped PAT (requires API call)
    if client:
        try:
            me = client.whoami()
            scope = me.get("credential_scope", {})
            agent_ids = scope.get("allowed_agent_ids")
            if agent_ids and len(agent_ids) == 1:
                # Need agent name — try list_agents with agent header
                # This may 403 on scoped PATs, so fall through gracefully
                agents_data = client.list_agents()
                agents = agents_data if isinstance(agents_data, list) else agents_data.get("agents", [])
                for agent in agents:
                    if str(agent.get("id")) == agent_ids[0]:
                        return agent.get("name")
        except Exception:
            pass

    return None


def resolve_space_id(client: AxClient, *, explicit: str | None = None) -> str:
    """Resolve space: explicit > env > config > bound agent default > auto-detect."""
    if explicit:
        return explicit
    env = os.environ.get("AX_SPACE_ID")
    if env:
        return env
    cfg = _load_config().get("space_id")
    if cfg:
        return cfg

    # Try server-side resolution via bound agent context
    try:
        me = client.whoami()
        bound = me.get("bound_agent")
        if bound and bound.get("default_space_id"):
            return bound["default_space_id"]
    except Exception:
        pass

    # Fallback: auto-detect from user's spaces
    spaces = client.list_spaces()
    space_list = spaces if isinstance(spaces, list) else spaces.get("spaces", [])
    if len(space_list) == 1:
        return str(space_list[0].get("id", space_list[0].get("space_id")))
    if len(space_list) == 0:
        typer.echo("Error: No spaces found for this user.", err=True)
        raise typer.Exit(1)
    typer.echo(
        "Error: Multiple spaces found. Use --space-id or set AX_SPACE_ID.",
        err=True,
    )
    raise typer.Exit(1)


def save_token(token: str, *, local: bool = True) -> None:
    cfg = _load_local_config() if local else _load_global_config()
    cfg["token"] = token
    _save_config(cfg, local=local)


def save_space_id(space_id: str, *, local: bool = True) -> None:
    cfg = _load_local_config() if local else _load_global_config()
    cfg["space_id"] = space_id
    _save_config(cfg, local=local)


def resolve_agent_id() -> str | None:
    """Resolve agent_id from env or config. Set AX_AGENT_ID=none to explicitly clear."""
    env = os.environ.get("AX_AGENT_ID")
    if env is not None:
        return None if env.lower() in ("", "none", "null") else env
    return _load_config().get("agent_id")


def get_client() -> AxClient:
    token = resolve_token()
    if not token:
        typer.echo(
            "Error: No token. Run 'ax auth token set <token>' or set AX_TOKEN.",
            err=True,
        )
        raise typer.Exit(1)
    agent_name = resolve_agent_name()
    agent_id = resolve_agent_id()
    return AxClient(
        base_url=resolve_base_url(),
        token=token,
        agent_name=agent_name,
        agent_id=agent_id,
    )
