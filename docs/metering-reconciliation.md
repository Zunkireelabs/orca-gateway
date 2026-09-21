# Metering reconciliation (S5)

What each meter in `orca_gw.calls` is, how it gets there, and what "reconcile, don't estimate"
means for each one today.

## LLM tokens (`llm_prompt_tokens`, `llm_completion_tokens`, `llm_model`, `llm_cost_usd`)

**Real, end-to-end, once two small fixes are both live.** The seam has carried a `usage`
`TurnEvent` since S2; `elevenlabs_llm.py` forwards it correctly and always has. Two gaps existed
underneath that, both fixed in this slice or its immediate companion:

1. `backends/zunkiree.py`'s `_to_turn_event()` had no case for `usage` — it fell into a catch-all
   that fabricated an `error` event. **Fixed here**, with a regression test that fails if any
   future `seam.TurnEvent.type` is left unhandled the same way.
2. Zunkiree's `clinic_agent.py` never read `response.usage` off its two OpenAI calls. Fixed by a
   companion brief on the Zunkiree side (`ZUNKIREE-EMIT-USAGE-BRIEF.md`), confirmed live on stage
   (deployed sha `c272fcc`) with a real usage payload observed in the stream:
   `{"model": "gpt-4o-mini", "prompt_tokens": 3506, "completion_tokens": 35, "total_tokens": 3541}`.

`orca_gateway/calls_repo.py` accumulates prompt/completion tokens across a call's turns and
`orca_gateway/cost.py` prices them from a small hardcoded, dated table. **Never estimated**: a call
that never gets a `usage` event keeps `llm_prompt_tokens`/`llm_completion_tokens`/`llm_cost_usd` as
`null` (see `orca_gw.tenant_daily_spend.unpriced_call_count` for how the aggregate reports this
honestly instead of collapsing it to `$0`).

### When usage and turns are counted (fixed in the follow-up to S5)

Usage and the turn count are recorded when a backend run COMPLETES, inside `work()`, once. The
first cut recorded usage in the handler after the coalescer, so every duplicate raw request
sharing one result added the same tokens again (a 6-request fan-out recorded exactly 6x), and
counted the turn at run start, so every coalescer restart counted again. Rows written before
that fix (calls up to 2026-09-21) are overstated and were not rewritten.

`abandoned_run_count` counts runs that reached the backend but never completed (cancelled by a
restart, timed out, failed). The provider probably still bills a request that was already sent,
but usage only arrives at the end of a completed stream, so for those runs there is no number to
record and estimating one is forbidden. We record the count instead: a call with abandoned runs
has a real cost at or above `llm_cost_usd`. Closing that gap needs the backend to report usage
for a run it is cancelled out of, or a reconcile against the provider's own usage export.

## STT minutes and TTS characters (`stt_minutes`, `tts_characters`)

**A real, per-call reconciliation source exists — checked in ElevenLabs' own API docs on
2026-09-20, not assumed.** This is better than the brief's best case (a per-*agent* usage API): it
is per-*conversation*.

`GET https://api.elevenlabs.io/v1/convai/conversations/{conversation_id}` returns, under
`metadata.charging`:

- `tts_usage.total_characters` — the TTS meter this gateway cannot see, sourced exactly
- `tts_usage.total_audio_output_seconds`, `asr_usage.total_audio_input_seconds` — convertible to
  `stt_minutes` (divide by 60; `asr_usage` is the STT/ASR side)
- `cost_fiat` — "the sum of the LLM price and the non-LLM platform price," a second independent
  cross-check against this gateway's own `llm_cost_usd`
- `agent_id` — confirms the tenant↔agent mapping this gateway also stores as
  `tenant_channels.elevenlabs_agent_id`

ElevenLabs' docs mark `tts_usage`/`asr_usage` **"analytics-only, not billing"** — the authoritative
dollar figure for an invoice reconciliation is `cost_fiat`, and the character/second counts should
be treated as accurate for volume reconciliation but not guaranteed to be the literal billing unit.

There is also `GET /v1/convai/conversations` (list, filterable by `agent_id`) for pulling a whole
tenant's conversations in a window, but it does **not** carry the cost/usage breakdown — that
requires the per-conversation GET above, one call per `conversation_id`.

**The join, concretely:** for each `orca_gw.calls` row, call the per-conversation endpoint with
`conversation_id` (this gateway's own `conversation_id` IS ElevenLabs' `conversation_id` — it is
derived from the same `traceparent` ElevenLabs sends on every turn) and write `stt_minutes` /
`tts_characters` back in. **Not built in this slice** (S5 brief §5: no pricing/reconciliation
*service* — this documents the procedure; a script or console action can automate the pull later,
sized once S6 exists). Until automated, the manual procedure is: for the calls in a reconciliation
window, `GET` each `conversation_id` from that endpoint and update the two columns, or cross-check
totals against ElevenLabs' own Monitor → Conversations / usage dashboard.

**What this gateway guarantees today regardless of automation:** every `orca_gw.calls` row carries
`conversation_id` and (when the tenant's `elevenlabs_agent_id` is configured) `elevenlabs_agent_id`
— the only two keys this join will ever need.

## Telephony minutes (`telephony_minutes`)

Zero by design. No telephony connector exists (K1 open). The column exists so the schema doesn't
change shape when one lands.

## "Ended" (`ended_at`, `ended_reason`)

There is no telephony hangup signal and no explicit end-of-call event from ElevenLabs today (its
own dashboard presumably knows when a conversation ended, but that is not exposed to this gateway
as a webhook this cycle). Decided and implemented:

- A call is closed by a periodic **idle-timeout sweep** (`sweep.py`, run as a background task
  inside the single uvicorn worker — no telephony hangup exists to close it on the request path),
  when no turn has arrived for its `conversation_id` for **5 minutes** (`ended_reason = 'timed_out'`
  — a real phone call is not silent that long).
- `ended_reason = 'kill_switch'` is set **immediately at request time** (not by the sweep) when an
  open call's next turn is refused because the tenant's kill switch is now on — a deterministic
  signal, no reason to wait.
- `ended_reason = 'completed'` is reserved, unused by this slice: it is for when an explicit
  end-of-call signal exists (a future telephony hangup, or an ElevenLabs end-of-conversation
  webhook if one is added later).
- `ended_reason = 'error'` is reserved similarly — a single failed turn does not prove the call is
  over (the caller may just keep talking), so this slice does not set it proactively.

## Enforcement outcomes on the row

- `max_session_seconds` trip: WARNING log `max_session_seconds tripped tenant=.. conversation=..
  elapsed=..s limit=..s`, and the row is closed with `ended_reason = 'max_session'`. The gateway
  cannot hang up, so later turns get the same spoken handoff (English only for now).
- `daily_spend_cap` trip: WARNING log `daily_spend_cap tripped tenant=.. conversation=.. spend=..
  cap=..`, and an already-closed row with `ended_reason = 'daily_spend_cap'` and zero turns is
  written (not counted in `tenant_daily_spend`). Retries of that conversation keep being refused.

## Per-request arrival log

Every `/chat/completions` request logs one INFO line at arrival (the uvicorn access line is written
at response time, so it says nothing about when a request arrived):

    voice request arrival conversation=<trace-id> depth=<len(messages)> text_sha=<sha256[:12]>
    span=<traceparent span id> arrived_mono=<monotonic seconds> decision=<...>

`decision` is what the coalescer did with THIS request: `started` (new run), `restarted` (a newer
text for the same turn cancelled the in-flight run), `joined` (shares an existing run's result),
`stale` (the conversation already moved past this depth), or `not_coalesced` (answered without the
backend). Only a hash of the text is logged, never the text: a turn can contain caller PII. Logging
only, no behaviour change. The package logger now has its own handler; before this, every INFO line
from `orca_gateway` (including the idle-sweep line) was silently dropped in the container.

## `per_caller_rate_limit`

Stored, not enforced — no telephony, no caller ID, no phone number exists at the gateway today, so
there is no identity to enforce against. Documented as inert, same pattern as S4's
`take_message`/`handoff_anyway`.
