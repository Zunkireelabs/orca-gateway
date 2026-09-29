-- P6 Fix B (P6-RECONCILE-JOIN-FIX-BRIEF.md §2.5): this gateway's own `conversation_id` is the W3C
-- traceparent trace-id, NOT ElevenLabs' `conv_…` id (proven on prod, session 60 — see
-- docs/metering-reconciliation.md). Store the real ElevenLabs conversation id once reconcile has
-- matched it via list-and-match, so re-runs join directly on this column instead of re-listing and
-- re-matching. Nullable and additive, same shape as 0011: null until reconcile writes it, and it
-- is never used as the write key for `record_elevenlabs_meters` (that stays this gateway's own
-- `conversation_id`, which is already unique).
alter table orca_gw.calls
    add column elevenlabs_conversation_id text;
