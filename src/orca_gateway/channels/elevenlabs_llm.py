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
import logging
import re
import time
import uuid
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from orca_gateway import deps
from orca_gateway.coalescer import ClientGoneError, StaleTurnError, TurnCoalescer
from orca_gateway.config import get_settings
from orca_gateway.seam import Identity
from orca_gateway.tenants import (
    SLUG_PATTERN,
    TenantStoreError,
    TenantUnavailableError,
    availability,
    require_serving,
)

router = APIRouter()

_SLUG = re.compile(SLUG_PATTERN)
_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")
# max_session_seconds exceeded (S5 brief §3.4): a call-center-generic message, not tenant-authored
# like out_of_hours_message, since there is nothing product-specific about a session time limit.
_SESSION_LIMIT_MESSAGE = (
    "Thank you for calling {brand}. We've reached the time limit for this call; "
    "please call back to continue."
)
_coalescer: TurnCoalescer | None = None


def get_coalescer() -> TurnCoalescer:
    global _coalescer
    if _coalescer is None:
        settings = get_settings()
        _coalescer = TurnCoalescer(
            debounce_s=settings.voice_debounce_ms / 1000,
            max_concurrent_runs=settings.voice_max_concurrent_runs,
            run_timeout_s=settings.voice_run_timeout_s,
        )
    return _coalescer


class UpstreamError(Exception):
    pass


class DailySpendCapExceeded(Exception):
    """Raised inside work() so the coalescer can share ONE refusal decision across every
    duplicate raw request racing the same turn, rather than each computing (and DB-reading) it
    independently ahead of the coalescer's own race."""


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


def _tenant_slug(request: Request) -> str:
    """Which tenant this request is for. Set per agent in the platform's "Request headers".
    There is NO default tenant: a missing or malformed header is a client error, never a guess."""
    slug = request.headers.get("x-orca-tenant", "")
    if not _SLUG.match(slug):
        raise HTTPException(400, "missing or invalid X-Orca-Tenant header")
    return slug


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

    slug = _tenant_slug(request)
    log = logging.getLogger("orca_gateway.channels.elevenlabs_llm")
    # Metering is a add-on to serving, never a precondition for it: if it cannot even be
    # constructed (e.g. no database configured), every check below degrades to "skip metering"
    # rather than turning into an unrelated 500 on every call.
    try:
        metering = deps.get_calls_repo()
    except Exception:
        metering = None
        log.warning("calls repo unavailable; metering disabled for this request")
    try:
        cfg = await deps.get_tenant_store().get(slug)
        ch = require_serving(cfg, "voice")
    except TenantUnavailableError as exc:
        # Fail CLOSED and uniformly (no slug enumeration): unknown, inactive, disabled, killed.
        log.warning("tenant refused slug=%s reason=%s", slug, exc.reason)
        if exc.reason == "kill switch on" and metering is not None:
            # A deterministic, definitive signal that this call is over -- close it now rather
            # than waiting for the idle-timeout sweep (see 0002_calls.sql).
            try:
                await metering.close_call(conversation_id, "kill_switch")
            except Exception:
                log.exception(
                    "failed to close call on kill switch conversation=%s", conversation_id
                )
        raise HTTPException(403, "tenant unavailable") from None
    except TenantStoreError:
        log.exception("tenant config unreachable slug=%s", slug)
        raise HTTPException(503, "tenant config unavailable") from None

    backend = deps.get_backend()

    async def record_turn() -> None:
        if metering is None:
            return
        try:
            await metering.record_turn(
                tenant_slug=slug,
                channel="voice",
                conversation_id=conversation_id,
                agent_id=ch.agent_id,
                elevenlabs_agent_id=ch.elevenlabs_agent_id,
            )
        except Exception:
            log.exception(
                "metering record_turn failed slug=%s conversation=%s", slug, conversation_id
            )

    async def work(text: str) -> TurnResult:
        # Deliberately the ONLY place daily_spend_cap / max_session_seconds are read and
        # record_turn is written: the coalescer guarantees `work()` runs exactly once per real
        # turn, no matter how many duplicate raw HTTP requests the platform fans a spoken turn
        # into (S1). Doing these DB round trips in the handler BEFORE calling
        # `get_coalescer().submit()` would run once per duplicate (inflating turn_count) AND add
        # variable per-duplicate latency ahead of the very race the coalescer exists to resolve
        # deterministically -- regression-tested by test_fan_out_of_four_variants_makes_exactly
        # _one_backend_call, which is timing-sensitive to exactly this.
        #
        # daily_spend_cap only gates a call's FIRST turn (no row yet) -- an already-running call
        # is never cut off mid-conversation by a cap it started under (S5 brief §3.4). Metering
        # being unreachable fails OPEN: a metering outage must not become a full serving outage
        # on top of it, the same call the tenant store's own stale-cache grace period makes
        # elsewhere.
        existing_call = None
        if metering is not None:
            try:
                existing_call = await metering.get_open_call(conversation_id)
            except Exception:
                log.exception("metering unreachable checking call state slug=%s", slug)
            if existing_call is None and ch.daily_spend_cap is not None:
                try:
                    spend = await metering.daily_spend_usd(slug)
                except Exception:
                    spend = None
                    log.exception("metering unreachable reading daily spend slug=%s", slug)
                if spend is not None and spend >= ch.daily_spend_cap:
                    log.warning(
                        "tenant refused slug=%s reason=daily_spend_cap spend=%.6f cap=%.2f",
                        slug,
                        spend,
                        ch.daily_spend_cap,
                    )
                    raise DailySpendCapExceeded()

        if existing_call is not None and ch.max_session_seconds is not None:
            elapsed = (deps.now() - existing_call.started_at).total_seconds()
            if elapsed > ch.max_session_seconds:
                # A clean handoff-style response, never a hard error mid-sentence (S5 brief
                # §3.4). The call itself stays open -- closed later by the idle-timeout sweep
                # once the caller actually stops talking, not unilaterally by this one turn.
                await record_turn()
                text = _SESSION_LIMIT_MESSAGE.replace("{brand}", ch.spoken_brand_name)
                return TurnResult(answer=text, usage=None)

        await record_turn()
        answer_tokens: list[str] = []
        answer: str | None = None
        usage: dict | None = None
        async for event in backend.session(
            agent_id=ch.agent_id,
            channel="voice",
            identity=Identity(authority="anonymous"),
            tenant=slug,
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

    available = availability(cfg, ch, deps.now())
    if not available.open and ch.out_of_hours_behaviour == "say_closed":
        # Channel-level decision: the tenant chose to say it is closed rather than let the agent
        # pick up. The backend is never called, so nothing can be offered or promised. Not
        # coalesced (pre-dates S5): a duplicate raw request here records more than one turn, an
        # acceptable, low-stakes inaccuracy since no backend cost is ever attached to this path.
        await record_turn()
        text = (ch.out_of_hours_message or "").replace("{brand}", ch.spoken_brand_name)
        result = TurnResult(answer=text, usage=None)
    else:
        try:
            result = await get_coalescer().submit(
                conversation_id, depth, turn, work, request.is_disconnected
            )
        except DailySpendCapExceeded:
            raise HTTPException(403, "tenant unavailable") from None
        except ClientGoneError:
            return Response(status_code=499)
        except StaleTurnError:
            # The conversation has moved past this turn. A non-2xx would be retried at the same
            # depth and stay stale forever, so end it with a benign empty completion instead.
            result = TurnResult(answer="", usage=None)
        except Exception:
            log.exception("voice turn failed conversation=%s depth=%s", conversation_id, depth)
            raise HTTPException(502, "upstream agent unavailable") from None

    if result.usage and metering is not None:
        try:
            await metering.record_usage(
                conversation_id=conversation_id,
                model=result.usage.get("model", ""),
                prompt_tokens=result.usage.get("prompt_tokens", 0),
                completion_tokens=result.usage.get("completion_tokens", 0),
            )
        except Exception:
            log.exception("metering record_usage failed conversation=%s", conversation_id)

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
