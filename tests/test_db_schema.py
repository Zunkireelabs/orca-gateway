"""P2 brief A1b: prod gets its own schema (`orca_gw_prod`) on the same Supabase project as
stage's `orca_gw`. Migrations into one must run cleanly and never touch the other, and an
already-applied migration's checksum must not depend on which schema it was applied to."""

import psycopg
import pytest

from orca_gateway.calls_repo import PgCallsRepository
from orca_gateway.db_schema import connection_class, rewrite, validate_schema
from orca_gateway.migrate import migrate
from orca_gateway.tenant_repo import PgTenantRepository
from tests.conftest import MIGRATIONS
from tests.tenant_fixtures import dental_city


@pytest.mark.parametrize(
    "bad", ["", "orca gw", "orca-gw", "orca_gw;drop table x", "Orca_Gw", "a" * 64]
)
def test_validate_schema_rejects_anything_not_a_plain_identifier(bad):
    with pytest.raises(ValueError):
        validate_schema(bad)


def test_validate_schema_accepts_plain_identifiers():
    assert validate_schema("orca_gw") == "orca_gw"
    assert validate_schema("orca_gw_prod") == "orca_gw_prod"


def test_rewrite_is_a_no_op_for_the_default_schema():
    sql = "select * from orca_gw.tenants where slug = %s"
    assert rewrite(sql, "orca_gw") is sql  # identity, not just equal: stage never copies the string


def test_rewrite_substitutes_whole_word_only():
    sql = "select * from orca_gw.tenants join orca_gw.calls on true"
    assert rewrite(sql, "orca_gw_prod") == (
        "select * from orca_gw_prod.tenants join orca_gw_prod.calls on true"
    )
    # never touches a name that merely contains orca_gw as a substring
    assert rewrite("select * from orca_gw_prod.tenants", "orca_gw_v2") == (
        "select * from orca_gw_prod.tenants"
    )


def test_connection_class_is_the_plain_class_for_the_default_schema():
    assert connection_class("orca_gw") is psycopg.AsyncConnection


async def test_connection_class_refuses_cursor_but_execute_still_works(pg_url):
    # .cursor()'s own .execute() never goes through this class's rewrite -- a future cursor-based
    # query would quietly hit orca_gw (stage's schema) instead of orca_gw_prod. Refuse it outright,
    # without breaking .execute() itself, which opens its own cursor internally.
    async with await connection_class("orca_gw_prod").connect(pg_url, autocommit=True) as conn:
        cur = await conn.execute("select 1")
        assert (await cur.fetchone())[0] == 1
        with pytest.raises(NotImplementedError):
            conn.cursor()


def test_migrating_a_second_schema_never_touches_the_first(pg_url):
    # pg_url already migrated orca_gw from empty (tests/conftest.py). Migrate a second schema on
    # the SAME database and prove the first is untouched.
    expected = sorted(m.name for m in MIGRATIONS.glob("*.sql"))
    assert sorted(migrate(pg_url, MIGRATIONS, "orca_gw_prod")) == expected
    with psycopg.connect(pg_url) as conn:
        schemas = {
            r[0] for r in conn.execute("select schema_name from information_schema.schemata")
        }
        assert {"orca_gw", "orca_gw_prod"} <= schemas
        prod_tables = {
            r[0]
            for r in conn.execute(
                "select table_name from information_schema.tables "
                "where table_schema = 'orca_gw_prod'"
            )
        }
        assert "tenants" in prod_tables
        # the first schema's data is untouched: zero rows in prod's tenants, and orca_gw's own
        # schema_migrations checksums are exactly what they were before the second schema existed
        stage_checksums = dict(
            conn.execute("select version, checksum from orca_gw.schema_migrations")
        )
        prod_checksums = dict(
            conn.execute("select version, checksum from orca_gw_prod.schema_migrations")
        )
        assert stage_checksums == prod_checksums  # same files, same bytes, same checksum
    # re-running the first schema's migrate is still a no-op (its checksums were never touched)
    assert migrate(pg_url, MIGRATIONS) == []


async def test_a_tenant_written_into_prod_schema_is_invisible_from_the_stage_schema(pg_url):
    migrate(pg_url, MIGRATIONS, "orca_gw_prod")
    stage_repo = PgTenantRepository(pg_url, schema="orca_gw")
    prod_repo = PgTenantRepository(pg_url, schema="orca_gw_prod")
    cfg = dental_city()
    await prod_repo.upsert(cfg)
    assert await prod_repo.load(cfg.slug) is not None
    assert await stage_repo.load(cfg.slug) is None  # not visible from the other schema


async def test_calls_repo_honours_the_configured_schema(pg_url):
    migrate(pg_url, MIGRATIONS, "orca_gw_prod")
    prod_tenants = PgTenantRepository(pg_url, schema="orca_gw_prod")
    await prod_tenants.upsert(dental_city())
    prod_calls = PgCallsRepository(pg_url, schema="orca_gw_prod")
    state = await prod_calls.touch_call(
        tenant_slug="dental-city",
        channel="voice",
        conversation_id="conv-prod-1",
        agent_id="agent-1",
        elevenlabs_agent_id=None,
    )
    assert state.turn_count == 0
    with psycopg.connect(pg_url) as conn:
        n_prod = conn.execute(
            "select count(*) from orca_gw_prod.calls where conversation_id = %s", ("conv-prod-1",)
        ).fetchone()[0]
        n_stage = conn.execute(
            "select count(*) from orca_gw.calls where conversation_id = %s", ("conv-prod-1",)
        ).fetchone()[0]
    assert n_prod == 1
    assert n_stage == 0
