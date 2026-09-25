-- P4 brief A3: a code-side phone-number guard. `phone_guard` is a per-channel flag, default OFF
-- (today's behaviour); `allowed_phone_numbers` is the list of numbers the channel may say. With the
-- guard on, every phone-shaped span not in the list is replaced -- an empty list replaces them all.
alter table orca_gw.tenant_channels
    add column phone_guard boolean not null default false,
    add column allowed_phone_numbers text[] not null default '{}',
    add column phone_guard_message text;
