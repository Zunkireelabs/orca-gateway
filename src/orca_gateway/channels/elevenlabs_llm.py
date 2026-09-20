"""Channel adapter: a voice platform's "custom LLM" client -> the seam.

The platform speaks the OpenAI Chat Completions wire shape. That shape, and the platform's
habit of resending full history and racing several requests per spoken turn, live here and
nowhere else. Everything below this module speaks the seam (tenant, agent, channel, turn).

Stream vs buffer: BUFFER to `done`. Token events are provisional; `done.answer` is the
authoritative text (final-answer corrections such as a phone-number sanitizer land there).
Streaming tokens to a text-to-speech engine would speak text that can no longer be
un-spoken. Buffering costs time-to-first-audio (the whole agent turn) and buys two things:
the spoken text is always the corrected text, and an upstream failure is a clean HTTP error
before any audio starts, never a half-spoken sentence.
"""

from __future__ import annotations

import hmac
import json
import re
import time
import uuid
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from orca_gateway.coalescer import ClientGoneError, StaleTurnError, TurnCoalescer
from orca_gateway.config import get_settings
from orca_gateway.seam import Identity

router = APIRouter()

_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")
_coalescer: TurnCoalescer | None = None


def get_coalescer() -> TurnCoalescer:
    global _coalescer
    if _coalescer is None:
        _coalescer = TurnCoalescer(debounce_s=get_settings().voice_debounce_ms / 1000)
    return _coalescer


class UpstreamError(Exception):
    pass


@dataclass
class TurnResult:
    answer: str
    usage: dict | None


def _authorize(request: Request) -> None:
    # The platform sends an Authorization header even when no key is configured, so its
    # presence proves nothing: the value must match our own secret. Fail closed if unset.
    secret = get_settings().voice_shared_secret
    if not secret:
        raise HTTPException(503, "voice adapter not configured")
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied, f"Bearer {secret}"):
        raise HTTPException(401, "unauthorized")


def _conversation_id(request: Request) -> str:
    match = _TRACEPARENT.match(request.headers.get("traceparent", ""))
    if not match or set(match.group(1)) == {"0"}:
        raise HTTPException(
            400, "missing or invalid traceparent; refusing to derive a conversation id"
        )
    return match.group(1)


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if isinstance(content, str) and content.strip():
            return content
    raise HTTPException(400, "no user turn in messages")


def _chunk(completion_id: str, model: str, **choice) -> str:
    body = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, **choice}],
    }
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


@router.post("/chat/completions")
async def chat_completions(request: Request):
    _authorize(request)
    conversation_id = _conversation_id(request)
    body = await request.json()
    if body.get("stream") is not True:
        raise HTTPException(400, "only stream=true is supported")
    messages = body.get("messages") or []
    turn = _last_user_text(messages)
    depth = len(messages)  # reliable turn index; trace-id alone cannot separate duplicates

    settings = get_settings()
    if not settings.voice_tenant:
        raise HTTPException(503, "voice adapter not configured")

    from orca_gateway import main as main_module  # late: main includes this router

    backend = main_module.get_backend()

    async def work(text: str) -> TurnResult:
        answer_tokens: list[str] = []
        answer: str | None = None
        usage: dict | None = None
        async for event in backend.session(
            agent_id=settings.voice_agent_id,
            channel="voice",
            identity=Identity(authority="anonymous"),
            tenant=settings.voice_tenant,
            turn=text,
            conversation_id=conversation_id,
        ):
            if event.type == "token":
                answer_tokens.append(event.data.get("text", ""))
            elif event.type == "done":
                answer = event.data.get("answer", "")
            elif event.type == "usage":
                usage = event.data
            elif event.type == "error":
                raise UpstreamError(event.data.get("message", ""))
        text_out = answer if answer is not None else "".join(answer_tokens)
        if not text_out.strip():
            raise UpstreamError("empty answer")
        return TurnResult(answer=text_out, usage=usage)

    try:
        result = await get_coalescer().submit(
            conversation_id, depth, turn, work, request.is_disconnected
        )
    except ClientGoneError:
        return Response(status_code=499)
    except StaleTurnError:
        raise HTTPException(409, "superseded by a later turn") from None
    except Exception:
        import logging

        logging.getLogger("orca_gateway.channels.elevenlabs_llm").exception(
            "voice turn failed conversation=%s depth=%s", conversation_id, depth
        )
        raise HTTPException(502, "upstream agent unavailable") from None

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    model = body.get("model", "orca")

    async def stream():
        yield _chunk(
            completion_id,
            model,
            delta={"role": "assistant", "content": result.answer},
            finish_reason=None,
        )
        yield _chunk(completion_id, model, delta={}, finish_reason="stop")
        if result.usage:  # only when the backend reported real numbers; never fabricate zeros
            usage_body = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [],
                "usage": result.usage,
            }
            yield f"data: {json.dumps(usage_body)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
