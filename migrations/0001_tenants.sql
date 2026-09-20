-- orca_gw: the gateway's OWN schema. Never `public` (that belongs to another application in the
-- same Supabase project). Nothing here reads or writes anything outside orca_gw.
create schema if not exists orca_gw;

-- Who a tenant is, and how the gateway reaches its brain.
create table orca_gw.tenants (
    id              uuid primary key default gen_random_uuid(),
    slug            text not null unique
                    check (slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    display_name    text not null,
    is_active       boolean not null default true,
    timezone        text not null default 'Asia/Kathmandu',   -- IANA
    backend         text not null check (backend in ('zunkiree')),
    -- The ONLY place a backend's own vocabulary may exist. zunkiree: {"site_id": ..., "base_url": ...}
    backend_config  jsonb not null default '{}'::jsonb,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

-- One row per (tenant, channel): a tenant's voice settings and its chat settings are different objects.
create table orca_gw.tenant_channels (
    tenant_id             uuid not null references orca_gw.tenants (id) on delete cascade,
    channel               text not null check (channel in ('voice', 'chat')),
    is_enabled            boolean not null default true,
    agent_id              text not null default 'default',
    languages             text[] not null check (cardinality(languages) >= 1),
    default_language      text not null,
    voice_id              text,
    spoken_brand_name     text not null,        -- how the name is SAID, a decision, not a label
    handoff_target        text,
    -- weekday (0=Mon..6=Sun) -> list of [start, end] local windows in the tenant timezone.
    -- null = no human handoff is configured. Missing weekday = no handoff that day.
    handoff_hours         jsonb,
    out_of_hours_behaviour text not null default 'handoff_anyway'
                    check (out_of_hours_behaviour in ('take_message', 'say_closed', 'handoff_anyway')),
    out_of_hours_message  text,                 -- what is said when behaviour = say_closed; {brand} is substituted
    escalation_policy     text not null default 'none' check (escalation_policy in ('none', 'handoff', 'instruction')),
    escalation_instruction text,
    -- STOPGAP for a product gap: the product cannot express closures or a weekly schedule at all.
    -- A channel-level "do not offer, say closed instead" list. NOT the product's own authoritative schedule.
    closed_dates          date[] not null default '{}',
    closed_weekdays       smallint[] not null default '{}'
                    check (closed_weekdays <@ array[0,1,2,3,4,5,6]::smallint[]),
    -- Caps: stored from day one. Only kill_switch is enforced today; the rest are enforced by metering.
    max_session_seconds   integer check (max_session_seconds is null or max_session_seconds > 0),
    daily_spend_cap       numeric(10, 2) check (daily_spend_cap is null or daily_spend_cap >= 0),
    per_caller_rate_limit integer check (per_caller_rate_limit is null or per_caller_rate_limit > 0),
    kill_switch           boolean not null default false,
    created_at            timestamptz not null default now(),
    updated_at            timestamptz not null default now(),
    primary key (tenant_id, channel),
    check (default_language = any (languages)),
    check (out_of_hours_behaviour <> 'say_closed' or out_of_hours_message is not null)
);

-- Deny by default: RLS on with no policies. The service connects as the table owner, which bypasses it.
alter table orca_gw.tenants enable row level security;
alter table orca_gw.tenant_channels enable row level security;

-- Supabase API roles must never see this schema (skipped where those roles do not exist, e.g. CI).
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
