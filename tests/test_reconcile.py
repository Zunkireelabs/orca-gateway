"""P6 brief §3.1/§5: the ElevenLabs reconciliation reader. Read-only against the vendor (a mocked
transport, never a real network call) and against the call path (only `calls_needing_reconciliation`
/ `record_elevenlabs_meters` are exercised here, never anything on the request-serving side).

`CONVERSATION_PAYLOAD` is shaped exactly like the `GET /v1/convai/conversations/{id}` response
documented in docs/metering-reconciliation.md (checked against ElevenLabs' own API docs, 2026-09-20)
-- a recorded payload shape, not a live vendor call.
"""

from __future__ import annotations

from datetime import date

import httpx

from orca_gateway.reconcile import ElevenLabsClient, parse_charging, reconcile

CONVERSATION_PAYLOAD = {
    "conversation_id": "conv-1",
    "agent_id": "front-desk-el-agent",
    "metadata": {
        "charging": {
            "tts_usage": {"total_characters": 842, "total_audio_output_seconds": 61.4},
            "asr_usage": {"total_audio_input_seconds": 47.2},
            "cost_fiat": 0.083,
        }
    },
}


def _mock_transport(responses: dict[str, httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["xi-api-key"] == "test-key"  # never omitted, never logged
        conversation_id = request.url.path.rsplit("/", 1)[-1]
        return responses.get(conversation_id, httpx.Response(404))

    return httpx.MockTransport(handler)


class FakeCallsRepo:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.written: list[dict] = []

    async def calls_needing_reconciliation(self, *, since, tenant_slug=None):
        return list(self._rows)

    async def record_elevenlabs_meters(
        self, *, conversation_id, stt_minutes, tts_characters, elevenlabs_cost_fiat
    ):
        self.written.append(
            {
                "conversation_id": conversation_id,
                "stt_minutes": stt_minutes,
                "tts_characters": tts_characters,
                "elevenlabs_cost_fiat": elevenlabs_cost_fiat,
            }
        )
        return True


# ---- parsing --------------------------------------------------------------------------------


def test_parse_charging_converts_seconds_to_minutes_and_passes_cost_fiat_through():
    charging = CONVERSATION_PAYLOAD["metadata"]["charging"]
    stt_minutes, tts_characters, cost_fiat = parse_charging(charging)
    assert stt_minutes == round(47.2 / 60.0, 2)
    assert tts_characters == 842
    assert cost_fiat == 0.083


def test_parse_charging_missing_fields_stay_none_not_guessed():
    stt_minutes, tts_characters, cost_fiat = parse_charging({})
    assert (stt_minutes, tts_characters, cost_fiat) == (None, None, None)


# ---- the reconciliation loop -----------------------------------------------------------------


async def test_reconcile_writes_back_stt_tts_and_cost_fiat():
    client = ElevenLabsClient(
        "test-key",
        client=httpx.AsyncClient(
            base_url="https://api.elevenlabs.io",
            transport=_mock_transport({"conv-1": httpx.Response(200, json=CONVERSATION_PAYLOAD)}),
        ),
    )
    repo = FakeCallsRepo([{"conversation_id": "conv-1", "tenant_slug": "dental-city"}])

    summary = await reconcile(repo, client, since=date(2026, 9, 1), sleep_s=0)

    assert summary == {"calls_in_window": 1, "reconciled": 1, "not_found": 0, "errors": 0}
    assert repo.written == [
        {
            "conversation_id": "conv-1",
            "stt_minutes": round(47.2 / 60.0, 2),
            "tts_characters": 842,
            "elevenlabs_cost_fiat": 0.083,
        }
    ]


async def test_reconcile_is_idempotent_a_second_run_writes_the_same_values():
    def make_client() -> ElevenLabsClient:
        return ElevenLabsClient(
            "test-key",
            client=httpx.AsyncClient(
                base_url="https://api.elevenlabs.io",
                transport=_mock_transport(
                    {"conv-1": httpx.Response(200, json=CONVERSATION_PAYLOAD)}
                ),
            ),
        )

    repo = FakeCallsRepo([{"conversation_id": "conv-1", "tenant_slug": "dental-city"}])
    await reconcile(repo, make_client(), since=date(2026, 9, 1), sleep_s=0)
    await reconcile(repo, make_client(), since=date(2026, 9, 1), sleep_s=0)
    assert repo.written[0] == repo.written[1]


async def test_reconcile_counts_a_conversation_elevenlabs_never_heard_of_as_not_found():
    client = ElevenLabsClient(
        "test-key",
        client=httpx.AsyncClient(
            base_url="https://api.elevenlabs.io", transport=_mock_transport({})
        ),
    )
    repo = FakeCallsRepo([{"conversation_id": "conv-missing", "tenant_slug": "dental-city"}])
    summary = await reconcile(repo, client, since=date(2026, 9, 1), sleep_s=0)
    assert summary == {"calls_in_window": 1, "reconciled": 0, "not_found": 1, "errors": 0}
    assert repo.written == []


async def test_reconcile_one_bad_conversation_does_not_abort_the_window():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("conv-bad"):
            return httpx.Response(500)
        return httpx.Response(200, json=CONVERSATION_PAYLOAD)

    client = ElevenLabsClient(
        "test-key",
        client=httpx.AsyncClient(
            base_url="https://api.elevenlabs.io", transport=httpx.MockTransport(handler)
        ),
    )
    repo = FakeCallsRepo(
        [
            {"conversation_id": "conv-bad", "tenant_slug": "dental-city"},
            {"conversation_id": "conv-1", "tenant_slug": "dental-city"},
        ]
    )
    summary = await reconcile(repo, client, since=date(2026, 9, 1), sleep_s=0)
    assert summary == {"calls_in_window": 2, "reconciled": 1, "not_found": 0, "errors": 1}
    assert len(repo.written) == 1

