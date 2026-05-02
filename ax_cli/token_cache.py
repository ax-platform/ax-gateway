"""Token exchange and caching (AUTH-SPEC-001 §13).

PAT → exchange → short-lived JWT → cache → use on all API calls.
PAT never touches business endpoints. Only sent to /auth/exchange.

Cache key: sha256(pat_key_id + token_class + agent_id + audience + scope)
Cache file: separate from PAT config, permissions 0600.
"""

import hashlib
import json
import logging
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Buffer before expiry to trigger refresh (seconds)
_REFRESH_BUFFER = 30


def _project_cache_dir() -> Path | None:
    """Find project-level .ax/ directory (where config.toml lives).

    Each agent has its own directory and config — cache lives there too.
    Never falls back to ~/.ax/ since multiple agents share the machine.
    """
    cur = Path.cwd()
    for parent in [cur, *cur.parents]:
        ax_dir = parent / ".ax"
        if ax_dir.is_dir() and (ax_dir / "config.toml").exists():
            cache = ax_dir / "cache"
            cache.mkdir(exist_ok=True)
            return cache
    return None


def _cache_dir() -> Path:
    """Project-level .ax/cache/ — falls back to CWD/.ax/cache/ if no project found."""
    project = _project_cache_dir()
    if project:
        return project
    # Last resort: create .ax/cache in CWD
    d = Path.cwd() / ".ax" / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_key(
    pat_key_id: str,
    token_class: str,
    agent_id: str | None,
    audience: str,
    scope: str,
    agent_name: str | None = None,
) -> str:
    """Deterministic cache key per AUTH-SPEC-001 §13.

    ``agent_name`` is included for the Gateway-first enrollment/bind stub where
    the backend accepts ``agent_name`` instead of an existing ``agent_id``.
    """
    raw = f"{pat_key_id}:{token_class}:{agent_id or ''}:{agent_name or ''}:{audience}:{scope}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _extract_key_id(pat: str) -> str | None:
    """Extract key_id from axp_X_KEYID.SECRET format."""
    if not pat.startswith("axp_"):
        return None
    rest = pat[6:]  # skip axp_X_
    dot = rest.find(".")
    if dot < 1:
        return None
    return rest[:dot]


class TokenExchanger:
    """Transparent PAT→JWT exchange with caching.

    Usage:
        exchanger = TokenExchanger(base_url, pat)
        jwt = exchanger.get_token("user_access", scope="messages tasks")
        # jwt is cached and auto-refreshes
    """

    def __init__(self, base_url: str, pat: str):
        self.base_url = base_url.rstrip("/")
        self.pat = pat
        self.pat_key_id = _extract_key_id(pat)
        self._cache: dict[str, dict] = {}  # in-memory cache
        self._cache_dir = _cache_dir()
        self._load_disk_cache()

    def _load_disk_cache(self) -> None:
        """Load cached tokens from disk."""
        cache_file = self._cache_dir / "tokens.json"
        if cache_file.exists():
            try:
                # Verify permissions
                mode = cache_file.stat().st_mode & 0o777
                if mode != 0o600:
                    logger.warning("Token cache has wrong permissions %o, removing", mode)
                    cache_file.unlink()
                    return
                data = json.loads(cache_file.read_text())
                # Only load entries for this PAT
                if isinstance(data, dict):
                    self._cache = {
                        k: v for k, v in data.items() if isinstance(v, dict) and v.get("pat_key_id") == self.pat_key_id
                    }
            except (json.JSONDecodeError, OSError):
                pass

    def _save_disk_cache(self) -> None:
        """Persist cache to disk with 0600 permissions."""
        cache_file = self._cache_dir / "tokens.json"
        # Merge with existing entries from other PATs
        existing = {}
        if cache_file.exists():
            try:
                existing = json.loads(cache_file.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        existing.update(self._cache)
        # Prune expired entries
        now = time.time()
        pruned = {k: v for k, v in existing.items() if v.get("exp", 0) > now}
        cache_file.write_text(json.dumps(pruned))
        cache_file.chmod(0o600)

    def get_token(
        self,
        token_class: str = "user_access",
        *,
        agent_id: str | None = None,
        agent_name: str | None = None,
        audience: str = "ax-api",
        scope: str = "messages tasks context agents spaces search",
        requested_ttl: int | None = None,
        resource: str | None = None,
        force_refresh: bool = False,
    ) -> str:
        """Get a valid JWT, using cache or exchanging if needed."""
        if not self.pat_key_id:
            raise ValueError("Cannot extract key_id from PAT — invalid format")

        key = _cache_key(self.pat_key_id, token_class, agent_id, audience, scope, agent_name)

        # Check cache (skip if force_refresh)
        if not force_refresh:
            cached = self._cache.get(key)
            if cached and cached.get("exp", 0) > time.time() + _REFRESH_BUFFER:
                return cached["access_token"]

        # Exchange
        jwt_data = self._exchange(
            token_class,
            agent_id=agent_id,
            agent_name=agent_name,
            audience=audience,
            scope=scope,
            requested_ttl=requested_ttl,
            resource=resource,
        )
        entry = {
            "access_token": jwt_data["access_token"],
            "exp": time.time() + jwt_data["expires_in"],
            "token_class": token_class,
            "pat_key_id": self.pat_key_id,
            "agent_id": jwt_data.get("agent_id") or agent_id,
            "agent_name": jwt_data.get("agent_name") or agent_name,
            "scope": jwt_data.get("scope") or scope,
        }
        self._cache[key] = entry
        self._save_disk_cache()
        return jwt_data["access_token"]

    def _exchange(
        self,
        token_class: str,
        *,
        agent_id: str | None,
        agent_name: str | None,
        audience: str,
        scope: str,
        requested_ttl: int | None,
        resource: str | None,
    ) -> dict:
        """POST /auth/exchange — PAT in, JWT out."""
        body: dict = {
            "requested_token_class": token_class,
            "audience": audience,
            "scope": scope,
        }
        if requested_ttl is not None:
            body["requested_ttl"] = requested_ttl
        if resource:
            body["resource"] = resource
        if agent_id:
            body["agent_id"] = agent_id
        elif agent_name:
            body["agent_name"] = agent_name

        r = httpx.post(
            f"{self.base_url}/auth/exchange",
            json=body,
            headers={
                "Authorization": f"Bearer {self.pat}",
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )
        r.raise_for_status()
        return r.json()

    def clear_cache(self) -> None:
        """Discard all cached tokens for this PAT."""
        self._cache.clear()
        self._save_disk_cache()
