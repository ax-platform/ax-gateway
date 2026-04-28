"""Token / URL / space resolution and client factory.

Config resolution: CWD .ax/config.toml → project-local .ax/config.toml → ~/.ax/config.toml
Agent identity lives with the workspace, not the machine.

IMPORTANT: All writes go to the current working directory by default.
Each agent should run from its own directory. Config is local to where
the agent operates — never shared via ~/.ax/ unless explicitly requested.
"""

import os
import re
import tomllib  # stdlib 3.11+
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

import typer

from .client import AxClient


def _find_project_root() -> Path | None:
    """Walk up from CWD looking for .ax/ config dir.

    Does NOT use .git boundaries — identity is workspace-scoped, not
    repo-scoped. The agent's working directory determines config, not
    which git repo they happen to be inside.
    """
    cur = Path.cwd()
    for parent in [cur, *cur.parents]:
        if (parent / ".ax").is_dir():
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


def _normalize_user_env(env_name: str) -> str:
    """Return a filesystem-safe user-login environment name."""
    value = env_name.strip().lower()
    if not value:
        raise ValueError("User environment name cannot be empty")
    return re.sub(r"[^a-z0-9_.-]+", "-", value).strip(".-")


def _active_user_env_path() -> Path:
    return _global_config_dir() / "users" / ".active"


def _resolve_user_env() -> str | None:
    env = os.environ.get("AX_USER_ENV") or os.environ.get("AX_ENV")
    if env:
        return _normalize_user_env(env)
    marker = _active_user_env_path()
    if marker.exists():
        value = marker.read_text().strip()
        if value:
            return _normalize_user_env(value)
    return None


def _set_active_user_env(env_name: str) -> None:
    marker = _active_user_env_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(_normalize_user_env(env_name) + "\n")
    marker.chmod(0o600)


def _user_config_path(env_name: str | None = None) -> Path:
    """User login credential store, separate from agent runtime config.

    Backward compatible default is ~/.ax/user.toml. Named environments use
    ~/.ax/users/<env>/user.toml and can be selected with AX_ENV/AX_USER_ENV or
    by the active environment marker written by `axctl login --env`.
    """
    resolved = _normalize_user_env(env_name) if env_name else _resolve_user_env()
    if resolved in {"default", "user"}:
        return _global_config_dir() / "user.toml"
    if resolved:
        return _global_config_dir() / "users" / resolved / "user.toml"
    return _global_config_dir() / "user.toml"


def _load_user_config(env_name: str | None = None) -> dict:
    """Load the user login config created by `axctl login`."""
    cf = _user_config_path(env_name)
    if cf.exists():
        return tomllib.loads(cf.read_text())
    return {}


def _save_user_config(cfg: dict, *, env_name: str | None = None, activate: bool = True) -> Path:
    """Save user login config without touching agent workspace config."""
    cf = _user_config_path(env_name)
    d = cf.parent
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    for k, v in cfg.items():
        if isinstance(v, str):
            lines.append(f'{k} = "{v}"')
        else:
            lines.append(f"{k} = {v}")
    cf.write_text("\n".join(lines) + "\n")
    cf.chmod(0o600)
    if env_name and activate:
        _set_active_user_env(env_name)
    return cf


def _load_local_config() -> dict:
    """Load project-local .ax/config.toml if it exists."""
    local = _local_config_dir()
    if local and (local / "config.toml").exists():
        return tomllib.loads((local / "config.toml").read_text())
    return {}


def _load_runtime_config_file(raw_path: str | None) -> dict:
    """Load an explicit runtime config file and resolve its token_file."""
    if not raw_path:
        return {}
    config_path = Path(raw_path).expanduser()
    cfg = tomllib.loads(config_path.read_text())
    token_file = cfg.get("token_file")
    if token_file and not cfg.get("token"):
        token_path = Path(str(token_file)).expanduser()
        if not token_path.is_absolute():
            token_path = config_path.parent / token_path
        cfg["token"] = token_path.read_text().strip()
    return cfg


def _read_token_file(raw_path: str | None) -> str | None:
    if not raw_path:
        return None
    try:
        return Path(raw_path).expanduser().read_text().strip()
    except OSError:
        return None


_global_config_warned = False
_unsafe_local_config_warned = False


def _load_global_config() -> dict:
    """Load ~/.ax/config.toml.

    Warns (once) if global config contains credentials (token, agent_id,
    agent_name). These should live in profiles or workspace config, not
    the global fallback. Global config should only have base_url defaults.
    """
    global _global_config_warned
    cf = _global_config_dir() / "config.toml"
    if not cf.exists():
        return {}
    cfg = tomllib.loads(cf.read_text())
    # Warn about credentials in global config
    cred_keys = {"token", "token_file", "agent_id", "agent_name"}
    found = cred_keys & set(cfg.keys())
    if found and not _global_config_warned:
        _global_config_warned = True
        import sys

        sys.stderr.write(
            f"\033[33m⚠  Global config (~/.ax/config.toml) contains credentials: {', '.join(sorted(found))}\033[0m\n"
            "   Move credentials to a profile (ax profile add) or workspace .ax/config.toml.\n"
            "   Global config should only have defaults like base_url.\n\n"
        )
    return cfg


def _has_agent_identity(cfg: dict) -> bool:
    return bool(cfg.get("agent_id") or cfg.get("agent_name"))


def _is_unsafe_user_token_agent_config(cfg: dict) -> bool:
    """Detect local configs that would make an agent act with a user PAT.

    A valid agent runtime config uses an agent PAT (`axp_a_`) or an explicit
    non-PAT token. A valid user login config declares `principal_type = "user"`.
    The unsafe shape is the stale hybrid: user PAT plus agent identity.
    """
    token = str(cfg.get("token") or "")
    principal_type = str(cfg.get("principal_type") or "").lower()
    return token.startswith("axp_u_") and principal_type != "user" and _has_agent_identity(cfg)


def _warn_ignored_unsafe_local_config(config_path: Path) -> None:
    global _unsafe_local_config_warned
    if _unsafe_local_config_warned:
        return
    _unsafe_local_config_warned = True
    import sys

    sys.stderr.write(
        f"\033[33m⚠  Ignoring unsafe local aX config: {config_path}\033[0m\n"
        "   It combines a user PAT (axp_u_) with agent identity fields.\n"
        "   User PATs are for user-authored setup and API work, not agent runtime identity.\n"
        '   Use an agent PAT profile for agent work, or set principal_type = "user" for user-only config.\n\n'
    )


def _load_active_profile_config() -> dict:
    """Load the active profile as normal command defaults.

    `ax profile use` has always promised to set the default profile, but the
    command factory only read config.toml. This makes profiles boring: once a
    profile is active, ordinary `ax context ...` and `ax spaces ...` commands
    use its base URL and token file unless env/local config overrides them.
    """

    marker = _global_config_dir() / "profiles" / ".active"
    if not marker.exists():
        return {}

    name = marker.read_text().strip()
    if not name:
        return {}

    profile_path = _global_config_dir() / "profiles" / name / "profile.toml"
    if not profile_path.exists():
        return {}

    profile = tomllib.loads(profile_path.read_text())
    cfg: dict = {}
    if profile.get("base_url"):
        cfg["base_url"] = profile["base_url"]
    if "agent_name" in profile:
        cfg["agent_name"] = profile.get("agent_name")
    # Explicitly clear stale global config when the active profile is user-only.
    cfg["agent_id"] = profile.get("agent_id")
    cfg["space_id"] = profile.get("space_id")

    token_file = profile.get("token_file")
    if token_file:
        try:
            cfg["token"] = Path(token_file).expanduser().read_text().strip()
        except OSError:
            pass
    return cfg


def _active_profile_name() -> str | None:
    marker = _global_config_dir() / "profiles" / ".active"
    if not marker.exists():
        return None
    name = marker.read_text().strip()
    return name or None


def _active_profile_path(name: str | None = None) -> Path | None:
    profile_name = name or _active_profile_name()
    if not profile_name:
        return None
    return _global_config_dir() / "profiles" / profile_name / "profile.toml"


def _load_active_profile_diagnostic() -> tuple[str | None, Path | None, dict]:
    name = _active_profile_name()
    path = _active_profile_path(name)
    if not name or not path or not path.exists():
        return name, path, {}

    profile = tomllib.loads(path.read_text())
    cfg: dict = {}
    if profile.get("base_url"):
        cfg["base_url"] = profile["base_url"]
    if "agent_name" in profile:
        cfg["agent_name"] = profile.get("agent_name")
    if "agent_id" in profile:
        cfg["agent_id"] = profile.get("agent_id")
    if profile.get("space_id"):
        cfg["space_id"] = profile.get("space_id")

    token_file = profile.get("token_file")
    if token_file:
        cfg["token_file"] = str(Path(token_file).expanduser())
        try:
            cfg["token"] = Path(token_file).expanduser().read_text().strip()
        except OSError:
            cfg["token_error"] = f"cannot read token_file: {token_file}"
    return name, path, cfg


def _token_kind(token: str | None) -> str:
    if not token:
        return "missing"
    if token.startswith("axp_u_"):
        return "user_pat"
    if token.startswith("axp_a_"):
        return "agent_pat"
    if token.startswith("eyJ"):
        return "jwt"
    return "other"


def _redact_token(token: str | None) -> str | None:
    if not token:
        return None
    if len(token) <= 10:
        return "***"
    return f"{token[:6]}...{token[-4:]}"


def _host_from_url(base_url: str | None) -> str | None:
    if not base_url:
        return None
    parsed = urlparse(base_url)
    return parsed.hostname or base_url


def _source_record(
    name: str,
    *,
    path: Path | None = None,
    exists: bool,
    used: bool = False,
    ignored: bool = False,
    reason: str | None = None,
    keys: list[str] | None = None,
) -> dict:
    record = {
        "name": name,
        "exists": exists,
        "used": used,
        "ignored": ignored,
    }
    if path:
        record["path"] = str(path)
    if reason:
        record["reason"] = reason
    if keys:
        record["keys"] = sorted(keys)
    return record


def diagnose_auth_config(*, env_name: str | None = None, explicit_space_id: str | None = None) -> dict:
    """Return machine-readable auth/config resolution diagnostics.

    This is intentionally static: it does not exchange tokens or call the API.
    `ax qa preflight` is the runtime truth gate; this is the instrument panel
    that explains which local inputs would feed that runtime path.
    """

    normalized_env = _normalize_user_env(env_name) if env_name else None
    sources: list[dict] = []
    warnings: list[dict] = []
    problems: list[dict] = []
    field_sources: dict[str, str] = {}
    effective: dict[str, str | None] = {}

    def apply_cfg(cfg: dict, source: str) -> None:
        for key in ("token", "base_url", "agent_name", "agent_id", "space_id", "principal_type"):
            if key in cfg:
                value = cfg.get(key)
                if value is not None:
                    effective[key] = str(value)
                else:
                    effective.pop(key, None)
                field_sources[key] = source

    global_path = _global_config_dir() / "config.toml"
    global_cfg = tomllib.loads(global_path.read_text()) if global_path.exists() else {}
    global_cred_keys = sorted({"token", "token_file", "agent_id", "agent_name"} & set(global_cfg.keys()))
    sources.append(
        _source_record(
            "global_config",
            path=global_path,
            exists=global_path.exists(),
            used=not bool(normalized_env) and bool(global_cfg),
            keys=list(global_cfg.keys()) if global_cfg else None,
        )
    )
    if global_cred_keys:
        warnings.append(
            {
                "code": "global_config_contains_credentials",
                "path": str(global_path),
                "keys": global_cred_keys,
                "reason": "global config should only contain defaults such as base_url",
            }
        )

    selected_profile_name, selected_profile_path, active_profile_cfg = _load_active_profile_diagnostic()
    selected_user_env = normalized_env or _resolve_user_env()
    user_cfg = _load_user_config(selected_user_env)
    user_path = _user_config_path(selected_user_env)
    explicit_cfg_env = os.environ.get("AX_CONFIG_FILE")
    explicit_cfg_path = Path(explicit_cfg_env).expanduser() if explicit_cfg_env else None
    explicit_cfg = _load_runtime_config_file(explicit_cfg_env)

    local_dir = _local_config_dir()
    local_path = (local_dir / "config.toml") if local_dir else None
    local_cfg = tomllib.loads(local_path.read_text()) if local_path and local_path.exists() else {}
    unsafe_local = bool(local_cfg and _is_unsafe_user_token_agent_config(local_cfg))

    if normalized_env:
        sources.append(
            _source_record(
                f"user_login:{normalized_env}",
                path=user_path,
                exists=user_path.exists(),
                used=bool(user_cfg),
                keys=list(user_cfg.keys()) if user_cfg else None,
            )
        )
        if not user_cfg:
            problems.append(
                {
                    "code": "missing_user_login_env",
                    "reason": f"No user login found for env '{normalized_env}'",
                }
            )
        else:
            apply_cfg(user_cfg, f"user_login:{normalized_env}")
            effective["principal_type"] = "user"
            field_sources["principal_type"] = f"user_login:{normalized_env}"

        if selected_profile_name:
            sources.append(
                _source_record(
                    f"active_profile:{selected_profile_name}",
                    path=selected_profile_path,
                    exists=bool(selected_profile_path and selected_profile_path.exists()),
                    ignored=True,
                    reason="--env selects a named user login and bypasses active agent profiles",
                    keys=list(active_profile_cfg.keys()) if active_profile_cfg else None,
                )
            )
        if local_path:
            reason = "--env selects a named user login and bypasses local runtime config"
            if unsafe_local:
                reason = "unsafe user PAT plus agent identity; also bypassed by --env"
            sources.append(
                _source_record(
                    "local_config",
                    path=local_path,
                    exists=local_path.exists(),
                    ignored=local_path.exists(),
                    reason=reason if local_path.exists() else None,
                    keys=list(local_cfg.keys()) if local_cfg else None,
                )
            )
            if unsafe_local:
                warnings.append(
                    {
                        "code": "unsafe_local_config_ignored",
                        "path": str(local_path),
                        "reason": "local config combines user PAT (axp_u_) with agent identity fields",
                    }
                )
    else:
        apply_cfg(global_cfg, "global_config")

        sources.append(
            _source_record(
                f"user_login:{selected_user_env}" if selected_user_env else "user_login",
                path=user_path,
                exists=user_path.exists(),
                used=bool(user_cfg),
                keys=list(user_cfg.keys()) if user_cfg else None,
            )
        )
        apply_cfg(user_cfg, f"user_login:{selected_user_env}" if selected_user_env else "user_login")

        if selected_profile_name:
            sources.append(
                _source_record(
                    f"active_profile:{selected_profile_name}",
                    path=selected_profile_path,
                    exists=bool(selected_profile_path and selected_profile_path.exists()),
                    used=bool(active_profile_cfg),
                    keys=list(active_profile_cfg.keys()) if active_profile_cfg else None,
                )
            )
            apply_cfg(active_profile_cfg, f"active_profile:{selected_profile_name}")
            if "principal_type" not in active_profile_cfg and _has_agent_identity(active_profile_cfg):
                effective["principal_type"] = "agent"
                field_sources["principal_type"] = f"active_profile:{selected_profile_name}"
        else:
            sources.append(
                _source_record(
                    "active_profile",
                    path=selected_profile_path,
                    exists=False,
                    used=False,
                )
            )

        if local_path:
            if unsafe_local:
                sources.append(
                    _source_record(
                        "local_config",
                        path=local_path,
                        exists=local_path.exists(),
                        ignored=True,
                        reason="local config combines user PAT (axp_u_) with agent identity fields",
                        keys=list(local_cfg.keys()) if local_cfg else None,
                    )
                )
                warnings.append(
                    {
                        "code": "unsafe_local_config_ignored",
                        "path": str(local_path),
                        "reason": "local config combines user PAT (axp_u_) with agent identity fields",
                    }
                )
            else:
                sources.append(
                    _source_record(
                        "local_config",
                        path=local_path,
                        exists=local_path.exists(),
                        used=bool(local_cfg),
                        keys=list(local_cfg.keys()) if local_cfg else None,
                    )
                )
                apply_cfg(local_cfg, "local_config")
                if "principal_type" not in local_cfg and _has_agent_identity(local_cfg):
                    effective["principal_type"] = "agent"
                    field_sources["principal_type"] = "local_config"

    if explicit_cfg_path:
        sources.append(
            _source_record(
                "runtime_config",
                path=explicit_cfg_path,
                exists=explicit_cfg_path.exists(),
                used=bool(explicit_cfg),
                keys=list(explicit_cfg.keys()) if explicit_cfg else None,
            )
        )
        if explicit_cfg:
            runtime_source = f"runtime_config:{explicit_cfg_path}"
            apply_cfg(explicit_cfg, runtime_source)
            if "principal_type" not in explicit_cfg and _has_agent_identity(explicit_cfg):
                effective["principal_type"] = "agent"
                field_sources["principal_type"] = runtime_source
    else:
        sources.append(
            _source_record(
                "runtime_config",
                path=None,
                exists=False,
                used=False,
            )
        )

    used_env_keys: list[str] = []
    if not normalized_env:
        env_overrides = {
            "token": os.environ.get("AX_TOKEN"),
            "base_url": os.environ.get("AX_BASE_URL"),
            "agent_name": os.environ.get("AX_AGENT_NAME"),
            "agent_id": os.environ.get("AX_AGENT_ID"),
            "space_id": os.environ.get("AX_SPACE_ID"),
        }
        for key, value in env_overrides.items():
            if value is None:
                continue
            used_env_keys.append(f"AX_{key.upper()}")
            if key in {"agent_name", "agent_id"} and value.lower() in ("", "none", "null"):
                effective.pop(key, None)
            else:
                effective[key] = value
            field_sources[key] = f"env:AX_{key.upper()}"
        if used_env_keys:
            sources.append(
                _source_record(
                    "environment",
                    exists=True,
                    used=True,
                    keys=used_env_keys,
                )
            )
    if explicit_space_id:
        effective["space_id"] = explicit_space_id
        field_sources["space_id"] = "option:--space-id"

    token = effective.get("token")
    base_url = effective.get("base_url") or "http://localhost:8001"
    token_kind = _token_kind(str(token) if token else None)
    agent_identity_present = bool(effective.get("agent_id") or effective.get("agent_name"))
    principal_type = effective.get("principal_type")
    if token_kind == "user_pat" and agent_identity_present and principal_type != "user":
        principal_intent = "mixed_user_token_agent_identity"
        problems.append(
            {
                "code": "user_pat_with_agent_identity",
                "reason": "effective config would combine user PAT with agent identity",
            }
        )
    elif principal_type == "user" or token_kind == "user_pat":
        principal_intent = "user"
    elif principal_type == "agent" or token_kind == "agent_pat" or agent_identity_present:
        principal_intent = "agent"
    elif token_kind == "missing":
        principal_intent = "missing"
        problems.append({"code": "missing_token", "reason": "no token resolved"})
    else:
        principal_intent = "unknown"

    return {
        "ok": not problems,
        "selected_env": normalized_env or selected_user_env,
        "selected_profile": selected_profile_name,
        "runtime_config": str(explicit_cfg_path) if explicit_cfg_path else None,
        "effective": {
            "auth_source": field_sources.get("token"),
            "token_kind": token_kind,
            "token": _redact_token(str(token) if token else None),
            "base_url": base_url,
            "base_url_source": field_sources.get("base_url"),
            "host": _host_from_url(base_url),
            "space_id": effective.get("space_id"),
            "space_source": field_sources.get("space_id"),
            "agent_name": effective.get("agent_name"),
            "agent_name_source": field_sources.get("agent_name"),
            "agent_id": effective.get("agent_id"),
            "agent_id_source": field_sources.get("agent_id"),
            "principal_type": principal_type,
            "principal_intent": principal_intent,
        },
        "sources": sources,
        "warnings": warnings,
        "problems": problems,
    }


def _load_config() -> dict:
    """Merge global -> active profile -> local -> explicit runtime file.

    AX_CONFIG_FILE is intentionally last because it is an environment-selected
    runtime identity, used by channel/headless processes where CWD may contain
    unrelated project config.
    """
    merged = _load_global_config()
    user_cfg = _load_user_config()
    if user_cfg:
        merged.update(user_cfg)

    active_profile = _load_active_profile_config()
    if active_profile:
        merged.update(active_profile)
        if "principal_type" not in active_profile and (
            active_profile.get("agent_id") or active_profile.get("agent_name")
        ):
            merged["principal_type"] = "agent"

    # When running from $HOME, the "local" config is the same ~/.ax/config.toml
    # already loaded as global. Do not let that stale file override the active
    # profile we just applied.
    if _local_config_dir() != _global_config_dir():
        local_cfg = _load_local_config()
        if local_cfg:
            if _is_unsafe_user_token_agent_config(local_cfg):
                local_dir = _local_config_dir()
                config_path = (local_dir / "config.toml") if local_dir else Path.cwd() / ".ax" / "config.toml"
                _warn_ignored_unsafe_local_config(config_path)
            else:
                merged.update(local_cfg)
                if "principal_type" not in local_cfg and _has_agent_identity(local_cfg):
                    merged["principal_type"] = "agent"
    explicit_cfg = _load_runtime_config_file(os.environ.get("AX_CONFIG_FILE"))
    if explicit_cfg:
        merged.update(explicit_cfg)
        if "principal_type" not in explicit_cfg and _has_agent_identity(explicit_cfg):
            merged["principal_type"] = "agent"
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
    cfg = _load_config()
    return (
        os.environ.get("AX_TOKEN")
        or _read_token_file(os.environ.get("AX_TOKEN_FILE"))
        or cfg.get("token")
        or _read_token_file(cfg.get("token_file"))
    )


def resolve_user_token() -> str | None:
    """Resolve the user login token, ignoring agent-local runtime config."""
    token = os.environ.get("AX_USER_TOKEN")
    if token:
        return token
    cfg = _load_user_config()
    token = cfg.get("token")
    if token:
        return token
    fallback = os.environ.get("AX_TOKEN") or _load_config().get("token")
    if fallback and str(fallback).startswith("axp_u_"):
        return fallback
    return None


def resolve_base_url() -> str:
    return os.environ.get("AX_BASE_URL") or _load_config().get("base_url", "http://localhost:8001")


def resolve_gateway_config() -> dict:
    """Return repo-local Gateway identity config, if this workspace opted in.

    Gateway-native agent configs intentionally avoid readable PATs. A workspace
    opts in with:

        [gateway]
        mode = "local"
        url = "http://127.0.0.1:8765"

        [agent]
        agent_name = "codex-pass-through"

    Top-level `gateway_url` / `gateway_mode` / `agent_name` are accepted as a
    compatibility convenience for early local configs.
    """
    cfg = _load_config()
    gateway = cfg.get("gateway") if isinstance(cfg.get("gateway"), dict) else {}
    agent = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}
    mode = str(gateway.get("mode") or cfg.get("gateway_mode") or "").strip().lower()
    url = str(gateway.get("url") or gateway.get("base_url") or cfg.get("gateway_url") or "").strip()
    agent_name = str(
        agent.get("agent_name") or agent.get("name") or cfg.get("gateway_agent_name") or cfg.get("agent_name") or ""
    ).strip()
    registry_ref = str(
        agent.get("registry_ref") or agent.get("registry") or cfg.get("gateway_registry_ref") or ""
    ).strip()
    workdir = str(agent.get("workdir") or gateway.get("workdir") or cfg.get("gateway_workdir") or "").strip()
    enabled = mode in {"local", "pass_through", "gateway"} or bool(url)
    if not enabled:
        return {}
    return {
        "mode": mode or "local",
        "url": url or "http://127.0.0.1:8765",
        "agent_name": agent_name or None,
        "registry_ref": registry_ref or None,
        "workdir": workdir or None,
    }


def resolve_user_base_url() -> str:
    cfg = _load_user_config()
    return os.environ.get("AX_USER_BASE_URL") or cfg.get("base_url") or resolve_base_url()


def resolve_agent_name(*, explicit: str | None = None, client: AxClient | None = None) -> str | None:
    """Resolve agent name: explicit > env > auto-detect from single-agent scope > local config.

    Resolution order:
    1. --agent flag (explicit)
    2. AX_AGENT_NAME env var; set to none/null/empty to explicitly clear
    3. Auto-detect: if PAT is scoped to exactly 1 agent, use that
    4. Project-local .ax/config.toml agent_name
    5. None (send as user)
    """
    if explicit:
        return explicit
    env = os.environ.get("AX_AGENT_NAME")
    if env is not None:
        if env.lower() in ("", "none", "null"):
            return None
        return env

    # Project-local config (no API calls needed — fastest path)
    cfg = _load_config()
    if cfg.get("principal_type") == "user":
        return None
    if cfg.get("agent_name"):
        return cfg["agent_name"]

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


def _space_items(result: object) -> list[dict]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if not isinstance(result, dict):
        return []
    for key in ("spaces", "items", "results"):
        items = result.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _space_lookup_key(value: object) -> str:
    normalized = str(value or "").strip().lower()
    normalized = re.sub(r"[\s_]+", "-", normalized)
    return re.sub(r"-+", "-", normalized).strip("-")


def _is_uuid_like(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def resolve_space_id(client: AxClient, *, explicit: str | None = None) -> str:
    """Resolve space: explicit > env > config > bound agent default > auto-detect."""
    if explicit:
        ref = explicit.strip()
        if not ref:
            typer.echo("Error: Empty space value. Use a space id, slug, or name.", err=True)
            raise typer.Exit(1)
        if _is_uuid_like(ref):
            return ref

        spaces = _space_items(client.list_spaces())
        needle = _space_lookup_key(ref)
        matches = []
        for space in spaces:
            values = (
                space.get("id"),
                space.get("space_id"),
                space.get("slug"),
                space.get("name"),
            )
            if any(_space_lookup_key(value) == needle for value in values if value):
                matches.append(space)

        if not matches:
            typer.echo(
                f"Error: No visible space matched '{explicit}'. Use a space UUID or run `axctl spaces list`.",
                err=True,
            )
            raise typer.Exit(1)
        if len(matches) > 1:
            candidates = ", ".join(
                str(space.get("slug") or space.get("name") or space.get("id") or space.get("space_id"))
                for space in matches[:5]
            )
            typer.echo(
                f"Error: Space '{explicit}' matched multiple spaces ({candidates}). Use the space UUID.",
                err=True,
            )
            raise typer.Exit(1)

        space_id = matches[0].get("id") or matches[0].get("space_id")
        if not space_id:
            typer.echo(f"Error: Matched space '{explicit}' did not include an id.", err=True)
            raise typer.Exit(1)
        return str(space_id)
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
    space_list = _space_items(spaces)
    if len(space_list) == 1:
        return str(space_list[0].get("id", space_list[0].get("space_id")))
    if len(space_list) == 0:
        typer.echo("Error: No spaces found for this user.", err=True)
        raise typer.Exit(1)
    typer.echo(
        "Error: Multiple spaces found. Use --space/--space-id or set AX_SPACE_ID.",
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
    cfg = _load_config()
    if cfg.get("principal_type") == "user":
        return None
    return cfg.get("agent_id")


def get_client() -> AxClient:
    token = resolve_token()
    if not token:
        typer.echo(
            "Error: No API credential found. For Gateway-managed agents, use "
            "`ax gateway local ... --workdir <path>` so Gateway can broker the "
            "agent identity. If Gateway is logged out, open http://127.0.0.1:8765 "
            "or run `ax gateway login` from a trusted terminal.",
            err=True,
        )
        raise typer.Exit(1)
    base_url = resolve_base_url()
    agent_name = resolve_agent_name()
    agent_id = resolve_agent_id()

    # Verbose environment indicator: show which API you're hitting
    if os.environ.get("AX_VERBOSE", "").lower() in ("1", "true", "yes"):
        import sys
        from urllib.parse import urlparse

        host = urlparse(base_url).hostname or base_url
        sys.stderr.write(f"\033[2m[env: {host}]\033[0m\n")

    return AxClient(
        base_url=base_url,
        token=token,
        agent_name=agent_name,
        agent_id=agent_id,
    )


def get_user_client() -> AxClient:
    """Return a user-authored client for setup/management operations."""
    token = resolve_user_token()
    if not token:
        typer.echo(
            "Error: No user login found. Run 'axctl login' with a user PAT.",
            err=True,
        )
        raise typer.Exit(1)
    if token.startswith("axp_a_"):
        typer.echo(
            "Error: User login is backed by an agent PAT. Run 'axctl login' with a user PAT.",
            err=True,
        )
        raise typer.Exit(1)
    return AxClient(base_url=resolve_user_base_url(), token=token)
