from collections.abc import AsyncIterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

from orca_gateway.main import app
from orca_gateway.seam import Channel, Identity, TurnEvent


class _FakeBackend:
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
        assert conversation_id  # never empty
        yield TurnEvent(type="token", data={"text": "hi"})
        yield TurnEvent(type="done", data={"answer": "hi", "sources": []})


class _BlowsUpMidStreamBackend:
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
        yield TurnEvent(type="token", data={"text": "partial"})
        raise ConnectionError("upstream reset the connection, from vendor.example.com")


@contextmanager
def _with_backend(backend):
    from orca_gateway import main as main_module

    original = main_module.get_backend
    main_module.get_backend = lambda: backend  # type: ignore[assignment]
    try:
        yield
    finally:
        main_module.get_backend = original


def _turn_payload(**overrides):
    payload = {
        "agent_id": "front-desk",
        "channel": "voice",
        "identity": {"authority": "anonymous"},
        "tenant": "my-tenant",
        "turn": "hello",
    }
    payload.update(overrides)
    return payload


def test_submit_turn_streams_sse() -> None:
    with _with_backend(_FakeBackend()):
        client = TestClient(app)
        response = client.post("/v1/turn", json=_turn_payload())
        assert response.status_code == 200
        assert '"type": "token"' in response.text
        assert '"type": "done"' in response.text


def test_submit_turn_rejects_invalid_channel() -> None:
    client = TestClient(app)
    for bad_channel in ("Voice", "sms", "", "phone"):
        response = client.post("/v1/turn", json=_turn_payload(channel=bad_channel))
        assert response.status_code == 422, bad_channel


def test_submit_turn_backend_failure_yields_terminal_error_event() -> None:
    with _with_backend(_BlowsUpMidStreamBackend()):
        client = TestClient(app)
        response = client.post("/v1/turn", json=_turn_payload())
        assert response.status_code == 200
        assert '"type": "token"' in response.text
        assert '"type": "error"' in response.text
        # Backend/vendor detail must never leak into the event body.
        assert "vendor.example.com" not in response.text
        assert "ConnectionError" not in response.text
