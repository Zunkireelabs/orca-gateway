-- P4 gateway-polish brief A3+A4: built-in messages follow the caller's own language, and a
-- silence turn ("...") is answered by the gateway itself. Both flags are per-channel, default
-- OFF (today's behaviour), so each rolls out to one tenant at a time and back with a config edit.
alter table orca_gw.tenant_channels
    add column caller_language_messages boolean not null default false,
    add column silence_nudge boolean not null default false,
    add column silence_nudge_message text;

-- A silence turn the gateway answers itself is recorded with ended_by = 'silence' (not a normal
-- completed turn, not billable): visible on the Calls panel, costless, never a backend call.
alter table orca_gw.turns drop constraint turns_ended_by_check;
alter table orca_gw.turns add constraint turns_ended_by_check
    check (ended_by in
        ('out_of_hours', 'max_session', 'daily_spend_cap', 'kill_switch', 'error', 'silence'));
