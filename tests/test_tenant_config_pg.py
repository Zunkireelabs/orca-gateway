"""Against a REAL Postgres: migrations from empty, and the write/read/invalidate/read round trip."""

import json

import psycopg

from orca_gateway.migrate import bootstrap, migrate
from orca_gateway.tenant_repo import PgTenantRepository
from orca_gateway.tenants import TenantStore
from tests.conftest import MIGRATIONS
from tests.tenant_fixtures import dental_city, quiet_spa


def test_migrations_run_from_empty_create_only_orca_gw_and_are_idempotent(pg_url):
    assert migrate(pg_url, MIGRATIONS) == []  # second run: nothing to do
    with psycopg.connect(pg_url) as conn:
        tables = {
            r[0]: r[1]
            for r in conn.execute(
                "select table_name, table_schema from information_schema.tables "
                "where table_schema in ('orca_gw', 'public')"
            )
        }
        assert set(tables) == {
            "tenants",
            "tenant_channels",
            "calls",
            "tenant_daily_spend",
            "schema_migrations",
        }
        assert set(tables.values()) == {"orca_gw"}  # nothing created in public
        rls = dict(
            conn.execute(
                "select relname, relrowsecurity from pg_class c join pg_namespace n "
                "on n.oid = c.relnamespace where n.nspname = 'orca_gw' and relkind = 'r' "
                "and relname in ('tenants', 'tenant_channels', 'calls', 'tenant_daily_spend')"
            )
        )
        assert rls == {
            "tenants": True,
            "tenant_channels": True,
            "calls": True,
            "tenant_daily_spend": True,
        }


def test_editing_an_applied_migration_is_refused(pg_url, tmp_path):
    (tmp_path / "0001_tenants.sql").write_text("select 1;")  # different content, same name
    import pytest

    with pytest.raises(SystemExit, match="edited after being applied"):
        migrate(pg_url, tmp_path)


async def test_config_round_trips_write_read_invalidate_read(pg_url):
    repo = PgTenantRepository(pg_url)
    await repo.upsert(dental_city())
    await repo.upsert(quiet_spa())
    store = TenantStore(repo, ttl_s=1000)

    first = await store.get("quiet-spa")
    assert first == quiet_spa()  # every field survives the round trip, incl. dates and hours
    assert first.channels["voice"].kill_switch is False

    await repo.set_kill_switch("quiet-spa", "voice", True)  # the edit
    assert (await store.get("quiet-spa")).channels["voice"].kill_switch is False  # cached
    store.invalidate("quiet-spa")
    assert (await store.get("quiet-spa")).channels["voice"].kill_switch is True
    assert await store.get("does-not-exist") is None


async def test_constraints_reject_inconsistent_rows_at_the_database(pg_url):
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(
            "insert into orca_gw.tenants (slug, display_name, backend) values "
            "('t', 'T', 'zunkiree')"
        )
        import pytest

        with pytest.raises(psycopg.errors.CheckViolation):  # say_closed without a message
            conn.execute(
                "insert into orca_gw.tenant_channels (tenant_id, channel, languages, "
                "default_language, spoken_brand_name, out_of_hours_behaviour) "
                "select id, 'voice', '{en}', 'en', 'T', 'say_closed' from orca_gw.tenants"
            )
        with pytest.raises(psycopg.errors.CheckViolation):  # bad slug
            conn.execute(
                "insert into orca_gw.tenants (slug, display_name, backend) values "
                "('Bad Slug', 'x', 'zunkiree')"
            )


async def test_bootstrap_inserts_missing_tenants_and_never_overwrites_edits(pg_url, tmp_path):
    (tmp_path / "dental-city.json").write_text(dental_city().model_dump_json())
    assert await bootstrap(pg_url, tmp_path) == ["dental-city"]

    repo = PgTenantRepository(pg_url)
    await repo.set_kill_switch("dental-city", "voice", True)  # an edit made after bootstrap
    assert await bootstrap(pg_url, tmp_path) == []  # already present: untouched
    assert (await repo.load("dental-city")).channels["voice"].kill_switch is True
    json.loads((tmp_path / "dental-city.json").read_text())  # file itself is plain config data
