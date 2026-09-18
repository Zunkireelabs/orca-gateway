import httpx
import pytest

from orca_gateway.backends.zunkiree import UnknownTenantError, ZunkireeAgentBackend
from orca_gateway.seam import Identity

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


@pytest.fixture
def identity() -> Identity:
    return Identity(authority="anonymous")


async def test_session_streams_tokens_then_done(identity: Identity) -> None:
    captured: dict = {}
    client = httpx.AsyncClient(transport=_mock_transport(captured))
    backend = ZunkireeAgentBackend(
        base_url="https://staging-api.example.com",
        tenant_keys={"my-tenant": "some-site-id"},
        client=client,
    )

    events = [
        event
        async for event in backend.session(
            agent_id="front-desk",
            channel="voice",
            identity=identity,
            tenant="my-tenant",
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
    backend = ZunkireeAgentBackend(
        base_url="https://staging-api.example.com",
        tenant_keys={"my-tenant": "some-site-id"},
        client=client,
    )

    async for _ in backend.session(
        agent_id="front-desk",
        channel="voice",
        identity=identity,
        tenant="my-tenant",
        turn="hello",
        conversation_id="conv-1",
    ):
        pass

    assert captured["url"] == "https://staging-api.example.com/api/v1/query/stream"
    import json

    body = json.loads(captured["body"])
    assert body == {
        "site_id": "some-site-id",
        "question": "hello",
        "session_id": "conv-1",
        "channel": "voice",
    }


async def test_unknown_tenant_raises(identity: Identity) -> None:
    client = httpx.AsyncClient(transport=_mock_transport({}))
    backend = ZunkireeAgentBackend(base_url="https://x", tenant_keys={}, client=client)

    with pytest.raises(UnknownTenantError):
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
    backend = ZunkireeAgentBackend(
        base_url="https://x", tenant_keys={"my-tenant": "site"}, client=client
    )

    with pytest.raises(ValueError):
        async for _ in backend.session(
            agent_id="front-desk",
            channel="voice",
            identity=identity,
            tenant="my-tenant",
            turn="hi",
            conversation_id="",
        ):
            pass
