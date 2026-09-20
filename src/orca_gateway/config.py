from __future__ import annotations

import json
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Gateway-wide config. Anything backend-specific (URLs, per-tenant routing
    keys) lives here as data, never as a literal in a backend's own module.
    """

    model_config = SettingsConfigDict(env_prefix="ORCA_", env_file=".env")

    zunkiree_base_url: str = "https://staging-api.zunkireelabs.com"
    # JSON object mapping our `tenant` id -> the backend's own tenant key.
    # e.g. '{"my-tenant": "some-backend-tenant-key"}'
    zunkiree_tenant_keys: str = "{}"

    # Voice channel adapter. All required at runtime; the route fails closed if unset.
    voice_tenant: str = ""
    voice_agent_id: str = "default"
    voice_shared_secret: str = ""
    voice_debounce_ms: int = 300
    # Max backend runs in flight ACROSS conversations. The stage backend has a 2-socket pool
    # that shares a connection ceiling with production, so stay at or below it.
    voice_max_concurrent_runs: int = 1
    # Bound on one backend call (including waiting for a slot). A hung backend must not
    # hold a call open forever.
    voice_run_timeout_s: float = 25.0

    def tenant_key_map(self) -> dict[str, str]:
        return json.loads(self.zunkiree_tenant_keys)


@lru_cache
def get_settings() -> Settings:
    return Settings()
