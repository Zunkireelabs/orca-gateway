import httpx
import pytest

from orca_gateway.backends.zunkiree import ZunkireeAgentBackend
from orca_gateway.seam import Identity
from orca_gateway.tenants import TenantStore, TenantUnavailableError
from tests.tenant_fixtures import InMemoryRepo, dental_city

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
