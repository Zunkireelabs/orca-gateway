-- P5 brief: an agent becomes a real, platform-level object (vision §2.3) instead of just the
-- string `tenant_channels.agent_id`. Additive / keep-live: `agent_id` stays the routing key the
-- seam sends unchanged; `agent_ref` is the object link Fleet reads, added beside it. Both
-- kill switches default false, so nothing changes for a single tenant×channel until someone
-- flips one.

create table orca_gw.agents (
    id             uuid primary key default gen_random_uuid(),
    -- the natural key: this is the string that lives in tenant_channels.agent_id today.
    name           text not null unique
                   check (name ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
    display_name   text not null,
    -- §8.9: the two classes NEVER merge. Only public_receptionist has rows this cycle.
    class          text not null
                   check (class in ('public_receptionist', 'operator_copilot')),
    -- passive in P5: documents which product's brain serves this agent; NOT used for routing
    -- until P10 (routing stays per-tenant via tenants.backend). e.g.
    -- {"kind":"backend","backend":"zunkiree"}
    brain_binding  jsonb not null default '{}'::jsonb,
    -- the product this agent belongs to (owns/serves its brain today). Product-blind string.
    owning_product text not null default 'zunkiree',
    -- agent-definition version; passive seed for P7 evals/versioning. Starts at 1.
    version        integer not null default 1 check (version > 0),
    -- P5's per-agent kill switch. Emergency control; blast radius = every tenant×channel using
    -- this agent (see brief §5). Default false = no behaviour change on deploy.
    kill_switch    boolean not null default false,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

-- link tenant_channels to the object WITHOUT disturbing the routing key. Two sources of truth,
-- on purpose and temporarily: agent_id (text) stays what session() sends to the backend;
-- agent_ref (uuid) is the object link Fleet reads. Do NOT make agent_id a FK or drop it here --
-- P10 consolidates onto agent_ref when routing moves to the object.
alter table orca_gw.tenant_channels
    add column agent_ref uuid references orca_gw.agents (id);

-- backfill: one agent per distinct existing agent_id string; everything today is a receptionist.
insert into orca_gw.agents (name, display_name, class)
    select distinct agent_id,
           initcap(replace(agent_id, '-', ' ')),
           'public_receptionist'
    from orca_gw.tenant_channels
on conflict (name) do nothing;

update orca_gw.tenant_channels tc
    set agent_ref = a.id
    from orca_gw.agents a
    where a.name = tc.agent_id;

alter table orca_gw.agents enable row level security;  -- deny-by-default, owner bypasses, as 0001

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
