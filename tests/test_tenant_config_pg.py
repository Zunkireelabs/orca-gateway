"""Against a REAL Postgres: migrations from empty, and the write/read/invalidate/read round trip."""

import json

import psycopg
import pytest

from orca_gateway.migrate import bootstrap, migrate
from orca_gateway.tenant_repo import PgTenantRepository
from orca_gateway.tenants import TenantStore
from tests.conftest import MIGRATIONS
from tests.tenant_fixtures import chat, dental_city, quiet_spa, voice


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
            "turns",
            "call_labels",
            "config_audit",
            "schema_migrations",
        }
        assert set(tables.values()) == {"orca_gw"}  # nothing created in public
        rls = dict(
            conn.execute(
                "select relname, relrowsecurity from pg_class c join pg_namespace n "
                "on n.oid = c.relnamespace where n.nspname = 'orca_gw' and relkind = 'r' "
                "and relname in ('tenants', 'tenant_channels', 'calls', 'tenant_daily_spend', "
                "'turns', 'call_labels', 'config_audit')"
            )
        )
        assert rls == {
            "tenants": True,
            "tenant_channels": True,
            "calls": True,
            "tenant_daily_spend": True,
            "turns": True,
            "call_labels": True,
            "config_audit": True,
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


async def test_max_concurrent_runs_round_trips_and_is_audited(pg_url):
    repo = PgTenantRepository(pg_url)
    await repo.upsert(dental_city())
    before, after = await repo.update_channel_config(
        "dental-city", "voice", {"max_concurrent_runs": 3}
    )
    assert before["max_concurrent_runs"] is None
    assert after["max_concurrent_runs"] == 3
    assert (await repo.load("dental-city")).channels["voice"].max_concurrent_runs == 3
    with psycopg.connect(pg_url) as conn:
        row = conn.execute(
            "select before, after from orca_gw.config_audit "
            "where action = 'update_channel_config' order by at desc limit 1"
        ).fetchone()
    assert row[0]["max_concurrent_runs"] is None  # psycopg decodes jsonb to dict directly
    assert row[1]["max_concurrent_runs"] == 3


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
    assert await bootstrap(pg_url, tmp_path) == (["dental-city"], [])

    repo = PgTenantRepository(pg_url)
    await repo.set_kill_switch("dental-city", "voice", True)  # an edit made after bootstrap
    assert await bootstrap(pg_url, tmp_path) == ([], [])  # already present: untouched
    assert (await repo.load("dental-city")).channels["voice"].kill_switch is True
    json.loads((tmp_path / "dental-city.json").read_text())  # file itself is plain config data


async def test_bootstrap_adds_a_missing_channel_to_an_existing_tenant_and_audits_it(
    pg_url, tmp_path
):
    repo = PgTenantRepository(pg_url)
    await repo.upsert(dental_city())  # tenant exists with voice only

    cfg = dental_city()
    cfg.channels["chat"] = chat(kill_switch=True)
    (tmp_path / "dental-city.json").write_text(cfg.model_dump_json())
    assert await bootstrap(pg_url, tmp_path) == ([], ["dental-city/chat"])

    loaded = await repo.load("dental-city")
    assert set(loaded.channels) == {"voice", "chat"}
    assert loaded.channels["chat"].kill_switch is True

    with psycopg.connect(pg_url) as conn:
        row = conn.execute(
            "select before, after, actor from orca_gw.config_audit "
            "where action = 'channel_created' order by at desc limit 1"
        ).fetchone()
    assert row[0] is None
    assert row[1]["channel"] == "chat"
    assert row[2] == "bootstrap"


async def test_bootstrap_never_touches_an_existing_channel_that_differs_in_the_json(
    pg_url, tmp_path
):
    """The test that matters: a channel already present, even with different values in the JSON,
    is left completely alone -- no update, no audit row."""
    repo = PgTenantRepository(pg_url)
    await repo.upsert(dental_city())  # voice: kill_switch False, languages [ne, en]

    cfg = dental_city()
    cfg.channels["voice"] = voice(
        agent_id="front-desk",
        out_of_hours_behaviour="handoff_anyway",
        kill_switch=True,
        languages=["en"],
        default_language="en",
    )
    (tmp_path / "dental-city.json").write_text(cfg.model_dump_json())

    with psycopg.connect(pg_url) as conn:
        audit_count_before = conn.execute(
            "select count(*) from orca_gw.config_audit"
        ).fetchone()[0]

    assert await bootstrap(pg_url, tmp_path) == ([], [])

    loaded = await repo.load("dental-city")
    assert loaded.channels["voice"].kill_switch is False  # untouched
    assert loaded.channels["voice"].languages == ["ne", "en"]  # untouched

    with psycopg.connect(pg_url) as conn:
        audit_count_after = conn.execute("select count(*) from orca_gw.config_audit").fetchone()[
            0
        ]
    assert audit_count_after == audit_count_before  # nothing audited


async def test_bootstrap_rerun_after_adding_a_channel_is_a_noop(pg_url, tmp_path):
    repo = PgTenantRepository(pg_url)
    await repo.upsert(dental_city())

    cfg = dental_city()
    cfg.channels["chat"] = chat(kill_switch=True)
    (tmp_path / "dental-city.json").write_text(cfg.model_dump_json())
    assert await bootstrap(pg_url, tmp_path) == ([], ["dental-city/chat"])
    assert await bootstrap(pg_url, tmp_path) == ([], [])  # re-run: no-op


async def test_bootstrap_refuses_a_new_channel_without_kill_switch_and_inserts_nothing(
    pg_url, tmp_path
):
    repo = PgTenantRepository(pg_url)
    await repo.upsert(dental_city())

    cfg = dental_city()
    cfg.channels["chat"] = chat()  # kill_switch defaults to False
    (tmp_path / "dental-city.json").write_text(cfg.model_dump_json())

    import pytest

    with pytest.raises(ValueError, match="kill_switch"):
        await bootstrap(pg_url, tmp_path)

    loaded = await repo.load("dental-city")
    assert set(loaded.channels) == {"voice"}  # chat was not inserted

    with psycopg.connect(pg_url) as conn:
        count = conn.execute(
            "select count(*) from orca_gw.config_audit where action = 'channel_created'"
        ).fetchone()[0]
    assert count == 0


async def test_insert_missing_channels_runs_against_orca_gw_prod_too(pg_url, tmp_path):
    schema = "orca_gw_prod"
    from orca_gateway.migrate import migrate
    from tests.conftest import MIGRATIONS

    migrate(pg_url, MIGRATIONS, schema)
    repo = PgTenantRepository(pg_url, schema=schema)
    await repo.upsert(dental_city())

    cfg = dental_city()
    cfg.channels["chat"] = chat(kill_switch=True)
    (tmp_path / "dental-city.json").write_text(cfg.model_dump_json())
    assert await bootstrap(pg_url, tmp_path, schema) == ([], ["dental-city/chat"])
    assert set((await repo.load("dental-city")).channels) == {"voice", "chat"}


async def test_spoken_fallback_fields_default_off_round_trip_and_are_audited(pg_url):
    repo = PgTenantRepository(pg_url)
    await repo.upsert(dental_city())
    ch = (await repo.load("dental-city")).channels["voice"]
    assert (ch.spoken_kill_switch, ch.spoken_error_fallback) == (False, False)
    assert (ch.kill_switch_message, ch.error_fallback_message) == (None, None)
    before, after = await repo.update_channel_config(
        "dental-city",
        "voice",
        {"spoken_kill_switch": True, "error_fallback_message": "{brand}: try again."},
    )
    assert before["spoken_kill_switch"] is False and after["spoken_kill_switch"] is True
    ch = (await repo.load("dental-city")).channels["voice"]
    assert ch.spoken_kill_switch is True and ch.spoken_error_fallback is False
    assert ch.error_fallback_message == "{brand}: try again."
    with psycopg.connect(pg_url) as conn:
        row = conn.execute(
            "select before, after from orca_gw.config_audit "
            "where action = 'update_channel_config' order by at desc limit 1"
        ).fetchone()
    assert row[0]["spoken_kill_switch"] is False and row[1]["spoken_kill_switch"] is True


async def test_turns_accept_the_error_and_kill_switch_outcomes(pg_url):
    repo = PgTenantRepository(pg_url)
    await repo.upsert(dental_city())
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(
            "insert into orca_gw.calls (tenant_id, channel, conversation_id, agent_id) "
            "select id, 'voice', 'c-1', 'a' from orca_gw.tenants limit 1"
        )
        for depth, reason in enumerate(("error", "kill_switch"), start=1):
            conn.execute(
                "insert into orca_gw.turns (call_id, depth, ended_by) "
                "select id, %s, %s from orca_gw.calls",
                (depth, reason),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "insert into orca_gw.turns (call_id, depth, ended_by) "
                "select id, 9, 'nonsense' from orca_gw.calls"
            )
