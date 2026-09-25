-- P4 brief A1: a voice caller must hear something instead of silence (a 403 on a kill switch,
-- a raw 502 on a run timeout or backend error). Each guard is a per-channel flag, default OFF,
-- so it is switched on for one tenant at a time and rolled back as config. The messages are
-- channel config too: null = the built-in wording (English or Nepali by default_language).
alter table orca_gw.tenant_channels
    add column spoken_kill_switch boolean not null default false,
    add column spoken_error_fallback boolean not null default false,
    add column kill_switch_message text,
    add column error_fallback_message text;

-- A turn answered with the error fallback is recorded as 'error' (P4 brief A1: "metering
-- records the turn as error"). 'kill_switch' is added alongside so the two spoken-fallback
-- outcomes are both representable on a turn row.
alter table orca_gw.turns drop constraint turns_ended_by_check;
alter table orca_gw.turns add constraint turns_ended_by_check
    check (ended_by in ('out_of_hours', 'max_session', 'daily_spend_cap', 'kill_switch', 'error'));
