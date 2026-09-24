# Stage bootstrap tenants

Applied into the **stage** schema (`orca_gw`) only, by the stage deploy's migration step.

Bootstrap creates tenants and adds missing channels; it never edits anything that exists — edits
go through the console.

- A tenant whose slug is absent is created with every channel in its JSON
  (`PgTenantRepository.insert_if_missing`).
- A tenant whose slug already exists is otherwise left alone: **tenant-level fields** (display
  name, backend, `backend_config`) in this file are ignored for it, exactly as today. Only a
  **channel that has no row yet** for that tenant is inserted
  (`PgTenantRepository.insert_missing_channels`) -- insert-only, `on conflict … do nothing`, so a
  channel already present is never touched even where this file's values differ from what's live.
  A channel being added this way must have `kill_switch: true`; bootstrap fails the deploy step
  (and inserts nothing for that tenant) if it doesn't.

To change anything about an existing tenant or channel, use the console (Fleet / Config panels),
not this file. Editing this file after the first deploy has no effect on rows that already exist.
