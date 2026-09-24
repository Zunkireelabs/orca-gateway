from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from orca_gateway import deps
from orca_gateway import sweep as sweep_module
from orca_gateway.channels import elevenlabs_llm, widget_chat
from orca_gateway.config import get_settings
from orca_gateway.console import router as console_router
from orca_gateway.tenants import TenantStoreError

# uvicorn configures only its own loggers, so without a handler here every INFO line from this
# package (the per-request arrival log, the idle sweep) is silently dropped.
_pkg_logger = logging.getLogger("orca_gateway")
if not _pkg_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    _pkg_logger.addHandler(_handler)
    _pkg_logger.setLevel(logging.INFO)

logger = logging.getLogger("orca_gateway.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    task: asyncio.Task | None = None
    if settings.database_url:
        task = asyncio.create_task(
            sweep_module.run_forever(
                deps.get_calls_repo(),
                idle_s=settings.metering_idle_timeout_s,
                interval_s=settings.metering_sweep_interval_s,
                retention_days=settings.metering_turn_retention_days,
            )
        )
    else:
        # No database configured (e.g. some test setups): metering is simply off, not a crash.
        logger.warning("ORCA_DATABASE_URL unset; idle-timeout sweep not started")
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, TenantStoreError):
                pass


# No interactive docs or schema: the public surface is exactly what the reverse proxy allows.
app = FastAPI(
    title="orca-gateway", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
)
app.include_router(elevenlabs_llm.router)
# P3 brief A1: the widget's chat stream, a second thin channel adapter. Public/anonymous -- its
# own auth is the tenant's allowed_origins allowlist (checked inside the router), not a shared
# secret (never in a browser-shipped page). Traefik must route POST/OPTIONS /v1/widget/stream to
# this service (docker-compose.yml), same convention as /chat/completions.
app.include_router(widget_chat.router)
# The console (S6 PR 2). Auth is enforced inside the router (console_auth.require_session), not
# by omission here: every /console/* route depends on a valid session cookie, never 200 without
# one. Traefik must also route /console/* to this service (docker-compose.yml) -- the public
# surface pinned in S3b (POST /chat/completions + GET /health) is otherwise all it forwards.
app.include_router(console_router)


@app.get("/health")
def health() -> dict[str, str]:
    # Unauthenticated and cheap. It must NOT touch a backend: a probing health check
    # would compete for the (deliberately tiny) backend run cap.
    return {"status": "ok", "sha": get_settings().git_sha}
