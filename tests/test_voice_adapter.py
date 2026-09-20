import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from orca_gateway import main as main_module
from orca_gateway.channels import elevenlabs_llm
from orca_gateway.coalescer import TurnCoalescer
from orca_gateway.config import get_settings
from orca_gateway.main import app
from orca_gateway.seam import Channel, Identity, TurnEvent

TRACE = "ae020331887c7f6b95acd0c22afb86fa"
TP = f"00-{TRACE}-8a2e73c1d4f50b96-01"
SECRET = "s3cret-test"


class _Backend:
    def __init__(self, events=None, delay=0.0):
        self.calls: list[dict] = []
        self.events = events
        self.delay = delay

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
        self.calls.append(
            {
                "agent_id": agent_id,
                "channel": channel,
                "identity": identity.authority,
                "tenant": tenant,
                "turn": turn,
                "conversation_id": conversation_id,
            }
        )
        await asyncio.sleep(self.delay)
        for e in self.events or [
            TurnEvent(type="token", data={"text": "provisional"}),
            TurnEvent(type="done", data={"answer": f"final:{turn}", "sources": []}),
        ]:
            yield e


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setenv("ORCA_VOICE_TENANT", "my-tenant")
    monkeypatch.setenv("ORCA_VOICE_AGENT_ID", "front-desk")
    monkeypatch.setenv("ORCA_VOICE_SHARED_SECRET", SECRET)
    get_settings.cache_clear()
    backend = _Backend()
    monkeypatch.setattr(main_module, "get_backend", lambda: backend)
    monkeypatch.setattr(elevenlabs_llm, "_coalescer", TurnCoalescer(debounce_s=0.05))
    yield backend
    get_settings.cache_clear()


def _body(user="hello", **kw):
    return {
        "model": "x",
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [
            {"role": "system", "content": "THEIR TEMPLATE"},
            {"role": "assistant", "content": "earlier"},
            {"role": "user", "content": user},
        ],
        **kw,
    }


def _headers(tp=TP, auth=f"Bearer {SECRET}"):
    h = {}
    if tp:
        h["traceparent"] = tp
    if auth:
        h["authorization"] = auth
    return h


async def _post(body=None, headers=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.post(
            "/chat/completions", json=body or _body(), headers=headers or _headers()
        )


def _sse(text: str) -> list[str]:
    return [line[6:] for line in text.splitlines() if line.startswith("data: ")]


async def test_happy_path_speaks_done_answer_not_provisional_tokens(wired):
    r = await _post()
    assert r.status_code == 200
    frames = _sse(r.text)
    assert frames[-1] == "[DONE]"
    content = json.loads(frames[0])["choices"][0]["delta"]["content"]
    assert content == "final:hello"  # the authoritative answer; provisional tokens never spoken
    assert "provisional" not in r.text


async def test_seam_call_uses_trace_id_last_user_turn_and_ignores_their_prompt(wired):
    await _post()
    (call,) = wired.calls
    assert call["conversation_id"] == TRACE and call["turn"] == "hello"
    assert (call["channel"], call["identity"]) == ("voice", "anonymous")
    assert (call["tenant"], call["agent_id"]) == ("my-tenant", "front-desk")


@pytest.mark.parametrize("tp", [None, "", "garbage", f"00-{'0' * 32}-8a2e73c1d4f50b96-01"])
async def test_missing_or_invalid_traceparent_is_400_and_never_reaches_backend(wired, tp):
    r = await _post(headers=_headers(tp=tp))
    assert r.status_code == 400 and wired.calls == []


@pytest.mark.parametrize("auth", [None, "Bearer wrong", "Bearer ", SECRET])
async def test_authorization_presence_is_not_authentication(wired, auth):
    r = await _post(headers=_headers(auth=auth))
    assert r.status_code == 401 and wired.calls == []


async def test_fails_closed_when_secret_unset(wired, monkeypatch):
    monkeypatch.setenv("ORCA_VOICE_SHARED_SECRET", "")
    get_settings.cache_clear()
    assert (await _post()).status_code == 503


async def test_non_streaming_request_is_rejected(wired):
    assert (await _post(body=_body(stream=False))).status_code == 400


async def test_upstream_error_is_502_before_any_audio_and_leaks_nothing(wired):
    wired.events = [TurnEvent(type="error", data={"message": "vendor.example.com exploded"})]
    r = await _post()
    assert r.status_code == 502 and "vendor" not in r.text


async def test_fan_out_of_four_variants_makes_exactly_one_backend_call(wired):
    variants = ["हो, अलिकति भन्न।", "हो, अलिकति भन।", "हो, अलिकति बनाउँ।", "हो, अलिकति बन्न।"]
    rs = await asyncio.gather(*[_post(body=_body(user=v)) for v in variants])
    assert [r.status_code for r in rs] == [200] * 4
    assert len(wired.calls) == 1 and wired.calls[0]["turn"] == variants[-1]


async def test_usage_chunk_only_when_backend_reports_it(wired):
    assert '"usage"' not in (await _post()).text
    wired.events = [
        TurnEvent(type="done", data={"answer": "ok", "sources": []}),
        TurnEvent(type="usage", data={"prompt_tokens": 3, "completion_tokens": 1}),
    ]
    elevenlabs_llm._coalescer = TurnCoalescer(debounce_s=0.0)
    r = await _post(headers=_headers(tp=f"00-{'1' * 32}-8a2e73c1d4f50b96-01"))
    assert '"prompt_tokens": 3' in r.text
