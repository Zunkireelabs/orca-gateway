from __future__ import annotations

import json
import logging
import uuid

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from orca_gateway.backends.zunkiree import UnknownTenantError
from orca_gateway.channels import elevenlabs_llm
from orca_gateway.deps import get_backend
from orca_gateway.seam import Channel, Identity

logger = logging.getLogger("orca_gateway.main")

app = FastAPI(title="orca-gateway")
app.include_router(elevenlabs_llm.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


class TurnRequest(BaseModel):
    agent_id: str
    channel: Channel
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
        except Exception:
            # Never leak backend detail (vendor, URL, payload shape) into the
            # event body — that's the seam's whole point. Log it server-side.
            logger.exception(
                "backend session failed mid-stream tenant=%s channel=%s",
                request.tenant,
                request.channel,
            )
            error_event = {"type": "error", "message": "upstream agent unavailable"}
            yield f"data: {json.dumps(error_event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
