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

    def tenant_key_map(self) -> dict[str, str]:
        return json.loads(self.zunkiree_tenant_keys)


@lru_cache
def get_settings() -> Settings:
    return Settings()
