# Tenant config

A tenant is **data**. Two tenants differ by rows in Postgres schema `orca_gw` (`ORCA_DB_SCHEMA`, default
`orca_gw`; prod uses `orca_gw_prod` on the same project -- P2 brief D2), never by code. The schema of
record is `migrations/0001_tenants.sql`; nothing is ever created in `public`.

**Test for any field:** would a spa, a school or a dealership have it too? If not, it belongs in the
backend, not here.

| Table | Holds |
|---|---|
| `tenants` | `slug` (the stable handle), `display_name` (a label), `is_active`, `timezone` (IANA), `backend` (`zunkiree`), `backend_config` (the ONLY place a backend's vocabulary lives: `{site_id, base_url}`) |
| `tenant_channels` | one row per (tenant, channel `voice`/`chat`): `is_enabled`, `agent_id`, `languages`, `default_language`, `voice_id`, `spoken_brand_name`, `handoff_target`, `handoff_hours`, `out_of_hours_behaviour`, `out_of_hours_message`, `escalation_policy`/`_instruction`, `closed_dates`, `closed_weekdays`, caps, `allowed_origins`, `kill_switch`, spoken-fallback flags and wording, phone-guard flag/list/wording |

## What the gateway enforces today, and what it only stores

| Field | Status |
|---|---|
| `backend_config`, `agent_id` | **Enforced**: each tenant reaches its own brain |
| `is_active`, `is_enabled`, `kill_switch` | **Enforced**, fail closed (chat: a 200 SSE `error` frame; voice: 403, or a spoken message with `spoken_kill_switch`), backend never called |
| `spoken_kill_switch` + `kill_switch_message` (P4 A1) | **Voice only, default off.** A kill-switched voice channel speaks the message (`{brand}` -> `spoken_brand_name`; null = built-in wording in `default_language`, English or Nepali) instead of a 403 the caller hears as silence. Unknown, inactive and disabled tenants still get the 403. Chat is unchanged |
| `spoken_error_fallback` + `error_fallback_message` (P4 A1) | **Voice only, default off.** A run timeout or backend error is spoken ("could you say that again?") and recorded as an `error` turn, instead of a 502. Daily-spend-cap refusals are not errors and still 403. Chat is unchanged (a 502 keeps the widget's own fallback working) |
| `closed_dates`, `closed_weekdays`, `handoff_hours` + `out_of_hours_behaviour = say_closed` | **Enforced**: the gateway answers `out_of_hours_message` (`{brand}` -> `spoken_brand_name`) without calling the backend |
| `phone_guard` + `allowed_phone_numbers` + `phone_guard_message` (P4 A3) | **Voice and chat, default off.** Any phone-shaped span (7+ digits, ASCII or Devanagari; not an ISO date or a currency-marked amount) in an answer that is not in the list is **replaced** by the wording (null = built-in "please check the {brand} website for our contact number", English or Nepali). Runs on the answer text before number speech (voice) and before the `token`/`done` frames, including `suggestions` (chat). Also allowed: any phone-shaped number the caller said in this conversation (voice: the request's user messages; chat: the current question plus the stored user turns, only the current question if history is unreachable). Identifiers (a digit run hyphen-joined to letters, e.g. `BK-20260924-0002`) and dates (`2026-10-20`, `20 10 2026`, `20.10.2026`) are never phone-shaped. Dot-separated numbers with 2+ dots (`01.441.2345`) are guarded; one dot is a decimal. Empty list + on = every number the caller did not say is replaced (fail closed). Formatting and leading zeros are ignored; an explicit `+country code` on one side is forgiven. Entries need 7+ digits (rejected at save). Any number a tenant wants said must be listed, including one inside `out_of_hours_message`. Logs `[PHONE-GUARD] replaced tenant= channel= count=`, never a digit. Short codes (<7 digits) are out of scope. The stored transcript keeps the model's own text |
| `out_of_hours_behaviour = take_message` / `handoff_anyway` (the default) | **Stored; behaves as pass-through** ("answer anyway"). The brain now receives the context to act on them (P4 A2, below), but Orca itself still just passes through |
| `voice_id`, `languages`, `default_language` | **Stored** as config of record for provisioning; the voice platform chooses the voice, not the gateway |
| `allowed_origins` | **Enforced for `chat` only** (P3 brief A2): `POST /v1/widget/stream` rejects (403, no CORS header) any request whose `Origin` isn't listed, before any backend call. Stored but unused for `voice`, which has no browser origin |
| `per_caller_rate_limit` | **Enforced for `chat` only** (P3 brief A2): turns per rolling minute, checked per `session_id` and per client IP independently. Stored but unenforced for `voice` |
| `escalation_*`, `handoff_target`, `max_session_seconds`, `daily_spend_cap` | **Stored**; enforced by later slices (metering) |

## Tenant context sent to the brain (P4 A2)

Every backend turn, voice and chat, carries `channel_open`, `closed_reason` (`closed_date` /
`closed_weekday` / `outside_handoff_hours` / null, from `availability()`), `spoken_brand_name` and
`handoff_target` (nullable), read from the channel row the request was already gated on. It is additive
and unflagged: a brain that does not read a field ignores it, and Orca's own behaviour is unchanged.

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
