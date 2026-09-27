"""P5 brief: agents as first-class objects.

Covers migration 0010's backfill (an agent row per distinct pre-existing `agent_id`, all
`public_receptionist`), the agent kill switch's blast radius (every tenant×channel that
references it, independent of the existing per-channel switch, both reverting cleanly), and
`reporting.agent_rows()`. A normal turn still routing (no regression to the live path) is
asserted throughout via `require_serving`, never bypassed.
"""

from __future__ import annotations

import shutil

import psycopg
import pytest

from orca_gateway.migrate import migrate
from orca_gateway.reporting import Reporting
from orca_gateway.tenant_repo import PgTenantRepository
from orca_gateway.tenants import TenantUnavailableError, require_serving
from tests.conftest import MIGRATIONS
from tests.tenant_fixtures import chat, dental_city


async def test_migration_0010_backfills_one_agent_per_distinct_agent_id(pg_url, tmp_path):
    """Reproduces the real deploy shape (brief §2/§3): tenant×channel rows written under
    0001..0009 first, THEN 0010 lands on data that already exists, on a scratch schema so the
    already-fully-migrated `pg_url` fixture's own `orca_gw` schema is untouched."""
    schema = "orca_gw_prod"  # dropped fresh by the pg_url fixture, unused until now
    pre_0010 = tmp_path / "pre"
    pre_0010.mkdir()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name < "0010":
            shutil.copy(path, pre_0010 / path.name)
    migrate(pg_url, pre_0010, schema)

    # Written with plain SQL, not PgTenantRepository -- that repo's INSERT list already includes
    # `agent_ref`, which doesn't exist on this scratch schema until 0010 runs below.
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(
            f"insert into {schema}.tenants (slug, display_name, backend) "
            "values ('dental-city', 'The Dental City', 'zunkiree')"
        )
        conn.execute(
            f"insert into {schema}.tenant_channels "
            "(tenant_id, channel, agent_id, languages, default_language, spoken_brand_name) "
            f"select id, 'voice', 'front-desk', '{{en}}', 'en', 'Dental City' "
            f"from {schema}.tenants where slug = 'dental-city'"
        )
        conn.execute(
            f"insert into {schema}.tenant_channels "
            "(tenant_id, channel, agent_id, languages, default_language, spoken_brand_name) "
            f"select id, 'chat', 'front-desk', '{{en}}', 'en', 'Dental City' "
            f"from {schema}.tenants where slug = 'dental-city'"
        )
        conn.execute(
            f"insert into {schema}.tenants (slug, display_name, backend) "
            "values ('quiet-spa', 'Quiet Spa', 'zunkiree')"
        )
        conn.execute(
            f"insert into {schema}.tenant_channels "
            "(tenant_id, channel, agent_id, languages, default_language, spoken_brand_name) "
            f"select id, 'voice', 'concierge', '{{en}}', 'en', 'Quiet Spa' "
            f"from {schema}.tenants where slug = 'quiet-spa'"
        )

    migrate(pg_url, MIGRATIONS, schema)  # now applies 0010 against that existing data
    repo = PgTenantRepository(pg_url, schema=schema)

    with psycopg.connect(pg_url) as conn:
        rows = conn.execute(
            f"select name, display_name, class, kill_switch from {schema}.agents order by name"
        ).fetchall()
    assert [r[0] for r in rows] == ["concierge", "front-desk"]
    for _name, _display, agent_class, kill_switch in rows:
        assert agent_class == "public_receptionist"  # brief §7 dec. 3: only class seeded
        assert kill_switch is False  # default off: no behaviour change on deploy
    front_desk = next(r for r in rows if r[0] == "front-desk")
    assert front_desk[1] == "Front Desk"  # initcap(replace('front-desk', '-', ' '))

    loaded = await repo.load("dental-city")
    voice_ref = loaded.channels["voice"].agent_ref
    chat_ref = loaded.channels["chat"].agent_ref
    assert voice_ref is not None
    assert voice_ref == chat_ref  # one shared agent object across both channels -- the §2.3 proof
    assert loaded.channels["voice"].agent.name == "front-desk"
    assert loaded.channels["voice"].agent.agent_class == "public_receptionist"
    # the live path is untouched: agent_id (the routing key) still resolves the gate
    assert require_serving(loaded, "voice").agent_id == "front-desk"
    assert require_serving(loaded, "chat").agent_id == "front-desk"


@pytest.fixture
async def dental_city_with_agent(pg_url):
    with psycopg.connect(pg_url, autocommit=True) as conn:
        agent_id = conn.execute(
            "insert into orca_gw.agents (name, display_name, class) "
            "values ('front-desk', 'Front Desk', 'public_receptionist') returning id"
        ).fetchone()[0]
    repo = PgTenantRepository(pg_url)
    tenant = dental_city()
    tenant.channels["chat"] = chat(agent_id="front-desk")
    tenant.channels["voice"].agent_ref = agent_id
    tenant.channels["chat"].agent_ref = agent_id
    await repo.upsert(tenant)
    return repo


async def test_agent_kill_switch_refuses_every_channel_it_serves_and_reverts(
    dental_city_with_agent,
):
    repo = dental_city_with_agent
    loaded = await repo.load("dental-city")
    assert require_serving(loaded, "voice").agent_id == "front-desk"  # no regression
    assert require_serving(loaded, "chat").agent_id == "front-desk"

    assert await repo.set_agent_kill_switch("front-desk", True) is True

    loaded = await repo.load("dental-city")
    for channel in ("voice", "chat"):
        with pytest.raises(TenantUnavailableError, match="^agent kill switch on$"):
            require_serving(loaded, channel)
    assert loaded.channels["voice"].kill_switch is False  # the channel's OWN switch: untouched

    assert await repo.set_agent_kill_switch("front-desk", False) is True
    loaded = await repo.load("dental-city")
    assert require_serving(loaded, "voice").agent_id == "front-desk"
    assert require_serving(loaded, "chat").agent_id == "front-desk"


async def test_channel_kill_switch_stays_independent_of_the_agent_kill_switch(
    dental_city_with_agent,
):
    repo = dental_city_with_agent
    await repo.set_kill_switch("dental-city", "voice", True)
    loaded = await repo.load("dental-city")
    with pytest.raises(TenantUnavailableError, match="^kill switch on$"):
        require_serving(loaded, "voice")
    assert require_serving(loaded, "chat").agent_id == "front-desk"  # chat: unaffected

    await repo.set_kill_switch("dental-city", "voice", False)
    assert await repo.set_agent_kill_switch("front-desk", True) is True
    loaded = await repo.load("dental-city")
    with pytest.raises(TenantUnavailableError, match="^agent kill switch on$"):
        require_serving(loaded, "voice")


async def test_set_agent_kill_switch_is_audited_and_unknown_name_returns_false(pg_url):
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(
            "insert into orca_gw.agents (name, display_name, class) "
            "values ('front-desk', 'Front Desk', 'public_receptionist')"
        )
    repo = PgTenantRepository(pg_url)
    assert await repo.set_agent_kill_switch("no-such-agent", True) is False

    assert await repo.set_agent_kill_switch("front-desk", True) is True
    with psycopg.connect(pg_url) as conn:
        row = conn.execute(
            "select tenant_id, channel, before, after, actor from orca_gw.config_audit "
            "where action = 'agent_kill_switch' order by at desc limit 1"
        ).fetchone()
    assert row[0] is None and row[1] is None  # not scoped to a tenant or channel
    assert row[2] == {"agent": "front-desk", "kill_switch": False}
    assert row[3] == {"agent": "front-desk", "kill_switch": True}
    assert row[4] == "console"


async def test_agent_rows_lists_class_owning_product_version_and_used_by(
    pg_url, dental_city_with_agent,
):
    rows = await Reporting(pg_url).agent_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "front-desk"
    assert row["agent_class"] == "public_receptionist"
    assert row["owning_product"] == "zunkiree"
    assert row["version"] == 1
    assert row["kill_switch"] is False
    assert {(u["tenant_slug"], u["channel"]) for u in row["used_by"]} == {
        ("dental-city", "voice"),
        ("dental-city", "chat"),
    }


async def test_agent_rows_lists_an_agent_with_no_channels_yet(pg_url):
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(
            "insert into orca_gw.agents (name, display_name, class) "
            "values ('default', 'Default', 'public_receptionist')"
        )
    rows = await Reporting(pg_url).agent_rows()
    assert len(rows) == 1
    assert rows[0]["used_by"] == []
