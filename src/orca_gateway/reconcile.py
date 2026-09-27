"""P6 brief §3.1: the deferred STT/TTS/cost_fiat pull S5 documented but did not build (see
docs/metering-reconciliation.md). READ-ONLY against ElevenLabs and against the call path: it
never touches anything on the live voice/chat request path, and it only writes three meter
columns (`stt_minutes`, `tts_characters`, `elevenlabs_cost_fiat`) on `orca_gw.calls` rows that
already exist. Idempotent -- re-running overwrites those same three columns with whatever the
vendor reports right now.

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
from datetime import date

import httpx

from orca_gateway.calls_repo import PgCallsRepository
from orca_gateway.config import get_settings

logger = logging.getLogger("orca_gateway.reconcile")

ELEVENLABS_API_BASE = "https://api.elevenlabs.io"


class ElevenLabsClient:
    """One GET per conversation, nothing else -- see docs/metering-reconciliation.md for the
    endpoint and the `metadata.charging` shape this reads."""

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

    async def aclose(self) -> None:
        await self._client.aclose()


def parse_charging(charging: dict) -> tuple[float | None, int | None, float | None]:
    """(stt_minutes, tts_characters, elevenlabs_cost_fiat) from one conversation's
    `metadata.charging`. A field ElevenLabs didn't send stays None -- never guessed at zero (same
    rule as every other meter in this gateway)."""
    asr_seconds = (charging.get("asr_usage") or {}).get("total_audio_input_seconds")
    stt_minutes = None if asr_seconds is None else round(asr_seconds / 60.0, 2)
    tts_characters = (charging.get("tts_usage") or {}).get("total_characters")
    cost_fiat = charging.get("cost_fiat")
    return stt_minutes, tts_characters, cost_fiat


async def reconcile(
    calls_repo: PgCallsRepository,
    client: ElevenLabsClient,
    *,
    since: date,
    tenant_slug: str | None = None,
    sleep_s: float = 0.1,
) -> dict:
    """Gentle and serial: one GET per conversation, in `started_at` order, with a short pause
    between (brief §3.1: "one GET per conversation; serialize and be gentle" -- pilot volume is
    tiny, this is not meant to scale). Never raises on a single conversation's failure -- one bad
    id must not abort the whole window; it is counted and logged instead."""
    rows = await calls_repo.calls_needing_reconciliation(since=since, tenant_slug=tenant_slug)
    reconciled = 0
    not_found = 0
    errors = 0
    for row in rows:
        try:
            charging = await client.conversation_charging(row["conversation_id"])
        except httpx.HTTPError as exc:
            logger.warning(
                "reconcile failed conversation=%s tenant=%s: %s",
                row["conversation_id"],
                row["tenant_slug"],
                exc,
            )
            errors += 1
            continue
        if charging is None:
            logger.info(
                "no ElevenLabs conversation for conversation=%s tenant=%s",
                row["conversation_id"],
                row["tenant_slug"],
            )
            not_found += 1
            continue
        stt_minutes, tts_characters, cost_fiat = parse_charging(charging)
        await calls_repo.record_elevenlabs_meters(
            conversation_id=row["conversation_id"],
            stt_minutes=stt_minutes,
            tts_characters=tts_characters,
            elevenlabs_cost_fiat=cost_fiat,
        )
        reconciled += 1
        if sleep_s:
            await asyncio.sleep(sleep_s)
    return {
        "calls_in_window": len(rows),
        "reconciled": reconciled,
        "not_found": not_found,
        "errors": errors,
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
        f"(not found: {summary['not_found']}, errors: {summary['errors']})"
    )


if __name__ == "__main__":
    main()
