import json
import typing
from datetime import UTC, date, datetime

import httpx
import pytest

from orca_gateway.backends.zunkiree import ZunkireeAgentBackend, _to_turn_event
from orca_gateway.seam import Identity, TurnEvent
from orca_gateway.tenants import TenantStore, TenantUnavailableError
from tests.tenant_fixtures import InMemoryRepo, chat, dental_city

# Every wire event type Zunkiree is known to send, mapped to the wire payload that produces it.
# Kept next to seam.py's declared TurnEvent.type Literal below so a new member added there without
# a matching case here fails loudly instead of silently falling into the catch-all.
_KNOWN_WIRE_EVENTS: dict[str, dict] = {
    "token": {"type": "token", "data": "hi"},
    "tool": {"type": "tool_call", "name": "get_hours", "status": "running"},
    "usage": {"type": "usage", "data": {"model": "gpt-4o-mini", "total_tokens": 42}},
    "done": {"type": "done", "answer": "hi", "sources": []},
    "error": {"type": "error", "message": "backend blew up"},
}


def test_every_declared_turn_event_type_has_a_real_backend_case() -> None:
    """A declared TurnEvent.type with no corresponding case in _to_turn_event silently falls into
    the catch-all, which FABRICATES an error event ("unrecognized event type: ...") instead of
    dropping or forwarding the real one -- worse than doing nothing. Regression for the 'usage'
    case going missing since S2 despite being part of the seam's contract the whole time."""
    declared_types = set(typing.get_args(TurnEvent.model_fields["type"].annotation))
    assert declared_types == set(_KNOWN_WIRE_EVENTS), (
        "a TurnEvent.type was added or removed in seam.py without updating this test's "
        "_KNOWN_WIRE_EVENTS -- add the wire payload that should produce it"
    )
    for expected_type, wire_event in _KNOWN_WIRE_EVENTS.items():
        result = _to_turn_event(wire_event)
        assert result.type == expected_type
        if result.type == "error":
            # the one type allowed to legitimately be "error" -- must be the real message,
            # never the catch-all's fabricated one.
            assert result.data["message"] == "backend blew up"

SSE_BODY = (
    b'data: {"type": "tool_call", "name": "get_hours", "status": "running"}\n\n'
    b'data: {"type": "tool_call", "name": "get_hours", "status": "done"}\n\n'
    b'data: {"type": "token", "data": "Hello"}\n\n'
    b'data: {"type": "token", "data": " world"}\n\n'
    b'data: {"type": "done", "answer": "Hello world", "sources": []}\n\n'
)


def _mock_transport(captured: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(200, content=SSE_BODY, headers={"content-type": "text/event-stream"})

    return httpx.MockTransport(handler)


def _backend(client, *tenants) -> ZunkireeAgentBackend:
    return ZunkireeAgentBackend(tenants=TenantStore(InMemoryRepo(*tenants)), client=client)


@pytest.fixture
def identity() -> Identity:
    return Identity(authority="anonymous")


async def test_session_streams_tokens_then_done(identity: Identity) -> None:
    captured: dict = {}
    client = httpx.AsyncClient(transport=_mock_transport(captured))
    backend = _backend(client, dental_city())

    events = [
        event
        async for event in backend.session(
            agent_id="front-desk",
            channel="voice",
            identity=identity,
            tenant="dental-city",
            turn="hello",
            conversation_id="conv-1",
        )
    ]

    assert [e.type for e in events] == ["tool", "tool", "token", "token", "done"]
    assert events[0].data == {"name": "get_hours", "status": "running"}
    assert events[2].data == {"text": "Hello"}
    assert events[4].data["answer"] == "Hello world"


async def test_session_posts_to_query_stream_with_tenant_key(identity: Identity) -> None:
    captured: dict = {}
    client = httpx.AsyncClient(transport=_mock_transport(captured))
    backend = _backend(client, dental_city())

    async for _ in backend.session(
        agent_id="front-desk",
        channel="voice",
        identity=identity,
        tenant="dental-city",
        turn="hello",
        conversation_id="conv-1",
    ):
        pass

    assert captured["url"] == "https://staging-api.example.com/api/v1/query/stream"
    import json

    body = json.loads(captured["body"])
    assert body == {
        "site_id": "dental-city",
        "question": "hello",
        "session_id": "conv-1",
        "channel": "voice",
        # P4 A2: tenant context, from the channel row
        "channel_open": True,
        "closed_reason": None,
        "spoken_brand_name": "डेन्टल सिटी",
        "handoff_target": None,
    }


async def test_unknown_tenant_raises(identity: Identity) -> None:
    client = httpx.AsyncClient(transport=_mock_transport({}))
    backend = _backend(client)

    with pytest.raises(TenantUnavailableError):
        async for _ in backend.session(
            agent_id="front-desk",
            channel="voice",
            identity=identity,
            tenant="nope",
            turn="hi",
            conversation_id="conv-1",
        ):
            pass


async def test_empty_conversation_id_rejected(identity: Identity) -> None:
    client = httpx.AsyncClient(transport=_mock_transport({}))
    backend = _backend(client, dental_city())

    with pytest.raises(ValueError):
        async for _ in backend.session(
            agent_id="front-desk",
            channel="voice",
            identity=identity,
            tenant="dental-city",
            turn="hi",
            conversation_id="",
        ):
            pass


async def test_backend_binding_comes_from_the_tenant_row_not_from_code(identity: Identity) -> None:
    """Two tenants, two brains: the site and host come from each row's backend_config."""
    import json

    from tests.tenant_fixtures import quiet_spa

    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, content=SSE_BODY, headers={"content-type": "text/event-stream"})

    backend = _backend(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), dental_city(), quiet_spa()
    )
    for tenant in ("dental-city", "quiet-spa"):
        async for _ in backend.session(
            agent_id="a",
            channel="voice",
            identity=identity,
            tenant=tenant,
            turn="hi",
            conversation_id="c",
        ):
            pass
    assert seen[0][0] == "https://staging-api.example.com/api/v1/query/stream"
    assert seen[0][1]["site_id"] == "dental-city"
    assert seen[1][0] == "https://other-brain.example.com/api/v1/query/stream"
    assert seen[1][1]["site_id"] == "quiet-spa-site"


async def test_killed_or_inactive_tenant_is_refused_by_the_backend_itself(
    identity: Identity,
) -> None:
    killed = dental_city()
    killed.channels["voice"].kill_switch = True
    inactive = dental_city().model_copy(update={"slug": "gone", "is_active": False})
    backend = _backend(httpx.AsyncClient(transport=_mock_transport({})), killed, inactive)
    for tenant in ("dental-city", "gone"):
        with pytest.raises(TenantUnavailableError):
            async for _ in backend.session(
                agent_id="a",
                channel="voice",
                identity=identity,
                tenant=tenant,
                turn="hi",
                conversation_id="c",
            ):
                pass


# ---- P4 A2: tenant context sent to the brain, for both adapters -------------------------------


async def _payload(tenant, channel, now, identity) -> dict:
    captured: dict = {}
    backend = ZunkireeAgentBackend(
        tenants=TenantStore(InMemoryRepo(tenant)),
        client=httpx.AsyncClient(transport=_mock_transport(captured)),
        clock=lambda: now,
    )
    async for _ in backend.session(
        agent_id="a",
        channel=channel,
        identity=identity,
        tenant=tenant.slug,
        turn="hi",
        conversation_id="c-1",
    ):
        pass
    return json.loads(captured["body"])


def _with_chat(tenant):
    tenant.channels["chat"] = chat(spoken_brand_name="Chat Brand", handoff_target="+977-1-000")
    return tenant


# 2026-10-20 is a Tuesday; Asia/Kathmandu is UTC+5:45.
_TUE_NOON_UTC = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)


@pytest.mark.parametrize("channel", ["voice", "chat"])
async def test_payload_says_open_with_no_reason_when_nothing_closes_the_channel(
    identity: Identity, channel
) -> None:
    body = await _payload(_with_chat(dental_city()), channel, _TUE_NOON_UTC, identity)
    assert body["channel_open"] is True and body["closed_reason"] is None
    assert body["channel"] == channel


@pytest.mark.parametrize("channel", ["voice", "chat"])
@pytest.mark.parametrize(
    "over, reason",
    [
        ({"closed_dates": [date(2026, 10, 20)]}, "closed_date"),
        ({"closed_weekdays": [1]}, "closed_weekday"),
        ({"handoff_hours": {"1": [["09:00", "10:00"]]}}, "outside_handoff_hours"),
    ],
)
async def test_payload_carries_every_closed_reason(
    identity: Identity, channel, over, reason
) -> None:
    tenant = _with_chat(dental_city())
    for name in ("voice", "chat"):
        for k, v in over.items():
            setattr(tenant.channels[name], k, v)
    body = await _payload(tenant, channel, _TUE_NOON_UTC, identity)
    assert body["channel_open"] is False and body["closed_reason"] == reason


async def test_payload_is_per_channel_brand_and_handoff_target(identity: Identity) -> None:
    tenant = _with_chat(dental_city())
    tenant.channels["voice"].handoff_target = "+977-1-111"
    voice_body = await _payload(tenant, "voice", _TUE_NOON_UTC, identity)
    chat_body = await _payload(tenant, "chat", _TUE_NOON_UTC, identity)
    assert (voice_body["spoken_brand_name"], voice_body["handoff_target"]) == (
        "डेन्टल सिटी",
        "+977-1-111",
    )
    assert (chat_body["spoken_brand_name"], chat_body["handoff_target"]) == (
        "Chat Brand",
        "+977-1-000",
    )
