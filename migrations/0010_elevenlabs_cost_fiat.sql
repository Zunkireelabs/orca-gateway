-- P6 brief §6 dec. 2: trust ElevenLabs' own `cost_fiat` as the authoritative dollar figure for
-- non-LLM cost (STT + TTS + its per-minute platform price), stored verbatim as the cross-check
-- and source of truth for the Cost panel's non-LLM dollars -- never derived from the volume
-- meters (`stt_minutes`, `tts_characters`), which stay analytics-only per
-- docs/metering-reconciliation.md. Nullable and additive: null until `reconcile.py` (P6 §3.1)
-- writes it back, same "unknown, not zero" discipline as every other cost column here (see
-- 0002_calls.sql on llm_cost_usd) -- a call's all-in cost must never silently drop this term to
-- $0 just because it hasn't been reconciled yet.
alter table orca_gw.calls
    add column elevenlabs_cost_fiat numeric(12, 6)
        check (elevenlabs_cost_fiat is null or elevenlabs_cost_fiat >= 0);
