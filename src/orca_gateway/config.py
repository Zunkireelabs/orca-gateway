from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Gateway-wide config. Anything backend-specific (URLs, per-tenant routing
    keys) lives here as data, never as a literal in a backend's own module.
    """

    model_config = SettingsConfigDict(env_prefix="ORCA_", env_file=".env")

    # Tenant config lives in Postgres (schema orca_gw). Required at runtime.
    database_url: str = ""
    # A config edit takes effect within this many seconds unless invalidated explicitly.
    tenant_cache_ttl_s: float = 15.0

    # Voice channel adapter. All required at runtime; the route fails closed if unset.
    voice_shared_secret: str = ""
    voice_debounce_ms: int = 300
    # Max backend runs in flight ACROSS conversations. The stage backend has a 2-socket pool
    # that shares a connection ceiling with production, so stay at or below it.
    voice_max_concurrent_runs: int = 1
    # Bound on one backend call (including waiting for a slot). A hung backend must not
    # hold a call open forever.
    voice_run_timeout_s: float = 25.0

    # Commit the running image was built from (set by the Docker build).
    git_sha: str = "unknown"


@lru_cache
def get_settings() -> Settings:
    return Settings()
