"""Shared dependencies, so the app and the channel adapters both import from here
instead of from each other."""

from __future__ import annotations

from functools import lru_cache

from orca_gateway.backends.zunkiree import ZunkireeAgentBackend
from orca_gateway.config import get_settings
from orca_gateway.seam import AgentBackend


@lru_cache
def get_backend() -> AgentBackend:
    settings = get_settings()
    return ZunkireeAgentBackend(
        base_url=settings.zunkiree_base_url,
        tenant_keys=settings.tenant_key_map(),
    )
