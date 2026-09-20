-- S5: metering. One row per conversation (idempotency key = conversation_id: a call has many
-- turns and must produce exactly one row), plus a running per-tenant daily spend total that
-- daily_spend_cap enforcement reads before starting a new call's first turn.

-- Static per-channel config, same category as agent_id/voice_id already on this table (S4): which
-- ElevenLabs agent this tenant's voice channel is wired to in ElevenLabs' own dashboard. The
-- X-Orca-Tenant header design already implies a tenant maps to exactly one agent there; this
-- column just names it, so a call row can carry the join key §1.2 needs without inventing a new
-- per-call source for tenant-level data. Nullable: not every tenant/channel has it configured yet.
alter table orca_gw.tenant_channels add column elevenlabs_agent_id text;
--
-- "Ended" has no external signal in this codebase (no telephony hangup, no explicit end-of-call
-- event from the voice platform today): a call is closed by a periodic idle-timeout sweep, not by
-- the request path, when no turn has arrived for its conversation_id in 5 minutes (a real phone
-- call is not silent that long). ended_reason = 'completed' is reserved for when an explicit
-- end-of-call signal exists (telephony hangup, a future ElevenLabs end-of-conversation webhook) --
-- unused by this slice's sweep, which always closes with 'timed_out'. 'kill_switch' is set
-- immediately at request time (a deterministic signal, no need to wait for the sweep) when a
-- turn on an open call is refused because the tenant's kill switch is now on.

create table orca_gw.calls (
    id                    uuid primary key default gen_random_uuid(),
    tenant_id             uuid not null references orca_gw.tenants (id) on delete cascade,
    channel               text not null check (channel in ('voice', 'chat')),
    conversation_id       text not null unique,
    agent_id              text not null,          -- the backend's own agent id (e.g. Zunkiree's)
    elevenlabs_agent_id   text,                    -- join key for STT/TTS reconciliation (nullable: not
                                                    -- always known; see docs/metering-reconciliation.md)
    started_at            timestamptz not null default now(),
    ended_at              timestamptz,             -- null while open
    last_turn_at          timestamptz not null default now(),  -- drives the idle-timeout sweep
    turn_count            integer not null default 0 check (turn_count >= 0),
    -- LLM usage: nullable, never defaulted to 0. null means "unknown" (no usage event arrived
    -- yet, or ever, for this call), not "free". Populated only from a real backend usage event.
    llm_prompt_tokens     integer check (llm_prompt_tokens is null or llm_prompt_tokens >= 0),
    llm_completion_tokens integer check (llm_completion_tokens is null or llm_completion_tokens >= 0),
    llm_model             text,
    -- computed from the above and the hardcoded price table in cost.py; null when the model isn't
    -- in that table, or when token counts themselves are null. Never a guessed number.
    llm_cost_usd          numeric(12, 6) check (llm_cost_usd is null or llm_cost_usd >= 0),
    -- reconciled in from outside the gateway (ElevenLabs), not computed here. See §1.2 of the S5
    -- brief and docs/metering-reconciliation.md for whether/how that join is possible today.
    stt_minutes           numeric(10, 2) check (stt_minutes is null or stt_minutes >= 0),
    tts_characters        integer check (tts_characters is null or tts_characters >= 0),
    -- zero by design: no telephony connector exists yet (K1 open). The column exists so the
    -- schema doesn't change shape when one lands.
    telephony_minutes     numeric(10, 2) check (telephony_minutes is null or telephony_minutes >= 0),
    ended_reason          text check (ended_reason in ('completed', 'timed_out', 'kill_switch', 'error')),
    created_at            timestamptz not null default now(),
    updated_at            timestamptz not null default now(),
    check (ended_at is null or ended_reason is not null),
    check (ended_at is not null or ended_reason is null)
);

create index calls_open_idx on orca_gw.calls (last_turn_at) where ended_at is null;
create index calls_tenant_started_idx on orca_gw.calls (tenant_id, started_at);

-- One row per (tenant, date), incremented as calls close. A running total, not a ledger: this is
-- what daily_spend_cap reads before a new call's first turn, using whatever cost signal is real
-- today (llm_cost_usd is currently the only one). The date is the call's started_at date in UTC --
-- deliberately not the tenant's local timezone, so the cap check never has to load tenant config
-- twice in the same code path; a tenant near midnight local time may see the cap reset a few hours
-- off from their own day, which is an acceptable, documented approximation for a spend cap.
-- llm_cost_usd is the sum of only the calls that HAD a real cost; a call that closed with a null
-- llm_cost_usd (no usage event ever arrived, or an unpriced model) adds to unpriced_call_count
-- instead, never to llm_cost_usd as a fabricated zero. "Total known spend was $X across N calls,
-- and Y more calls of unknown cost" is the honest shape; collapsing an unknown into $0 would make
-- daily_spend_cap silently under-count and would make this table lie by omission.
create table orca_gw.tenant_daily_spend (
    tenant_id           uuid not null references orca_gw.tenants (id) on delete cascade,
    spend_date          date not null,
    llm_cost_usd        numeric(12, 6) not null default 0 check (llm_cost_usd >= 0),
    call_count          integer not null default 0 check (call_count >= 0),
    unpriced_call_count integer not null default 0 check (unpriced_call_count >= 0),
    updated_at          timestamptz not null default now(),
    primary key (tenant_id, spend_date)
);

alter table orca_gw.calls enable row level security;
alter table orca_gw.tenant_daily_spend enable row level security;

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
