-- P2 brief A5: a per-tenant, per-channel concurrency cap, sitting UNDER the per-environment
-- ceiling (ORCA_VOICE_MAX_CONCURRENT_RUNS). null = no cap of its own for that tenant/channel --
-- still bounded by the environment ceiling, exactly as before this column existed.
alter table orca_gw.tenant_channels
    add column max_concurrent_runs integer
        check (max_concurrent_runs is null or max_concurrent_runs > 0);
