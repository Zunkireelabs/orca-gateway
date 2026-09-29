"""P6 Fix B (P6-RECONCILE-JOIN-FIX-BRIEF.md): the deferred STT/TTS/cost_fiat pull S5 documented
but did not build (see docs/metering-reconciliation.md). READ-ONLY against ElevenLabs and against
the call path: it never touches anything on the live voice/chat request path, and it only writes
`elevenlabs_conversation_id` plus three meter columns (`stt_minutes`, `tts_characters`,
`elevenlabs_cost_fiat`) on `orca_gw.calls` rows that already exist.

This gateway's own `conversation_id` is the W3C traceparent trace-id, NOT ElevenLabs' `conv_…`
id (proven on prod, session 60 -- see the brief §1). So the join can't be a direct per-conversation
GET keyed on our id; instead this pulls ElevenLabs' conversation LIST for each candidate's
`elevenlabs_agent_id` and matches each stored call to the nearest-start-time conversation within
tolerance (brief §2). A call whose `elevenlabs_conversation_id` was already matched by a prior run
skips listing entirely and joins directly -- idempotent and cheap on re-run.

Run as a plain module, the same shape as migrate.py:

    python -m orca_gateway.reconcile --since 2026-09-01 [--tenant <slug>]

Reads `ELEVENLABS_API_KEY` from the environment -- never printed, never hardcoded. A pull against
real prod conversations is Sadin's own `!` action (brief §6 Q3): this CLI is read-only, but it
still hits a paid vendor, so it is not something to run against prod unattended.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections import defaultdict
from datetime import UTC, date, datetime

import httpx

from orca_gateway.calls_repo import PgCallsRepository
from orca_gateway.config import get_settings

logger = logging.getLogger("orca_gateway.reconcile")

ELEVENLABS_API_BASE = "https://api.elevenlabs.io"

# Brief §2.3: pilot calls are serial and seconds apart, so a nearest-start-time match within this
# window is trustworthy. The one weak spot is many truly-simultaneous calls (not a real risk at
# pilot volume; Fix A removes the heuristic entirely once it ships).
MATCH_TOLERANCE_SECONDS = 120


class ElevenLabsClient:
    """Two read-only calls, nothing else -- see docs/metering-reconciliation.md for the endpoints
    and the `metadata.charging` shape `conversation_charging` reads."""

    def __init__(self, api_key: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(base_url=ELEVENLABS_API_BASE, timeout=30.0)

    async def conversation_charging(self, conversation_id: str) -> dict | None:
        """`metadata.charging` for one conversation, or None if ElevenLabs has no such
        conversation (a call this gateway ran that never reached ElevenLabs, or a stale id) --
        distinguished from a real charging payload with every field null, which is possible and
        not an error."""
        resp = await self._client.get(
            f"/v1/convai/conversations/{conversation_id}",
            headers={"xi-api-key": self._api_key},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        body = resp.json()
        return (body.get("metadata") or {}).get("charging")

    async def list_conversations(self, *, agent_id: str, since_unix: int) -> list[dict]:
        """Every ElevenLabs conversation for `agent_id` with `start_time_unix_secs >= since_unix`
        (brief §2.2). No cost/usage in this payload -- just enough to match on: `conversation_id`,
        `agent_id`, `start_time_unix_secs`, `call_duration_secs`, `message_count`. Pages with
        `has_more` / `next_cursor` (per ElevenLabs' documented shape; not re-verified against a
        live response in this change -- ElevenLabs API access wasn't available in this session)
        until either exhausted or a page's oldest conversation is already older than
        `since_unix`."""
        conversations: list[dict] = []
        cursor: str | None = None
        while True:
            params: dict[str, str | int] = {"agent_id": agent_id, "page_size": 100}
            if cursor:
                params["cursor"] = cursor
            resp = await self._client.get(
                "/v1/convai/conversations",
                params=params,
                headers={"xi-api-key": self._api_key},
            )
            resp.raise_for_status()
            body = resp.json()
            page = body.get("conversations") or []
            conversations.extend(page)
            page_starts = [
                c["start_time_unix_secs"] for c in page if c.get("start_time_unix_secs") is not None
            ]
            has_more = bool(body.get("has_more"))
            cursor = body.get("next_cursor")
            if not has_more or not cursor:
                break
            if page_starts and min(page_starts) < since_unix:
                break
        return [
            c
            for c in conversations
            if c.get("start_time_unix_secs") is not None and c["start_time_unix_secs"] >= since_unix
        ]

    async def aclose(self) -> None:
        await self._client.aclose()


def parse_charging(
    charging: dict, *, conversation_id: str | None = None
) -> tuple[float | None, int | None, float | None]:
    """(stt_minutes, tts_characters, elevenlabs_cost_fiat) from one conversation's
    `metadata.charging`. A field ElevenLabs didn't send stays None -- never guessed at zero (same
    rule as every other meter in this gateway).

    P6 Follow-up C (brief §8, session 60 prod finding): the S5 doc invented a `cost_fiat` field --
    ElevenLabs' real payload has no such key, so this used to always return None. The real
    platform-dollar figure is `platform_price` (verified on a real prod conversation); it is
    written into the existing `elevenlabs_cost_fiat` column -- only the SOURCE field changes, not
    the column. `platform_charge`/`call_charge` are the same cost in ElevenLabs credits (a
    cross-check against the credit dashboard) -- logged, not persisted, since no meter needs them
    today. `llm_price` should be 0 here (ElevenLabs' Custom LLM bills to our own OpenAI key, never
    ElevenLabs) -- a non-zero value would mean ElevenLabs started pricing the LLM leg and
    `all_in` cost would start double-counting it, so that's logged as a warning rather than
    silently trusted."""
    asr_seconds = (charging.get("asr_usage") or {}).get("total_audio_input_seconds")
    stt_minutes = None if asr_seconds is None else round(asr_seconds / 60.0, 2)
    tts_characters = (charging.get("tts_usage") or {}).get("total_characters")
    platform_price = charging.get("platform_price")

    llm_price = charging.get("llm_price")
    if llm_price:
        logger.warning(
            "ElevenLabs charging.llm_price is non-zero (%s) for conversation=%s -- the Custom "
            "LLM leg is expected to bill $0 to ElevenLabs (it bills our own OpenAI key instead); "
            "a non-zero value means all_in cost may now double-count the LLM leg",
            llm_price,
            conversation_id,
        )
    logger.info(
        "elevenlabs charging credits conversation=%s platform_charge=%s call_charge=%s",
        conversation_id,
        charging.get("platform_charge"),
        charging.get("call_charge"),
    )
    return stt_minutes, tts_characters, platform_price


def match_conversations(
    calls: list[dict],
    conversations: list[dict],
    *,
    tolerance_s: int = MATCH_TOLERANCE_SECONDS,
) -> tuple[dict[str, str], set[str]]:
    """Brief §2.3: match each `calls` row to the nearest-start-time ElevenLabs conversation for
    the same `elevenlabs_agent_id`, within `tolerance_s`, enforcing a unique 1:1 assignment (a
    conversation is consumed once a call claims it). Global greedy-nearest: every (call, conv)
    pair within tolerance is considered, closest pairs win first, so a call never loses its best
    match to a call that had a worse one available elsewhere.

    Soft cross-check (call_duration_secs vs ended_at-started_at, message_count vs turn_count) is
    logged, not enforced -- the brief treats it as a confidence signal, not a hard filter; only
    time-tolerance and uniqueness gate a match.

    Returns `(matched, unmatched)`: `matched` maps our `conversation_id` -> ElevenLabs `conv_id`;
    `unmatched` is the set of our `conversation_id`s with no unique match inside tolerance --
    counted and skipped by the caller, never guessed."""
    by_agent: dict[str, list[dict]] = defaultdict(list)
    for conv in conversations:
        agent_id = conv.get("agent_id")
        if agent_id:
            by_agent[agent_id].append(conv)

    pairs: list[tuple[float, str, str]] = []
    for call in calls:
        agent_id = call.get("elevenlabs_agent_id")
        started_at = call.get("started_at")
        if not agent_id or started_at is None:
            continue
        started_unix = started_at.timestamp()
        for conv in by_agent.get(agent_id, []):
            conv_start = conv.get("start_time_unix_secs")
            conv_id = conv.get("conversation_id")
            if conv_start is None or not conv_id:
                continue
            diff = abs(started_unix - conv_start)
            if diff <= tolerance_s:
                pairs.append((diff, call["conversation_id"], conv_id))

    pairs.sort(key=lambda p: p[0])
    matched: dict[str, str] = {}
    consumed_convs: set[str] = set()
    for _diff, our_id, conv_id in pairs:
        if our_id in matched or conv_id in consumed_convs:
            continue
        matched[our_id] = conv_id
        consumed_convs.add(conv_id)

    candidate_ids = {
        call["conversation_id"]
        for call in calls
        if call.get("elevenlabs_agent_id") and call.get("started_at") is not None
    }
    unmatched = candidate_ids - matched.keys()
    for our_id in unmatched:
        logger.info("no unique ElevenLabs conversation match within tolerance for %s", our_id)
    return matched, unmatched


def _soft_validate(call: dict, conv: dict) -> bool:
    """Logs (never raises/rejects) when the duration/message-count cross-check disagrees with a
    time-based match -- brief §2.3's "soft-validated" signal, informational only."""
    ok = True
    ended_at = call.get("ended_at")
    started_at = call.get("started_at")
    duration_secs = conv.get("call_duration_secs")
    if ended_at is not None and started_at is not None and duration_secs is not None:
        expected = (ended_at - started_at).total_seconds()
        if abs(expected - duration_secs) > 30:
            ok = False
    turn_count = call.get("turn_count")
    message_count = conv.get("message_count")
    if turn_count is not None and message_count is not None and abs(turn_count - message_count) > 2:
        ok = False
    if not ok:
        logger.info(
            "soft cross-check mismatch conversation=%s matched=%s (duration/message_count "
            "disagree with the time-based match -- kept, not rejected)",
            call["conversation_id"],
            conv.get("conversation_id"),
        )
    return ok


async def reconcile(
    calls_repo: PgCallsRepository,
    client: ElevenLabsClient,
    *,
    since: date,
    tenant_slug: str | None = None,
    sleep_s: float = 0.1,
) -> dict:
    """Gentle and serial: one charging GET per matched conversation, in `started_at` order, with a
    short pause between (brief §2's "read-only, serialized, gentle pagination" guard -- pilot
    volume is tiny, this is not meant to scale). Never raises on a single conversation's failure --
    one bad id must not abort the whole window; it is counted and logged instead."""
    rows = await calls_repo.calls_needing_reconciliation(since=since, tenant_slug=tenant_slug)

    unmatchable = 0
    direct_rows: list[dict] = []
    needs_match_rows: list[dict] = []
    for row in rows:
        if row.get("elevenlabs_conversation_id"):
            direct_rows.append(row)
        elif row.get("elevenlabs_agent_id"):
            needs_match_rows.append(row)
        else:
            unmatchable += 1
            logger.info(
                "no elevenlabs_agent_id for conversation=%s tenant=%s -- unmatchable",
                row["conversation_id"],
                row["tenant_slug"],
            )

    conv_id_by_our_id: dict[str, str] = {
        row["conversation_id"]: row["elevenlabs_conversation_id"] for row in direct_rows
    }

    unmatched_count = 0
    if needs_match_rows:
        since_unix = int(
            datetime(since.year, since.month, since.day, tzinfo=UTC).timestamp()
        )
        agent_ids = {row["elevenlabs_agent_id"] for row in needs_match_rows}
        conversations: list[dict] = []
        for agent_id in sorted(agent_ids):
            conversations.extend(
                await client.list_conversations(agent_id=agent_id, since_unix=since_unix)
            )
            if sleep_s:
                await asyncio.sleep(sleep_s)

        matched, unmatched = match_conversations(needs_match_rows, conversations)
        unmatched_count = len(unmatched)
        conversations_by_id = {c["conversation_id"]: c for c in conversations}
        rows_by_our_id = {row["conversation_id"]: row for row in needs_match_rows}
        for our_id, conv_id in matched.items():
            conv_id_by_our_id[our_id] = conv_id
            conv = conversations_by_id.get(conv_id)
            if conv is not None:
                _soft_validate(rows_by_our_id[our_id], conv)

    reconciled = 0
    not_found = 0
    errors = 0
    for row in sorted(conv_id_by_our_id.items(), key=lambda item: item[0]):
        our_id, conv_id = row
        try:
            charging = await client.conversation_charging(conv_id)
        except httpx.HTTPError as exc:
            logger.warning(
                "reconcile failed conversation=%s elevenlabs_conversation=%s: %s",
                our_id,
                conv_id,
                exc,
            )
            errors += 1
            continue
        if charging is None:
            logger.info(
                "no ElevenLabs conversation for conversation=%s elevenlabs_conversation=%s",
                our_id,
                conv_id,
            )
            not_found += 1
            continue
        stt_minutes, tts_characters, cost_fiat = parse_charging(charging, conversation_id=our_id)
        await calls_repo.record_elevenlabs_meters(
            conversation_id=our_id,
            stt_minutes=stt_minutes,
            tts_characters=tts_characters,
            elevenlabs_cost_fiat=cost_fiat,
            elevenlabs_conversation_id=conv_id,
        )
        reconciled += 1
        if sleep_s:
            await asyncio.sleep(sleep_s)

    return {
        "calls_in_window": len(rows),
        "reconciled": reconciled,
        "not_found": not_found,
        "errors": errors,
        "unmatchable": unmatchable,
        "unmatched": unmatched_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True, help="UTC calendar date, YYYY-MM-DD")
    parser.add_argument("--tenant", default=None, help="tenant slug; omit for every tenant")
    args = parser.parse_args()

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        sys.exit("ELEVENLABS_API_KEY is not set")
    settings = get_settings()
    if not settings.database_url:
        sys.exit("ORCA_DATABASE_URL is not set")
    since = date.fromisoformat(args.since)

    async def _run() -> dict:
        client = ElevenLabsClient(api_key)
        try:
            repo = PgCallsRepository(settings.database_url, schema=settings.db_schema)
            return await reconcile(repo, client, since=since, tenant_slug=args.tenant)
        finally:
            await client.aclose()

    summary = asyncio.run(_run())
    print(
        f"reconciled {summary['reconciled']}/{summary['calls_in_window']} calls "
        f"(not found: {summary['not_found']}, errors: {summary['errors']}, "
        f"unmatchable: {summary['unmatchable']}, unmatched: {summary['unmatched']})"
    )


if __name__ == "__main__":
    main()
