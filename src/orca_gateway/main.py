from __future__ import annotations

import json
import uuid
from functools import lru_cache

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from orca_gateway.backends.zunkiree import UnknownTenantError, ZunkireeAgentBackend
from orca_gateway.config import get_settings
from orca_gateway.seam import AgentBackend, Identity

app = FastAPI(title="orca-gateway")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@lru_cache
def get_backend() -> AgentBackend:
    settings = get_settings()
    return ZunkireeAgentBackend(
        base_url=settings.zunkiree_base_url,
        tenant_keys=settings.tenant_key_map(),
    )


class TurnRequest(BaseModel):
    agent_id: str
    channel: str
    identity: Identity
    tenant: str
    turn: str
    conversation_id: str | None = None


@app.post("/v1/turn")
async def submit_turn(request: TurnRequest) -> StreamingResponse:
    """Channel-agnostic entry point: run one turn through the seam and stream
    the events back as SSE. This is what a channel adapter (voice, chat, ...)
    calls — it never talks to a backend directly.
    """
    backend = get_backend()
    conversation_id = request.conversation_id or str(uuid.uuid4())

    async def event_stream():
        try:
            async for event in backend.session(
                agent_id=request.agent_id,
                channel=request.channel,
                identity=request.identity,
                tenant=request.tenant,
                turn=request.turn,
                conversation_id=conversation_id,
            ):
                yield f"data: {json.dumps({'type': event.type, **event.data})}\n\n"
        except UnknownTenantError:
            yield f"data: {json.dumps({'type': 'error', 'message': 'unknown tenant'})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
