-- P3 brief A2: a public, anonymous, browser-facing channel (the widget) has no shared secret --
-- instead its caller is bound to a list of allowed browser origins, editable/audited in Config
-- like every other channel field. Any channel may set it (not chat-only in the schema, same as
-- every other cap column here), but only chat enforces it today.
alter table orca_gw.tenant_channels
    add column allowed_origins text[] not null default '{}';
