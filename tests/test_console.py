"""S6 PR 2: the console. Against a REAL Postgres (auth, panel reads/writes, audit rows,
CSRF, and the "no cookie -> never 200" gate the external verify checks from outside)."""

from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from orca_gateway import deps
from orca_gateway.calls_repo import PgCallsRepository
from orca_gateway.config import get_settings
from orca_gateway.main import app
from orca_gateway.reporting import Reporting
from orca_gateway.tenant_repo import PgTenantRepository
from tests.tenant_fixtures import dental_city, quiet_spa

SECRET = "console-test-secret"


@pytest.fixture
def client(pg_url, monkeypatch) -> TestClient:
    monkeypatch.setenv("ORCA_DATABASE_URL", pg_url)
    monkeypatch.setenv("ORCA_CONSOLE_SECRET", SECRET)
    get_settings.cache_clear()
    deps.get_tenant_store.cache_clear()
    deps.get_tenant_repo.cache_clear()
    deps.get_calls_repo.cache_clear()
    # The session cookie is Secure (S6 brief §1): a plain-http test client would silently drop it
    # on every request after login, so serve the test client over https like the real deploy.
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
async def tenant(pg_url):
    await PgTenantRepository(pg_url).upsert(dental_city())
    await PgTenantRepository(pg_url).upsert(quiet_spa())
    return "dental-city"


async def _seed_call(pg_url, tenant, *, conversation_id="conv-console-1", started_at=None):
    repo = PgCallsRepository(pg_url)
    await repo.record_turn(
        tenant_slug=tenant,
        channel="voice",
        conversation_id=conversation_id,
        agent_id="front-desk",
        elevenlabs_agent_id="el-agent-9",
    )
    await repo.complete_turn(
        conversation_id=conversation_id,
        depth=2,
        usage={"model": "gpt-4o-mini", "prompt_tokens": 500, "completion_tokens": 50},
        user_text="hours today?",
        answer_text="9 to 5.",
        tools=[{"name": "lookup_hours", "status": "ok"}],
        latency_ms=850,
        coalescer={"requests": 1, "restarts": 0, "joins": 0},
    )
    await repo.close_call(conversation_id, "timed_out")
    if started_at is not None:
        with psycopg.connect(pg_url) as conn:
            conn.execute(
                "update orca_gw.calls set started_at = %s where conversation_id = %s",
                (started_at, conversation_id),
            )
    with psycopg.connect(pg_url) as conn:
        row = conn.execute(
            "select id from orca_gw.calls where conversation_id = %s", (conversation_id,)
        ).fetchone()
    return str(row[0])


def _login(client: TestClient) -> None:
    resp = client.post("/console/login", data={"secret": SECRET}, follow_redirects=False)
    assert resp.status_code == 303
    assert "orca_console_session" in resp.cookies


def _extract_csrf(html: str) -> str:
    # Every page's header logout form (and every write form) carries the session's csrf token
    # as a hidden field; the first occurrence is always the header's.
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


def _csrf(client: TestClient) -> str:
    resp = client.get("/console/fleet")
    assert resp.status_code == 200
    return _extract_csrf(resp.text)


# ---- auth gate ----------------------------------------------------------------------------------


def test_console_pages_are_401_or_redirect_without_a_cookie(client):
    paths = ("/console/fleet", "/console/calls", "/console/cost", "/console/config", "/console/")
    for path in paths:
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code in (401, 301, 302, 303, 307, 308), (path, resp.status_code)


def test_login_page_itself_renders_without_a_cookie(client):
    assert client.get("/console/login").status_code == 200


def test_wrong_secret_is_rejected(client):
    resp = client.post("/console/login", data={"secret": "nope"})
    assert resp.status_code == 401
    assert "orca_console_session" not in resp.cookies


def test_right_secret_sets_a_session_cookie_and_then_pages_load(client):
    _login(client)
    assert client.get("/console/fleet").status_code == 200


def test_write_without_csrf_token_is_refused(client, tenant):
    _login(client)
    resp = client.post(
        "/console/fleet/toggle",
        data={
            "csrf_token": "wrong",
            "tenant_slug": tenant,
            "channel": "voice",
            "field": "kill_switch",
            "on": "true",
        },
    )
    assert resp.status_code == 403


# ---- panel 1: fleet -------------------------------------------------------------------------


async def test_fleet_lists_tenant_channels(client, tenant):
    _login(client)
    resp = client.get("/console/fleet")
    assert resp.status_code == 200
    assert "dental-city" in resp.text and "front-desk" in resp.text


async def test_kill_switch_toggle_writes_audit_row_and_is_effective_immediately(
    client, tenant, pg_url
):
    _login(client)
    csrf = _csrf(client)
    resp = client.post(
        "/console/fleet/toggle",
        data={
            "csrf_token": csrf,
            "tenant_slug": tenant,
            "channel": "voice",
            "field": "kill_switch",
            "on": "true",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    cfg = await deps.get_tenant_store().get(tenant)  # cache must already reflect it
    assert cfg.channels["voice"].kill_switch is True
    with psycopg.connect(pg_url) as conn:
        rows = conn.execute(
            "select action, before, after from orca_gw.config_audit "
            "where action = 'kill_switch' order by at desc limit 1"
        ).fetchall()
    assert rows and rows[0][0] == "kill_switch"


# ---- panel 2: calls -------------------------------------------------------------------------


async def test_calls_list_and_detail_show_turns_and_tools(client, tenant, pg_url):
    call_id = await _seed_call(pg_url, tenant)
    _login(client)
    listing = client.get("/console/calls")
    assert listing.status_code == 200
    assert "conv-console-1" not in listing.text  # conversation_id isn't a listed column

    detail = client.get(f"/console/calls/{call_id}")
    assert detail.status_code == 200
    assert "hours today?" in detail.text
    assert "9 to 5." in detail.text
    assert "lookup_hours:ok" in detail.text
    assert "conv-console-1" in detail.text  # the ElevenLabs deep link carries the conversation id


async def test_call_detail_404s_for_unknown_call(client, tenant):
    _login(client)
    assert client.get("/console/calls/00000000-0000-0000-0000-000000000000").status_code == 404


async def test_labeling_a_call_writes_the_label_and_an_audit_row(client, tenant, pg_url):
    call_id = await _seed_call(pg_url, tenant, conversation_id="conv-console-2")
    _login(client)
    resp = client.get(f"/console/calls/{call_id}")
    csrf = _extract_csrf(resp.text)
    post = client.post(
        f"/console/calls/{call_id}/label",
        data={"csrf_token": csrf, "depth": "", "verdict": "good", "note": "sounded right"},
        follow_redirects=False,
    )
    assert post.status_code == 303
    with psycopg.connect(pg_url) as conn:
        rows = conn.execute(
            "select verdict, depth from orca_gw.call_labels where call_id = %s", (call_id,)
        ).fetchall()
        audit = conn.execute(
            "select action from orca_gw.config_audit where action = 'call_label'"
        ).fetchall()
    assert rows == [("good", None)]
    assert len(audit) == 1


async def test_calls_filter_by_tenant_and_date(client, tenant, pg_url):
    await _seed_call(pg_url, tenant, conversation_id="conv-console-3")
    _login(client)
    resp = client.get("/console/calls", params={"tenant_slug": "quiet-spa"})
    assert resp.status_code == 200
    assert "No calls match this filter." in resp.text


# ---- panel 3: cost --------------------------------------------------------------------------


async def test_cost_panel_flags_calls_before_the_metering_fix_cutoff(client, tenant, pg_url):
    before_cutoff = Reporting.METERING_FIX_CUTOFF - timedelta(days=1)
    call_id = await _seed_call(
        pg_url, tenant, conversation_id="conv-console-old", started_at=before_cutoff
    )
    _login(client)
    resp = client.get(f"/console/calls/{call_id}")
    assert "pre-metering-fix" in resp.text or "may be overstated" in resp.text

    cost_resp = client.get("/console/cost")
    assert cost_resp.status_code == 200
    assert "includes pre-metering-fix calls" in cost_resp.text


async def test_cost_export_csv_marks_overstated_rows(client, tenant, pg_url):
    before_cutoff = Reporting.METERING_FIX_CUTOFF - timedelta(days=1)
    await _seed_call(pg_url, tenant, conversation_id="conv-console-csv", started_at=before_cutoff)
    _login(client)
    resp = client.get("/console/cost/export.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    lines = resp.text.strip().splitlines()
    assert lines[0].split(",")[-1] == "overstated"
    body_row = next(r for r in lines[1:] if "conv-console-csv" in r)
    assert body_row.split(",")[-1] == "True"


# ---- panel 4: config ------------------------------------------------------------------------


async def test_config_form_renders_current_values(client, tenant):
    _login(client)
    resp = client.get(f"/console/config/{tenant}/voice")
    assert resp.status_code == 200
    assert 'value="front-desk"' in resp.text


async def test_config_save_updates_fields_and_writes_audit_row(client, tenant, pg_url):
    _login(client)
    get_resp = client.get(f"/console/config/{tenant}/voice")
    csrf = _extract_csrf(get_resp.text)
    resp = client.post(
        f"/console/config/{tenant}/voice",
        data={
            "csrf_token": csrf,
            "timezone": "Asia/Kathmandu",
            "agent_id": "front-desk",
            "elevenlabs_agent_id": "eleven-front-desk-1",
            "is_enabled": "on",
            "languages": "ne,en",
            "default_language": "ne",
            "voice_id": "voice-ne-1",
            "spoken_brand_name": "डेन्टल सिटी",
            "handoff_target": "",
            "out_of_hours_behaviour": "say_closed",
            "out_of_hours_message": "We are closed. Please call back later.",
            "escalation_policy": "none",
            "closed_dates": "",
            "max_session_seconds": "600",
            "daily_spend_cap": "25.00",
            "max_concurrent_runs": "2",
        },
    )
    assert resp.status_code == 200
    assert "Saved." in resp.text
    cfg = await PgTenantRepository(pg_url).load(tenant)
    ch = cfg.channels["voice"]
    assert ch.elevenlabs_agent_id == "eleven-front-desk-1"
    assert ch.out_of_hours_behaviour == "say_closed"
    assert ch.max_session_seconds == 600
    assert ch.max_concurrent_runs == 2
    with psycopg.connect(pg_url) as conn:
        rows = conn.execute(
            "select action from orca_gw.config_audit where action = 'update_channel_config'"
        ).fetchall()
    assert len(rows) == 1


async def test_config_save_takes_effect_within_the_cache_without_waiting_for_ttl(
    client, tenant, pg_url
):
    _login(client)
    # Warm the cache with the pre-edit value.
    cfg = await deps.get_tenant_store().get(tenant)
    assert cfg.channels["voice"].out_of_hours_behaviour != "say_closed"

    get_resp = client.get(f"/console/config/{tenant}/voice")
    csrf = _extract_csrf(get_resp.text)
    client.post(
        f"/console/config/{tenant}/voice",
        data={
            "csrf_token": csrf,
            "timezone": "Asia/Kathmandu",
            "agent_id": "front-desk",
            "is_enabled": "on",
            "languages": "ne,en",
            "default_language": "ne",
            "voice_id": "voice-ne-1",
            "spoken_brand_name": "डेन्टल सिटी",
            "out_of_hours_behaviour": "say_closed",
            "out_of_hours_message": "We are closed.",
            "escalation_policy": "none",
        },
    )
    fresh = await deps.get_tenant_store().get(tenant)
    assert fresh.channels["voice"].out_of_hours_behaviour == "say_closed"  # not stale


async def test_config_save_rejects_invalid_input_without_a_partial_write(client, tenant, pg_url):
    _login(client)
    get_resp = client.get(f"/console/config/{tenant}/voice")
    csrf = _extract_csrf(get_resp.text)
    resp = client.post(
        f"/console/config/{tenant}/voice",
        data={
            "csrf_token": csrf,
            "timezone": "Asia/Kathmandu",
            "agent_id": "front-desk",
            "is_enabled": "on",
            "languages": "ne,en",
            "default_language": "fr",  # not in languages -> invalid
            "voice_id": "voice-ne-1",
            "spoken_brand_name": "डेन्टल सिटी",
            "out_of_hours_behaviour": "handoff_anyway",
            "escalation_policy": "none",
        },
    )
    assert resp.status_code == 400
    cfg = await PgTenantRepository(pg_url).load(tenant)
    assert cfg.channels["voice"].default_language == "ne"  # unchanged


async def test_config_save_with_invalid_channel_field_does_not_partially_save_timezone(
    client, tenant, pg_url
):
    """A bad channel field must not leave a saved timezone change behind: both writes succeed
    together or neither does."""
    _login(client)
    get_resp = client.get(f"/console/config/{tenant}/voice")
    csrf = _extract_csrf(get_resp.text)
    resp = client.post(
        f"/console/config/{tenant}/voice",
        data={
            "csrf_token": csrf,
            "timezone": "America/New_York",  # would change if the write went through
            "agent_id": "front-desk",
            "is_enabled": "on",
            "languages": "ne,en",
            "default_language": "fr",  # invalid: not in languages
            "voice_id": "voice-ne-1",
            "spoken_brand_name": "डेन्टल सिटी",
            "out_of_hours_behaviour": "handoff_anyway",
            "escalation_policy": "none",
        },
    )
    assert resp.status_code == 400
    cfg = await PgTenantRepository(pg_url).load(tenant)
    assert cfg.timezone == "Asia/Kathmandu"  # unchanged, not partially saved
