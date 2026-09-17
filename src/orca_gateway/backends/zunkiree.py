"""The ONLY place a backend's request/response shape or tenant-key vocabulary is
allowed to exist. Nothing above this module may know it, or that this backend is
even Zunkiree — that's what makes it swappable.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx

from orca_gateway.seam import Identity, TurnEvent

logger = logging.getLogger("orca_gateway.backends.zunkiree")


class UnknownTenantError(Exception):
    pass


class ZunkireeAgentBackend:
    """Calls a Zunkiree-hosted agent's streaming query endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        tenant_keys: dict[str, str],
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._tenant_keys = tenant_keys
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def session(
        self,
        *,
        agent_id: str,
        channel: str,
        identity: Identity,
        tenant: str,
        turn: str,
        conversation_id: str,
    ) -> AsyncIterator[TurnEvent]:
        if tenant not in self._tenant_keys:
            raise UnknownTenantError(tenant)
        if not conversation_id:
            raise ValueError("conversation_id must be a non-empty, caller-scoped id")

        payload = {
            "site_id": self._tenant_keys[tenant],
            "question": turn,
            "session_id": conversation_id,
            "channel": "voice" if channel == "voice" else "chat",
        }

        async with self._client.stream(
            "POST", f"{self._base_url}/api/v1/query/stream", json=payload
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
        return TurnEvent(
            type="done",
            data={
                "answer": event.get("answer", ""),
                "sources": event.get("sources", []),
            },
        )
    if event_type == "tool_call":
        return TurnEvent(
            type="tool", data={"name": event.get("name", ""), "status": event.get("status", "")}
        )
    if event_type == "error":
        return TurnEvent(type="error", data={"message": event.get("message", "")})
    logger.warning("unrecognized backend event type=%s", event_type)
    return TurnEvent(type="error", data={"message": f"unrecognized event type: {event_type}"})
