# Tenant config

A tenant is **data**. Two tenants differ by rows in Postgres schema `orca_gw`, never by code. The schema
of record is `migrations/0001_tenants.sql`; nothing is ever created in `public`.

**Test for any field:** would a spa, a school or a dealership have it too? If not, it belongs in the
backend, not here.

| Table | Holds |
|---|---|
| `tenants` | `slug` (the stable handle), `display_name` (a label), `is_active`, `timezone` (IANA), `backend` (`zunkiree`), `backend_config` (the ONLY place a backend's vocabulary lives: `{site_id, base_url}`) |
| `tenant_channels` | one row per (tenant, channel `voice`/`chat`): `is_enabled`, `agent_id`, `languages`, `default_language`, `voice_id`, `spoken_brand_name`, `handoff_target`, `handoff_hours`, `out_of_hours_behaviour`, `out_of_hours_message`, `escalation_policy`/`_instruction`, `closed_dates`, `closed_weekdays`, caps, `kill_switch` |

## What the gateway enforces today, and what it only stores

| Field | Status |
|---|---|
| `backend_config`, `agent_id` | **Enforced**: each tenant reaches its own brain |
| `is_active`, `is_enabled`, `kill_switch` | **Enforced**, fail closed (403), backend never called |
| `closed_dates`, `closed_weekdays`, `handoff_hours` + `out_of_hours_behaviour = say_closed` | **Enforced**: the gateway answers `out_of_hours_message` (`{brand}` -> `spoken_brand_name`) without calling the backend |
| `out_of_hours_behaviour = take_message` / `handoff_anyway` | **Stored; behaves as pass-through.** Acting on them needs the agent to receive channel context, which the seam does not carry yet |
| `voice_id`, `languages`, `default_language` | **Stored** as config of record for provisioning; the voice platform chooses the voice, not the gateway |
| `escalation_*`, `handoff_target`, `max_session_seconds`, `daily_spend_cap`, `per_caller_rate_limit` | **Stored**; enforced by later slices (metering) |

## `closed_dates` / `closed_weekdays` are a STOPGAP

The product a tenant fronts may be unable to express closures, holidays or a weekly schedule at all.
These fields are a channel-level "do not offer, say closed instead" list held here **because of that gap**.
They are **not** the design and **not** authoritative operating hours (the product owns those; reference,
don't copy). When the product can express closures, delete these.

## Caching

Config is cached per slug for `ORCA_TENANT_CACHE_TTL_S` (default 15 s), at most 256 entries. An edit takes
effect within that time, or immediately via `TenantStore.invalidate(slug)`. During a database outage a stale
entry is served for at most 5 minutes; beyond that the tenant fails closed. **A kill switch therefore
takes up to the TTL to bite** unless the cache is invalidated.

## Adding a tenant

Insert rows (a console arrives in a later slice). `config/bootstrap-tenants/*.json` seeds a tenant only if
its slug is absent; it never overwrites an existing row.
