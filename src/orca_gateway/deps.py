"""Shared dependencies, so the app and the channel adapters both import from here
instead of from each other."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache

from orca_gateway.backends.zunkiree import ZunkireeAgentBackend
from orca_gateway.calls_repo import PgCallsRepository
from orca_gateway.config import get_settings
from orca_gateway.seam import AgentBackend
from orca_gateway.tenant_repo import PgTenantRepository
from orca_gateway.tenants import TenantStore, TenantStoreError


@lru_cache
def get_tenant_repo() -> PgTenantRepository:
    """The repository underneath get_tenant_store(), exposed directly for the console: console
    reads (fleet, config forms) and writes must never go through the 15s-TTL cache -- a save must
    be visible on the very next page load, not up to 15s later."""
    settings = get_settings()
    if not settings.database_url:
        raise TenantStoreError("ORCA_DATABASE_URL is not set")
    return PgTenantRepository(settings.database_url, schema=settings.db_schema)


@lru_cache
def get_tenant_store() -> TenantStore:
    settings = get_settings()
    if not settings.database_url:
        # Fail closed and cleanly: the adapter turns this into a 503, never a traceback.
        raise TenantStoreError("ORCA_DATABASE_URL is not set")
    return TenantStore(get_tenant_repo(), ttl_s=settings.tenant_cache_ttl_s)


@lru_cache
def get_calls_repo() -> PgCallsRepository:
    settings = get_settings()
    if not settings.database_url:
        raise TenantStoreError("ORCA_DATABASE_URL is not set")
    return PgCallsRepository(settings.database_url, schema=settings.db_schema)


@lru_cache
def get_backend() -> AgentBackend:
    return ZunkireeAgentBackend(tenants=get_tenant_store())


def now() -> datetime:
    """Current time (UTC). A function so tests can freeze it."""
    return datetime.now(UTC)
