"""P6 Fix B (P6-RECONCILE-JOIN-FIX-BRIEF.md §3): the ElevenLabs reconciliation reader, rewritten
for the list-and-match join. Read-only against the vendor (a mocked transport, never a real
network call) and against the call path (only `calls_needing_reconciliation` /
`record_elevenlabs_meters` are exercised here, never anything on the request-serving side).

`LIST_PAYLOAD` and `CONVERSATION_PAYLOAD` are shaped like the documented ElevenLabs response
(`GET /v1/convai/conversations` and `GET /v1/convai/conversations/{id}`) per
docs/metering-reconciliation.md and the brief §1/§2.2 -- a recorded payload shape, not a live
vendor call.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx

from orca_gateway.reconcile import (
    ElevenLabsClient,
    match_conversations,
    parse_charging,
    reconcile,
)

SINCE = date(2026, 9, 1)
SINCE_UNIX = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp())

# P6 Follow-up C: shaped from a REAL verified prod payload
# (conv_5801m3hah36bf7rb1b5yy3wyn5vp, a 41s dental-city-pilot call, session 60 2026-09-29).
# ElevenLabs' `metadata.charging` has no `cost_fiat` key at all -- the S5 doc invented it. The
# real platform-dollar field is `platform_price`; `platform_charge`/`call_charge` are the same
# cost in ElevenLabs credits; `llm_price`/`llm_charge` are 0 because the Custom LLM bills our own
# OpenAI key, never ElevenLabs.
CONVERSATION_PAYLOAD = {
    "conversation_id": "conv_1",
    "agent_id": "front-desk-el-agent",
    "metadata": {
        "charging": {
            "tts_usage": {"total_characters": 842, "total_audio_output_seconds": 61.4},
            "asr_usage": {"total_audio_input_seconds": 47.2},
            "llm_price": 0.0,
            "llm_charge": 0,
            "call_charge": 306,
            "platform_charge": 306,
            "platform_price": 0.0552,
            "free_minutes_consumed": 0.0,
            "free_llm_dollars_consumed": 0.0,
        }
    },
}


def _call_row(
    *,
    conversation_id: str,
    tenant_slug: str = "dental-city",
    elevenlabs_agent_id: str | None = "front-desk-el-agent",
    elevenlabs_conversation_id: str | None = None,
    started_at: datetime,
    ended_at: datetime | None = None,
    turn_count: int = 4,
) -> dict:
    return {
        "id": conversation_id,
        "conversation_id": conversation_id,
        "tenant_slug": tenant_slug,
        "elevenlabs_agent_id": elevenlabs_agent_id,
        "elevenlabs_conversation_id": elevenlabs_conversation_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "turn_count": turn_count,
        "stt_minutes": None,
        "tts_characters": None,
        "elevenlabs_cost_fiat": None,
    }


def _conv(
    conversation_id: str,
    *,
    agent_id: str = "front-desk-el-agent",
    start_time_unix_secs: int,
    call_duration_secs: int = 90,
    message_count: int = 4,
) -> dict:
    return {
        "conversation_id": conversation_id,
        "agent_id": agent_id,
        "start_time_unix_secs": start_time_unix_secs,
        "call_duration_secs": call_duration_secs,
        "message_count": message_count,
    }


def _list_transport(agent_pages: dict[str, list[dict]]) -> httpx.MockTransport:
    """One page per agent -- enough for these fixtures; pagination itself is exercised by the
    `has_more`/`next_cursor` unit test below."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["xi-api-key"] == "test-key"
        if request.url.path == "/v1/convai/conversations":
            agent_id = request.url.params["agent_id"]
            conversations = agent_pages.get(agent_id, [])
            return httpx.Response(
                200, json={"conversations": conversations, "has_more": False, "next_cursor": None}
            )
        conv_id = request.url.path.rsplit("/", 1)[-1]
        if conv_id == "conv_1":
            return httpx.Response(200, json=CONVERSATION_PAYLOAD)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class FakeCallsRepo:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.written: list[dict] = []

    async def calls_needing_reconciliation(self, *, since, tenant_slug=None):
        return list(self._rows)

    async def record_elevenlabs_meters(
        self,
        *,
        conversation_id,
        stt_minutes,
        tts_characters,
        elevenlabs_cost_fiat,
        elevenlabs_conversation_id=None,
    ):
        self.written.append(
            {
                "conversation_id": conversation_id,
                "stt_minutes": stt_minutes,
                "tts_characters": tts_characters,
                "elevenlabs_cost_fiat": elevenlabs_cost_fiat,
                "elevenlabs_conversation_id": elevenlabs_conversation_id,
            }
        )
        return True


def _client(agent_pages: dict[str, list[dict]]) -> ElevenLabsClient:
    return ElevenLabsClient(
        "test-key",
        client=httpx.AsyncClient(
            base_url="https://api.elevenlabs.io", transport=_list_transport(agent_pages)
        ),
    )


# ---- parsing --------------------------------------------------------------------------------


def test_parse_charging_converts_seconds_to_minutes_and_reads_platform_price_not_cost_fiat():
    charging = CONVERSATION_PAYLOAD["metadata"]["charging"]
    stt_minutes, tts_characters, cost_fiat = parse_charging(charging)
    assert stt_minutes == round(47.2 / 60.0, 2)
    assert tts_characters == 842
    assert cost_fiat == 0.0552  # platform_price -- ElevenLabs has no cost_fiat key at all


def test_parse_charging_missing_fields_stay_none_not_guessed():
    stt_minutes, tts_characters, cost_fiat = parse_charging({})
    assert (stt_minutes, tts_characters, cost_fiat) == (None, None, None)


def test_parse_charging_warns_when_llm_price_is_non_zero(caplog):
    charging = dict(CONVERSATION_PAYLOAD["metadata"]["charging"], llm_price=0.02)
    with caplog.at_level("WARNING", logger="orca_gateway.reconcile"):
        parse_charging(charging, conversation_id="our-1")
    assert any("llm_price" in record.message for record in caplog.records)


def test_parse_charging_does_not_warn_when_llm_price_is_zero(caplog):
    charging = CONVERSATION_PAYLOAD["metadata"]["charging"]
    with caplog.at_level("WARNING", logger="orca_gateway.reconcile"):
        parse_charging(charging, conversation_id="our-1")
    assert not any("llm_price" in record.message for record in caplog.records)


# ---- matching ---------------------------------------------------------------------------------


def test_match_conversations_matches_nearest_start_time_within_tolerance():
    started = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
    call = _call_row(conversation_id="our-1", started_at=started)
    conv = _conv("conv_1", start_time_unix_secs=int(started.timestamp()) + 5)
    matched, unmatched = match_conversations([call], [conv])
    assert matched == {"our-1": "conv_1"}
    assert unmatched == set()


def test_match_conversations_outside_tolerance_is_unmatched():
    started = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
    call = _call_row(conversation_id="our-1", started_at=started)
    conv = _conv("conv_1", start_time_unix_secs=int(started.timestamp()) + 300)
    matched, unmatched = match_conversations([call], [conv])
    assert matched == {}
    assert unmatched == {"our-1"}


def test_match_conversations_enforces_unique_1to1_no_double_claim():
    t0 = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
    call_a = _call_row(conversation_id="our-a", started_at=t0)
    call_b = _call_row(conversation_id="our-b", started_at=t0 + timedelta(seconds=10))
    conv = _conv("conv_1", start_time_unix_secs=int(t0.timestamp()) + 2)
    matched, unmatched = match_conversations([call_a, call_b], [conv])
    # call_a is the closer match to the one available conversation; call_b loses it and is
    # counted unmatched rather than guessed.
    assert matched == {"our-a": "conv_1"}
    assert unmatched == {"our-b"}


def test_match_conversations_null_agent_id_is_excluded_not_matched():
    started = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
    call = _call_row(conversation_id="our-1", elevenlabs_agent_id=None, started_at=started)
    conv = _conv("conv_1", start_time_unix_secs=int(started.timestamp()))
    matched, unmatched = match_conversations([call], [conv])
    assert matched == {}
    assert unmatched == set()  # never a "match candidate" in the first place


# ---- the reconciliation loop -----------------------------------------------------------------


async def test_reconcile_lists_matches_and_writes_back_meters_and_conv_id():
    started = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
    repo = FakeCallsRepo([_call_row(conversation_id="our-1", started_at=started)])
    client = _client(
        {"front-desk-el-agent": [_conv("conv_1", start_time_unix_secs=int(started.timestamp()))]}
    )

    summary = await reconcile(repo, client, since=SINCE, sleep_s=0)

    assert summary == {
        "calls_in_window": 1,
        "reconciled": 1,
        "not_found": 0,
        "errors": 0,
        "unmatchable": 0,
        "unmatched": 0,
    }
    assert repo.written == [
        {
            "conversation_id": "our-1",
            "stt_minutes": round(47.2 / 60.0, 2),
            "tts_characters": 842,
            "elevenlabs_cost_fiat": 0.0552,
            "elevenlabs_conversation_id": "conv_1",
        }
    ]


async def test_reconcile_null_agent_id_is_unmatchable_and_never_listed():
    repo = FakeCallsRepo(
        [
            _call_row(
                conversation_id="our-1",
                elevenlabs_agent_id=None,
                started_at=datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
            )
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no vendor call expected for an unmatchable row")

    client = ElevenLabsClient(
        "test-key",
        client=httpx.AsyncClient(
            base_url="https://api.elevenlabs.io", transport=httpx.MockTransport(handler)
        ),
    )

    summary = await reconcile(repo, client, since=SINCE, sleep_s=0)
    assert summary == {
        "calls_in_window": 1,
        "reconciled": 0,
        "not_found": 0,
        "errors": 0,
        "unmatchable": 1,
        "unmatched": 0,
    }
    assert repo.written == []


async def test_reconcile_outside_tolerance_is_unmatched_and_skipped():
    started = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
    repo = FakeCallsRepo([_call_row(conversation_id="our-1", started_at=started)])
    client = _client(
        {
            "front-desk-el-agent": [
                _conv("conv_1", start_time_unix_secs=int(started.timestamp()) + 300)
            ]
        }
    )

    summary = await reconcile(repo, client, since=SINCE, sleep_s=0)
    assert summary["reconciled"] == 0
    assert summary["unmatched"] == 1
    assert repo.written == []


async def test_reconcile_second_run_joins_directly_and_never_lists():
    started = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
    repo = FakeCallsRepo(
        [
            _call_row(
                conversation_id="our-1",
                elevenlabs_conversation_id="conv_1",
                started_at=started,
            )
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/convai/conversations":
            raise AssertionError("a stored elevenlabs_conversation_id must skip listing")
        assert request.headers["xi-api-key"] == "test-key"
        conv_id = request.url.path.rsplit("/", 1)[-1]
        if conv_id == "conv_1":
            return httpx.Response(200, json=CONVERSATION_PAYLOAD)
        return httpx.Response(404)

    client = ElevenLabsClient(
        "test-key",
        client=httpx.AsyncClient(
            base_url="https://api.elevenlabs.io", transport=httpx.MockTransport(handler)
        ),
    )

    summary = await reconcile(repo, client, since=SINCE, sleep_s=0)
    assert summary["reconciled"] == 1
    assert repo.written[0]["elevenlabs_conversation_id"] == "conv_1"


async def test_reconcile_one_bad_conversation_does_not_abort_the_window():
    t0 = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    repo = FakeCallsRepo(
        [
            _call_row(
                conversation_id="our-bad",
                elevenlabs_conversation_id="conv_bad",
                started_at=t0,
            ),
            _call_row(
                conversation_id="our-1",
                elevenlabs_conversation_id="conv_1",
                started_at=t1,
            ),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("conv_bad"):
            return httpx.Response(500)
        return httpx.Response(200, json=CONVERSATION_PAYLOAD)

    client = ElevenLabsClient(
        "test-key",
        client=httpx.AsyncClient(
            base_url="https://api.elevenlabs.io", transport=httpx.MockTransport(handler)
        ),
    )
    summary = await reconcile(repo, client, since=SINCE, sleep_s=0)
    assert summary == {
        "calls_in_window": 2,
        "reconciled": 1,
        "not_found": 0,
        "errors": 1,
        "unmatchable": 0,
        "unmatched": 0,
    }
    assert len(repo.written) == 1


async def test_reconcile_counts_a_conversation_elevenlabs_never_heard_of_as_not_found():
    repo = FakeCallsRepo(
        [
            _call_row(
                conversation_id="our-1",
                elevenlabs_conversation_id="conv-missing",
                started_at=datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
            )
        ]
    )
    client = ElevenLabsClient(
        "test-key",
        client=httpx.AsyncClient(
            base_url="https://api.elevenlabs.io",
            transport=httpx.MockTransport(lambda request: httpx.Response(404)),
        ),
    )
    summary = await reconcile(repo, client, since=SINCE, sleep_s=0)
    assert summary == {
        "calls_in_window": 1,
        "reconciled": 0,
        "not_found": 1,
        "errors": 0,
        "unmatchable": 0,
        "unmatched": 0,
    }
    assert repo.written == []


# ---- pagination -------------------------------------------------------------------------------


async def test_list_conversations_follows_has_more_and_next_cursor():
    page_1 = {
        "conversations": [_conv("conv_new", start_time_unix_secs=SINCE_UNIX + 1000)],
        "has_more": True,
        "next_cursor": "cursor-2",
    }
    page_2 = {
        "conversations": [_conv("conv_old", start_time_unix_secs=SINCE_UNIX + 10)],
        "has_more": False,
        "next_cursor": None,
    }
    calls_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls_seen.append(dict(request.url.params))
        if "cursor" not in request.url.params:
            return httpx.Response(200, json=page_1)
        return httpx.Response(200, json=page_2)

    client = ElevenLabsClient(
        "test-key",
        client=httpx.AsyncClient(
            base_url="https://api.elevenlabs.io", transport=httpx.MockTransport(handler)
        ),
    )
    conversations = await client.list_conversations(agent_id="agent-1", since_unix=SINCE_UNIX)
    assert [c["conversation_id"] for c in conversations] == ["conv_new", "conv_old"]
    assert len(calls_seen) == 2
    assert calls_seen[1]["cursor"] == "cursor-2"
