from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from orca_gateway.main import app
from orca_gateway.seam import Identity, TurnEvent


class _FakeBackend:
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
        assert conversation_id  # never empty
        yield TurnEvent(type="token", data={"text": "hi"})
        yield TurnEvent(type="done", data={"answer": "hi", "sources": []})


def test_submit_turn_streams_sse() -> None:
    from orca_gateway import main as main_module

    original = main_module.get_backend
    main_module.get_backend = lambda: _FakeBackend()  # type: ignore[assignment]
    try:
        client = TestClient(app)
        response = client.post(
            "/v1/turn",
            json={
                "agent_id": "front-desk",
                "channel": "voice",
                "identity": {"authority": "anonymous"},
                "tenant": "my-tenant",
                "turn": "hello",
            },
        )
        assert response.status_code == 200
        assert '"type": "token"' in response.text
        assert '"type": "done"' in response.text
    finally:
        main_module.get_backend = original
