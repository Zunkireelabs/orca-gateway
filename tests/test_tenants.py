from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from orca_gateway.tenants import (
    TenantConfig,
    TenantStore,
    TenantStoreError,
    TenantUnavailableError,
    availability,
    require_serving,
)
from tests.tenant_fixtures import InMemoryRepo, dental_city, quiet_spa, voice

SATURDAY_KTM_MORNING = datetime(2026, 9, 26, 5, 0, tzinfo=UTC)  # 10:45 Sat, Asia/Kathmandu
SUNDAY_KTM_MORNING = datetime(2026, 9, 27, 5, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


# ---- the fail-closed gate ------------------------------------------------------------------
def test_unknown_tenant_fails_closed():
    with pytest.raises(TenantUnavailableError, match="unknown"):
        require_serving(None, "voice")


def test_inactive_disabled_and_killed_all_fail_closed():
    inactive = dental_city().model_copy(update={"is_active": False})
    with pytest.raises(TenantUnavailableError, match="inactive"):
        require_serving(inactive, "voice")
    disabled = dental_city()
    disabled.channels["voice"].is_enabled = False
    with pytest.raises(TenantUnavailableError, match="not enabled"):
        require_serving(disabled, "voice")
    with pytest.raises(TenantUnavailableError, match="not enabled"):
        require_serving(dental_city(), "chat")  # no chat channel row at all
    killed = dental_city()
    killed.channels["voice"].kill_switch = True
    with pytest.raises(TenantUnavailableError, match="kill switch"):
        require_serving(killed, "voice")


def test_healthy_tenant_passes_the_gate():
    assert require_serving(dental_city(), "voice").agent_id == "front-desk"


# ---- availability (channel, not the product's own schedule) --------------------------------
def test_closed_weekday_and_closed_date_and_handoff_window():
    spa = quiet_spa()
    ch = spa.channels["voice"]
    assert availability(spa, ch, SATURDAY_KTM_MORNING).reason == "closed_weekday"
    assert availability(spa, ch, SUNDAY_KTM_MORNING).open
    assert availability(spa, ch, datetime(2026, 10, 21, 5, 0, tzinfo=UTC)).reason == "closed_date"

    ch.handoff_hours = {"6": [["10:00", "18:00"]]}  # Sundays only
    assert availability(spa, ch, SUNDAY_KTM_MORNING).open
    late = datetime(2026, 9, 27, 14, 0, tzinfo=UTC)  # 19:45 local: after the window
    assert availability(spa, ch, late).reason == "outside_handoff_hours"


def test_availability_uses_the_tenant_timezone_not_utc():
    spa = quiet_spa()
    ch = spa.channels["voice"]
    # 19:00 UTC Friday is 00:45 SATURDAY in Kathmandu: closed there, though still Friday in UTC.
    assert (
        availability(spa, ch, datetime(2026, 9, 25, 19, 0, tzinfo=UTC)).reason == "closed_weekday"
    )


# ---- validation ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        dict(default_language="fr"),  # not among languages
        dict(out_of_hours_behaviour="say_closed", out_of_hours_message=None),
        dict(closed_weekdays=[7]),
        dict(handoff_hours={"9": [["10:00", "18:00"]]}),
        dict(handoff_hours={"0": [["10:00"]]}),
        dict(languages=[]),
    ],
)
def test_channel_validation_rejects_inconsistent_config(bad):
    with pytest.raises(ValidationError):
        voice(**bad)


def test_tenant_validation_rejects_bad_slug_and_timezone():
    with pytest.raises(ValidationError):
        TenantConfig(slug="Bad Slug", display_name="x")
    with pytest.raises(ValidationError):
        TenantConfig(slug="ok", display_name="x", timezone="Mars/Olympus")


# ---- the cache: bounded TTL + explicit invalidation ----------------------------------------
async def test_cache_serves_within_ttl_then_refreshes():
    repo, clock = InMemoryRepo(dental_city()), Clock()
    store = TenantStore(repo, ttl_s=15, clock=clock)
    await store.get("dental-city")
    await store.get("dental-city")
    assert repo.loads == 1
    clock.t = 16
    await store.get("dental-city")
    assert repo.loads == 2


async def test_invalidate_makes_an_edit_visible_immediately():
    repo, clock = InMemoryRepo(dental_city()), Clock()
    store = TenantStore(repo, ttl_s=1000, clock=clock)
    assert (await store.get("dental-city")).channels["voice"].kill_switch is False
    repo.rows["dental-city"].channels["voice"].kill_switch = True  # the edit
    assert (await store.get("dental-city")).channels["voice"].kill_switch is False  # still cached
    store.invalidate("dental-city")
    assert (await store.get("dental-city")).channels["voice"].kill_switch is True


async def test_unknown_slug_is_cached_briefly_and_does_not_hammer_the_db():
    repo = InMemoryRepo()
    store = TenantStore(repo, ttl_s=15, clock=Clock())
    for _ in range(5):
        assert await store.get("nope") is None
    assert repo.loads == 1


async def test_cache_is_bounded():
    tenants = [dental_city().model_copy(update={"slug": f"t{i}"}) for i in range(10)]
    store = TenantStore(InMemoryRepo(*tenants), max_entries=3, clock=Clock())
    for t in tenants:
        await store.get(t.slug)
    assert len(store._entries) == 3


async def test_outage_serves_stale_only_within_the_grace_window():
    repo, clock = InMemoryRepo(dental_city()), Clock()
    store = TenantStore(repo, ttl_s=15, stale_grace_s=300, clock=clock)
    await store.get("dental-city")
    repo.fail = True
    clock.t = 100  # expired but inside grace: serve stale, do not fail every live call
    assert (await store.get("dental-city")).slug == "dental-city"
    clock.t = 400  # beyond grace: fail closed rather than serve arbitrarily old config
    with pytest.raises(TenantStoreError):
        await store.get("dental-city")


async def test_outage_with_nothing_cached_fails_closed():
    repo = InMemoryRepo(dental_city())
    repo.fail = True
    with pytest.raises(TenantStoreError):
        await TenantStore(repo, clock=Clock()).get("dental-city")
