"""Channel adapter: a website widget's chat stream -> the seam. As thin as `elevenlabs_llm.py`:
it learns nothing about what a tenant's product does, and the only thing that differs from voice
is the wire shape and the fact that this endpoint is public, anonymous and browser-facing
(P3 brief A1/A2).

Body: the subset of Zunkiree's own `QueryRequest` the widget already posts to
`/api/v1/query/stream` (`site_id`, `question`, `session_id`; `language` is accepted and ignored --
the seam has no slot for it yet, and the backend already defaults it). Anything else the widget
sends (image_data, etc.) is a product concern of Zunkiree's own product surface, never something
this gateway sees a use for.

Response: like voice, buffered to `done` (see elevenlabs_llm.py's own docstring for why the seam
itself always buffers a coalesced run to one final answer) and then re-emitted as the shape the
widget already parses: one `token` frame carrying the whole answer (the widget's `done` handler
requires a message bubble to already exist -- created by a prior `token`/etc. event -- so a
`done`-only response would silently vanish), then `done` with `answer`/`sources`/`suggestions`/
`session_id`, mirroring exactly what Zunkiree's own `/api/v1/query/stream` already sends. `tool`
and `usage` events are dropped from the wire (never something a browser tab should see) and kept
for metering only, same split as voice.

An Origin not on the tenant's chat row's `allowed_origins` is rejected with a flat 403 and no CORS
header, before any of the below, whether or not the tenant/chat row even exists (a nonexistent
tenant trivially allows no origin). Only once the origin passes does tenant availability (inactive,
disabled, kill switch) get answered with a 200 SSE `error` frame instead of an HTTP error status --
so Zunkiree's own widget fallback (its own, separate retry-once-on-the-direct-path behaviour, added
in the zunkiree-search-v1 repo, not here) never fires for a kill switch: falling back to the direct
path would defeat the kill switch entirely. Every OTHER failure (daily spend cap, an upstream
error) is a real HTTP error status before any byte of the stream, exactly like voice, so that
fallback CAN fire for those.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import time
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ValidationError, field_validator

from orca_gateway import deps
from orca_gateway.channels.elevenlabs_llm import get_coalescer, get_tenant_limiter
from orca_gateway.coalescer import ClientGoneError, StaleTurnError
from orca_gateway.messages import spoken_message
from orca_gateway.phone_guard import guard_phone_numbers
from orca_gateway.rate_limit import CallerRateLimiter
from orca_gateway.seam import Identity
from orca_gateway.tenants import (
    TenantStoreError,
    TenantUnavailableError,
    availability,
    require_serving,
)

router = APIRouter()
log = logging.getLogger("orca_gateway.channels.widget_chat")

# Bounds (P3 brief A2): a public, unauthenticated endpoint on the open internet must never trust
# the size or shape of what arrives. Not configurable via Settings -- these are safety bounds, not
# a tuning knob (same status as elevenlabs_llm.py's own regex constants).
_BODY_MAX_BYTES = 16 * 1024
_QUESTION_MAX_CHARS = 4000
_RATE_LIMIT_WINDOW_S = 60.0  # per_caller_rate_limit's only unit: turns per rolling minute.

# A single tenant-safe message for EVERY unavailability reason (unknown tenant, no chat row,
# disabled, kill switch): distinguishing them in the response would let a stranger on the internet
# enumerate tenant slugs and channel state. The real reason is still logged server-side.
_UNAVAILABLE_MESSAGE = "Sorry, chat isn't available right now. Please try again shortly."
_SESSION_LIMIT_MESSAGE = (
    "Thanks for chatting with {brand}. We've reached the limit for this conversation; "
    "please start a new one to continue."
)

_depth_counter = itertools.count(1)  # see module docstring: one call per request is enough here,
# a single process-wide monotonic counter is all `TurnCoalescer.submit()` needs (never resets,
# never keyed by conversation -- distinct requests always get distinct, increasing depths, which
# is the only property the coalescer relies on for "a newer depth supersedes an older one").

_rate_limiter: CallerRateLimiter | None = None


def get_rate_limiter() -> CallerRateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = CallerRateLimiter(window_s=_RATE_LIMIT_WINDOW_S)
    return _rate_limiter


class UpstreamError(Exception):
    pass


class DailySpendCapExceeded(Exception):
    """Same role as elevenlabs_llm.UpstreamError's sibling: one refusal decision shared across
    every duplicate request racing the same turn, rather than each computing it independently."""


@dataclass
class TurnResult:
    answer: str
    sources: list
    suggestions: list
    usage: dict | None


class WidgetChatRequest(BaseModel):
    site_id: str
    question: str
    session_id: str
    language: str | None = None  # accepted, ignored -- see module docstring

    @field_validator("site_id", "session_id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v

    @field_validator("question")
    @classmethod
    def _bounded_question(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("question must not be empty")
        if len(v) > _QUESTION_MAX_CHARS:
            raise ValueError(f"question longer than {_QUESTION_MAX_CHARS} characters")
        return v


def _client_ip(request: Request) -> str:
    """The caller's IP, trusting only the hop Traefik itself appends. Traefik is the only reverse
    proxy in front of this service (docker-compose.yml): it forwards whatever X-Forwarded-For a
    client sent and appends the address it actually accepted the connection FROM as the last
    entry. Everything left of that last entry came from the client and is trivially spoofable, so
    only the rightmost entry is ever trusted; its absence (no proxy, e.g. local dev) falls back to
    the direct peer address."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        last = xff.rsplit(",", 1)[-1].strip()
        if last:
            return last
    return request.client.host if request.client else "unknown"


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _error_stream(message: str):
    async def gen():
        yield _sse({"type": "error", "message": message})

    return gen()


@router.options("/v1/widget/stream")
async def widget_stream_preflight(request: Request) -> Response:
    # Preflight negotiates the protocol only -- it never resolves a tenant or touches the
    # backend, so there is nothing here a disallowed origin could learn by asking. The actual
    # POST enforces the tenant's own allowed_origins before doing anything real (see below).
    origin = request.headers.get("origin")
    headers = {
        "access-control-allow-methods": "POST, OPTIONS",
        "access-control-allow-headers": "content-type",
        "access-control-max-age": "600",
    }
    if origin:
        headers["access-control-allow-origin"] = origin
        headers["vary"] = "Origin"
    return Response(status_code=204, headers=headers)


@router.post("/v1/widget/stream")
async def widget_stream(request: Request):
    arrived = time.monotonic()
    # A Content-Length lie (absent, or smaller than what's actually sent) is still caught below --
    # this header check is only a fast path that avoids buffering an oversized body at all.
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > _BODY_MAX_BYTES:
        raise HTTPException(413, "payload too large")
    raw = await request.body()
    if len(raw) > _BODY_MAX_BYTES:
        raise HTTPException(413, "payload too large")
    try:
        body = WidgetChatRequest.model_validate_json(raw)
    except ValidationError:
        raise HTTPException(400, "bad request") from None

    origin = request.headers.get("origin", "")
    conversation_id = body.session_id
    depth = next(_depth_counter)

    def log_arrival(decision: str) -> None:
        log.info(
            "widget chat arrival tenant=%s conversation=%s depth=%d text_sha=%s "
            "arrived_mono=%.3f decision=%s",
            body.site_id,
            conversation_id,
            depth,
            hashlib.sha256(body.question.encode()).hexdigest()[:12],
            arrived,
            decision,
        )

    try:
        metering = deps.get_calls_repo()
    except Exception:
        metering = None
        log.warning("calls repo unavailable; metering disabled for this request")

    try:
        cfg = await deps.get_tenant_store().get(body.site_id)
    except TenantStoreError:
        log.exception("tenant config unreachable slug=%s", body.site_id)
        # The store being unreachable says nothing about which tenant or origin was asked for --
        # unlike the origin gate below, there is no allowlist to check here, and reflecting the
        # caller's own Origin leaks nothing origin-specific (same reasoning as the OPTIONS
        # preflight, which also reflects unconditionally). Without this, a browser sees a fetch
        # rejection indistinguishable from a network failure -- exactly the CORS-missing failure
        # mode this whole fix is about, just on the tenant-store-outage path instead of the
        # spend-cap one.
        store_error_headers = (
            {"access-control-allow-origin": origin, "vary": "Origin"} if origin else {}
        )
        return Response(status_code=503, content=b"", headers=store_error_headers)

    # Read the chat row directly (never through require_serving, which only RETURNS a row when
    # the tenant is fully servable) so the ORIGIN check below always has a real list to check
    # against: a disabled or kill-switched tenant still HAS a configured allowed_origins list, and
    # an unknown slug or missing chat row has an empty one -- so EVERY origin fails it, uniformly,
    # with no special case for "tenant doesn't exist" (see A2: any unlisted origin is rejected,
    # full stop; a nonexistent tenant trivially lists nothing).
    chat_row = cfg.channels.get("chat") if cfg is not None else None
    allowed_origins = chat_row.allowed_origins if chat_row is not None else []
    origin_ok = bool(origin) and origin in allowed_origins
    cors_headers = {"access-control-allow-origin": origin, "vary": "Origin"} if origin_ok else {}

    if not origin_ok:
        log.warning(
            "widget chat origin rejected tenant=%s origin=%s", body.site_id, origin or "-"
        )
        return Response(status_code=403, content=b"")

    try:
        ch = require_serving(cfg, "chat")
    except TenantUnavailableError as exc:
        log.warning("tenant refused slug=%s channel=chat reason=%s", body.site_id, exc.reason)
        if exc.reason == "kill switch on" and metering is not None:
            try:
                await metering.close_call(conversation_id, "kill_switch")
            except Exception:
                log.exception(
                    "failed to close call on kill switch conversation=%s", conversation_id
                )
        return StreamingResponse(
            _error_stream(_UNAVAILABLE_MESSAGE),
            media_type="text/event-stream",
            headers=cors_headers,
        )

    caller_ip = _client_ip(request)
    limiter = get_rate_limiter()
    if not limiter.allow(
        body.site_id, "chat", f"session:{conversation_id}", ch.per_caller_rate_limit
    ) or not limiter.allow(body.site_id, "chat", f"ip:{caller_ip}", ch.per_caller_rate_limit):
        log.warning(
            "widget chat rate limited tenant=%s conversation=%s ip=%s",
            body.site_id,
            conversation_id,
            caller_ip,
        )
        return Response(status_code=429, content=b"", headers=cors_headers)

    backend = deps.get_backend()

    async def _metering(what: str, call) -> object | None:
        if metering is None:
            return None
        try:
            return await call()
        except Exception:
            log.exception(
                "metering %s failed slug=%s conversation=%s", what, body.site_id, conversation_id
            )
            return None

    async def touch():
        return await _metering(
            "touch_call",
            lambda: metering.touch_call(
                tenant_slug=body.site_id,
                channel="chat",
                conversation_id=conversation_id,
                agent_id=ch.agent_id,
                elevenlabs_agent_id=ch.elevenlabs_agent_id,
            ),
        )

    async def complete(
        *,
        usage: dict | None,
        answer: str | None,
        tools: list[dict] | None = None,
        ended_by: str | None = None,
    ) -> None:
        latency_ms = int((time.monotonic() - arrived) * 1000)
        await _metering(
            "complete_turn",
            lambda: metering.complete_turn(
                conversation_id=conversation_id,
                depth=depth,
                usage=usage,
                user_text=body.question,
                answer_text=answer,
                tools=tools,
                latency_ms=latency_ms,
                ended_by=ended_by,
            ),
        )

    async def abandon() -> None:
        await _metering(
            "record_abandoned_run",
            lambda: asyncio.shield(metering.record_abandoned_run(conversation_id)),
        )

    async def work(text: str) -> TurnResult:
        # Mirrors elevenlabs_llm.work() exactly: daily_spend_cap gates a call's FIRST turn only,
        # metering is an add-on that fails open, a turn is counted once (on completion, never on
        # start or per raw HTTP request).
        existing_call = None
        if metering is not None:
            try:
                existing_call = await metering.get_open_call(conversation_id)
            except Exception:
                log.exception("metering unreachable checking call state slug=%s", body.site_id)
            if existing_call is not None and existing_call.ended_reason == "daily_spend_cap":
                await complete(usage=None, answer=None, ended_by="daily_spend_cap")
                raise DailySpendCapExceeded()
            if existing_call is None and ch.daily_spend_cap is not None:
                try:
                    spend = await metering.daily_spend_usd(body.site_id)
                except Exception:
                    spend = None
                    log.exception("metering unreachable reading daily spend slug=%s", body.site_id)
                if spend is not None and spend >= ch.daily_spend_cap:
                    await _metering(
                        "refuse_call",
                        lambda: metering.refuse_call(
                            tenant_slug=body.site_id,
                            channel="chat",
                            conversation_id=conversation_id,
                            agent_id=ch.agent_id,
                            elevenlabs_agent_id=ch.elevenlabs_agent_id,
                        ),
                    )
                    await complete(usage=None, answer=None, ended_by="daily_spend_cap")
                    raise DailySpendCapExceeded()

        if existing_call is not None and ch.max_session_seconds is not None:
            elapsed = (deps.now() - existing_call.started_at).total_seconds()
            if elapsed > ch.max_session_seconds:
                await _metering(
                    "close_call", lambda: metering.close_call(conversation_id, "max_session")
                )
                limit_text = _SESSION_LIMIT_MESSAGE.replace("{brand}", ch.spoken_brand_name)
                await complete(usage=None, answer=limit_text, ended_by="max_session")
                return TurnResult(answer=limit_text, sources=[], suggestions=[], usage=None)

        await touch()
        answer_tokens: list[str] = []
        answer: str | None = None
        sources: list = []
        suggestions: list = []
        usage: dict | None = None
        tools: list[dict] = []
        completed = False
        try:
            async for event in backend.session(
                agent_id=ch.agent_id,
                channel="chat",
                identity=Identity(authority="anonymous"),
                tenant=body.site_id,
                turn=text,
                conversation_id=conversation_id,
            ):
                if event.type == "token":
                    answer_tokens.append(event.data.get("text", ""))
                elif event.type == "done":
                    answer = event.data.get("answer", "")
                    sources = event.data.get("sources", [])
                    suggestions = event.data.get("suggestions", [])
                elif event.type == "usage":
                    usage = event.data
                elif event.type == "tool":
                    tools.append(
                        {"name": event.data.get("name", ""), "status": event.data.get("status", "")}
                    )
                elif event.type == "error":
                    raise UpstreamError(event.data.get("message", ""))
            text_out = answer if answer is not None else "".join(answer_tokens)
            if not text_out.strip():
                raise UpstreamError("empty answer")
            completed = True
        finally:
            if not completed:
                await abandon()
        await complete(usage=usage, answer=text_out, tools=tools)
        return TurnResult(answer=text_out, sources=sources, suggestions=suggestions, usage=usage)

    available = availability(cfg, ch, deps.now())
    if not available.open and ch.out_of_hours_behaviour == "say_closed":
        # Same channel-level decision as voice (A3): chat uses the tenant's own hours, but the
        # DEFAULT out_of_hours_behaviour ("handoff_anyway") already means "answer anyway" for
        # chat too -- only a tenant that explicitly sets say_closed reaches this branch.
        log_arrival("not_coalesced")
        closed_text = (ch.out_of_hours_message or "").replace("{brand}", ch.spoken_brand_name)
        await touch()
        await complete(usage=None, answer=closed_text, ended_by="out_of_hours")
        result = TurnResult(answer=closed_text, sources=[], suggestions=[], usage=None)
    else:
        try:
            result = await get_coalescer().submit(
                conversation_id,
                depth,
                body.question,
                work,
                request.is_disconnected,
                on_decision=log_arrival,
                # P2 brief A5, reused verbatim (P3 brief A1): this tenant's own cap, under the
                # SAME environment ceiling voice shares -- the module-level singletons imported
                # from elevenlabs_llm above, never a chat-only copy of either.
                slot=get_tenant_limiter().slot(body.site_id, "chat", ch.max_concurrent_runs),
            )
        except DailySpendCapExceeded:
            # Must carry cors_headers, not just a bare HTTPException: without them the browser's
            # fetch() rejects on the CORS failure before JS ever sees a status code, which is
            # indistinguishable from a network error -- and the widget's own fallback treats any
            # fetch rejection like a 5xx, retrying straight to the direct Zunkiree path. That
            # would bypass the daily spend cap entirely (see module docstring).
            return Response(status_code=403, content=b"", headers=cors_headers)
        except ClientGoneError:
            return Response(status_code=499)
        except StaleTurnError:
            result = TurnResult(answer="", sources=[], suggestions=[], usage=None)
        except Exception:
            log.exception(
                "chat turn failed conversation=%s depth=%s", conversation_id, depth
            )
            return Response(status_code=502, content=b"", headers=cors_headers)

    # Chat is buffered end-to-end (see module docstring): the caller sees nothing until `result`
    # is fully ready, unlike voice's token-by-token stream. This is exactly the time that
    # buffering costs a chat user -- logged separately from `complete()`'s `latency_ms` (which
    # metering stores per call, not per turn, and isn't grep-able from app logs alone).
    answer_wait_ms = int((time.monotonic() - arrived) * 1000)
    log.info(
        "widget chat answer wait tenant=%s conversation=%s depth=%d wait_ms=%d",
        body.site_id,
        conversation_id,
        depth,
        answer_wait_ms,
    )

    answer, suggestions = result.answer, result.suggestions
    if ch.phone_guard:
        # P4 A3: the same pass voice runs, before the token and done frames. The answer and the
        # follow-up suggestions are both text the visitor reads; the stored transcript keeps the
        # model's own text (as voice does). Counts only in the log, never a digit.
        replacement = spoken_message("phone_guard", ch)
        guarded = guard_phone_numbers(answer, ch.allowed_phone_numbers, replacement)
        replaced = guarded.replaced
        answer = guarded.text
        clean: list = []
        for suggestion in suggestions:
            if isinstance(suggestion, str):
                g = guard_phone_numbers(suggestion, ch.allowed_phone_numbers, replacement)
                replaced += g.replaced
                suggestion = g.text
            clean.append(suggestion)
        suggestions = clean
        if replaced:
            log.warning(
                "[PHONE-GUARD] replaced tenant=%s channel=chat count=%d conversation=%s depth=%d",
                body.site_id,
                replaced,
                conversation_id,
                depth,
            )

    async def stream():
        # One token frame carrying the whole answer, then done -- see module docstring for why
        # (the widget's `done` handler updates an existing message bubble; only a prior event
        # creates one). No fabricated intermediate deltas: same "the answer is a single unit"
        # buffering voice already does, in the shape chat's wire already expects.
        yield _sse({"type": "token", "data": answer})
        yield _sse(
            {
                "type": "done",
                "answer": answer,
                "sources": result.sources,
                "suggestions": suggestions,
                "session_id": conversation_id,
            }
        )

    return StreamingResponse(stream(), media_type="text/event-stream", headers=cors_headers)
