from __future__ import annotations

from fastapi import FastAPI

from orca_gateway.channels import elevenlabs_llm
from orca_gateway.config import get_settings

# No interactive docs or schema: the public surface is exactly what the reverse proxy allows.
app = FastAPI(title="orca-gateway", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(elevenlabs_llm.router)


@app.get("/health")
def health() -> dict[str, str]:
    # Unauthenticated and cheap. It must NOT touch a backend: a probing health check
    # would compete for the (deliberately tiny) backend run cap.
    return {"status": "ok", "sha": get_settings().git_sha}
