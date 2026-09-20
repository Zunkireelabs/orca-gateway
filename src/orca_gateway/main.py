from __future__ import annotations

from fastapi import FastAPI

from orca_gateway.channels import elevenlabs_llm

app = FastAPI(title="orca-gateway")
app.include_router(elevenlabs_llm.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
