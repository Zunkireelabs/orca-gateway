"""The seam: the one interface every channel adapter talks to.

    session(agent_id, channel, identity, tenant, turn) -> token stream + tool events + usage

Nothing upstream of an AgentBackend implementation may know what backs an agent — not
a vendor name, not a payload shape, not a tenant's product domain. Only a backend
implementation (see backends/) is allowed to know that.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal, Protocol

from pydantic import BaseModel

Channel = Literal["chat", "voice"]


class Identity(BaseModel):
    """Who is on this turn, and what they're allowed to do.

    `anonymous` is a caller with no verified identity — the only class in use this
    cycle. `authenticated` exists from day one so an operator copilot is a new value
    here, not a refactor of this type.
    """

    authority: Literal["anonymous", "authenticated"]
    external_id: str | None = None


class TurnEvent(BaseModel):
    """One event out of a session stream."""

    type: Literal["token", "tool", "usage", "done", "error"]
    data: dict


class AgentBackend(Protocol):
    """Something that can run a turn for a tenant's agent and stream back events."""

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
        """Run one turn. `conversation_id` scopes multi-turn state on the backend
        side — callers that want a stateless probe must pass a fresh id per call.
        """
        ...
