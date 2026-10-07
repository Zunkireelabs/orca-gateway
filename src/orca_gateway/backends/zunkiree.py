"""The ONLY place a backend's request/response shape or tenant-key vocabulary is
allowed to exist. Nothing above this module may know it, or that this backend is
even Zunkiree — that's what makes it swappable.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import httpx

from orca_gateway.seam import Channel, Identity, TurnEvent
from orca_gateway.tenants import TenantStore, availability, require_serving

logger = logging.getLogger("orca_gateway.backends.zunkiree")

# P.UI1: `ui` is an opaque structured-data passthrough (vision §2.1/§8.10) -- this gateway must
# never learn what a service or a card is. The only guardrail that belongs here is a size cap, not
# a schema: an oversized `ui` is dropped and logged rather than forwarded or rejecting the turn.
_UI_MAX_BYTES = 32 * 1024


class ZunkireeAgentBackend:
    """Calls a Zunkiree-hosted agent's streaming query endpoint."""

    def __init__(
        self,
        *,
        tenants: TenantStore,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._tenants = tenants
        self._clock = clock
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def session(
        self,
        *,
        agent_id: str,
        channel: Channel,
        identity: Identity,
        tenant: str,
        turn: str,
        conversation_id: str,
    ) -> AsyncIterator[TurnEvent]:
        # Defence in depth: the channel gate has already run, but this backend never serves a
        # tenant that is unknown, inactive, disabled or killed, whatever called it.
        cfg = await self._tenants.get(tenant)
        ch = require_serving(cfg, channel)
        assert cfg is not None
        if cfg.backend != "zunkiree":
            raise ValueError(f"tenant {tenant!r} is not bound to this backend")
        site_id = cfg.backend_config.get("site_id")
        base_url = cfg.backend_config.get("base_url")
        if not site_id or not base_url:
            raise ValueError(f"tenant {tenant!r} backend_config needs site_id and base_url")
        if not conversation_id:
            raise ValueError("conversation_id must be a non-empty, caller-scoped id")

        payload = {
            "site_id": site_id,
            "question": turn,
            "session_id": conversation_id,
            "channel": channel,
            # P4 A2: tenant context the brain cannot know on its own, from the same channel row
            # the adapter already gated on. Additive: a brain that doesn't read a field ignores it.
            "channel_open": (open_now := availability(cfg, ch, self._clock())).open,
            "closed_reason": open_now.reason,
            "spoken_brand_name": ch.spoken_brand_name,
            "handoff_target": ch.handoff_target,
        }

        async with self._client.stream(
            "POST", f"{base_url.rstrip('/')}/api/v1/query/stream", json=payload
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[len("data: ") :])
                yield _to_turn_event(event)


def _to_turn_event(event: dict) -> TurnEvent:
    event_type = event.get("type")
    if event_type == "token":
        return TurnEvent(type="token", data={"text": event.get("data", "")})
    if event_type == "done":
        data = {
            "answer": event.get("answer", ""),
            "sources": event.get("sources", []),
            "suggestions": event.get("suggestions", []),
        }
        ui = event.get("ui")
        if ui is not None:
            ui_bytes = len(json.dumps(ui))
            if ui_bytes > _UI_MAX_BYTES:
                logger.warning("dropping oversized ui field bytes=%d", ui_bytes)
            else:
                data["ui"] = ui
        return TurnEvent(type="done", data=data)
    if event_type == "tool_call":
        return TurnEvent(
            type="tool", data={"name": event.get("name", ""), "status": event.get("status", "")}
        )
    if event_type == "error":
        return TurnEvent(type="error", data={"message": event.get("message", "")})
    if event_type == "usage":
        return TurnEvent(type="usage", data=event.get("data", {}))
    logger.warning("unrecognized backend event type=%s", event_type)
    return TurnEvent(type="error", data={"message": f"unrecognized event type: {event_type}"})
