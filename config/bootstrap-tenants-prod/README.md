# Prod bootstrap tenants

Same shape and same rule as `config/bootstrap-tenants/` (P2 brief A3): bootstrap only **INSERTS**
a tenant whose slug is absent, so it never overwrites a console edit. This directory is deployed
into the **prod** schema (`orca_gw_prod`) only, by the prod dispatch (`docs/DEPLOY.md`); it is
never applied to stage, and stage's `config/bootstrap-tenants/` is never applied to prod.

Bootstrap creates tenants and adds missing channels; it never edits anything that exists — edits
go through the console. For a tenant that already exists, only a **channel that has no row yet**
is inserted (insert-only, `on conflict … do nothing`); tenant-level fields and any existing
channel's fields are ignored even where this file's values differ from what's live. A channel
being added this way must have `kill_switch: true`, or the bootstrap step fails and nothing is
inserted for that tenant.

`dental-city.json` here is **provisional**: `backend_config.base_url` names the clinic lane's
planned internal hostname (P2 brief §3 B2 -- `zunkiree-clinic-prod`, one Zunkiree worker, its own
DB pool slice), which does not exist yet. Confirm the actual hostname/port against B2 before the
first prod deploy that bootstraps this tenant, and correct this file if it differs.

`kill_switch: true` on the voice channel, deliberately: this tenant must not answer a real call
until Sadin has verified the clinic lane, set the prod secrets and is ready for exit test §5.
Flip it from the console (Fleet panel) when that's true, never by editing this file after the
first deploy (an edit here after bootstrap has no effect -- bootstrap never updates an existing
row, by design).
