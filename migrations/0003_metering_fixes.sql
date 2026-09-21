-- S5 follow-up. 0002 is already applied; it is never edited.
--
-- ended_reason gains the two enforcement outcomes so a tripped limit is visible on the row:
--   max_session      the call passed the channel's max_session_seconds (closed at the trip)
--   daily_spend_cap  the call was refused at its first turn (an already-closed row, 0 turns)
alter table orca_gw.calls drop constraint calls_ended_reason_check;
alter table orca_gw.calls add constraint calls_ended_reason_check
    check (ended_reason in ('completed', 'timed_out', 'kill_switch', 'error',
                            'max_session', 'daily_spend_cap'));

-- Runs that reached the backend and never completed (cancelled by a coalescer restart, timed
-- out, failed). The provider may still have billed them, but usage only arrives at the end of a
-- completed stream, so their cost is unknown to us. Counted, never estimated, so the gap between
-- llm_cost_usd and the real invoice is visible instead of silent.
alter table orca_gw.calls add column abandoned_run_count integer not null default 0
    check (abandoned_run_count >= 0);
