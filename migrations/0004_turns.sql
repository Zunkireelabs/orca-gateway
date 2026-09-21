-- S6 PR 1 (capture). Three tables; nothing here changes how calls are counted.
--
-- turns: one row per COMPLETED turn, written by the same transaction as calls.turn_count and the
-- usage totals (calls_repo.complete_turn), keyed unique on (call_id, depth). That key is the whole
-- point: the turn count, the usage and the transcript row are recorded together or not at all, and
-- a second attempt for the same depth is a no-op, so no second write path can double-count (the S5
-- bug). depth = len(messages) on the request, the coalescer's turn index.
--
-- PII: user_text and answer_text hold what callers say (names, phone numbers). They are stored so
-- the console can show a call, are NEVER written to logs, and are purged by the idle sweep after
-- ORCA_METERING_TURN_RETENTION_DAYS (30). calls, costs and labels are kept. No audio is stored.
create table orca_gw.turns (
    id           uuid primary key default gen_random_uuid(),
    call_id      uuid not null references orca_gw.calls (id) on delete cascade,
    depth        integer not null check (depth >= 0),
    user_text    text,                 -- the text the run actually answered (not every hypothesis)
    answer_text  text,                 -- null when nothing was spoken (a refused call)
    tools        jsonb not null default '[]'::jsonb,   -- [{name, status}] as opaque strings
    usage        jsonb,                -- the backend's usage payload for this run, as reported
    latency_ms   integer check (latency_ms is null or latency_ms >= 0),  -- first arrival -> answer
    coalescer    jsonb,                -- requests/restarts/joins for this depth, at completion
    -- set when the GATEWAY answered without the backend, instead of leaving it looking like an
    -- agent answer
    ended_by     text check (ended_by in ('out_of_hours', 'max_session', 'daily_spend_cap')),
    created_at   timestamptz not null default now(),
    unique (call_id, depth)
);
create index turns_created_at_idx on orca_gw.turns (created_at);

-- The seed of the eval set (a person's verdict on a call, or on one turn of it). Deliberately not
-- a foreign key to turns: a label must outlive the 30-day purge of the turn text it was about.
create table orca_gw.call_labels (
    id          uuid primary key default gen_random_uuid(),
    call_id     uuid not null references orca_gw.calls (id) on delete cascade,
    depth       integer check (depth is null or depth >= 0),   -- null = the whole call
    verdict     text not null check (verdict in ('good', 'bad', 'unsure')),
    note        text,
    created_at  timestamptz not null default now()
);
create index call_labels_call_idx on orca_gw.call_labels (call_id);

-- Every console write (config edit, kill switch, enable/disable, label) leaves one row: what
-- changed, before and after. actor is 'console' until there is an identity to record.
create table orca_gw.config_audit (
    id          uuid primary key default gen_random_uuid(),
    at          timestamptz not null default now(),
    tenant_id   uuid references orca_gw.tenants (id) on delete set null,
    channel     text,
    action      text not null,
    before      jsonb,
    after       jsonb,
    actor       text not null default 'console'
);
create index config_audit_at_idx on orca_gw.config_audit (at);

alter table orca_gw.turns enable row level security;
alter table orca_gw.call_labels enable row level security;
alter table orca_gw.config_audit enable row level security;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on schema orca_gw from anon;
        revoke all on all tables in schema orca_gw from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on schema orca_gw from authenticated;
        revoke all on all tables in schema orca_gw from authenticated;
    end if;
end
$$;
