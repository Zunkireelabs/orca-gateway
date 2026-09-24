"""P2 brief A5: a per-tenant, per-channel concurrency cap that sits under the per-environment
ceiling. One tenant must never be able to use another tenant's slots, and stage's ceiling of 1
must still be able to serve two capped-at-1 tenants concurrently (each in its own slot)."""

import asyncio
import time

from orca_gateway.coalescer import TurnCoalescer
from orca_gateway.tenant_concurrency import TenantConcurrencyLimiter


async def _never_gone() -> bool:
    return False


class _Work:
    """Same shape as tests/test_coalescer.py's _Work: tracks how many calls were in flight at
    once, PER TENANT, so a leak across tenants would show up as an inflated max_inflight."""

    def __init__(self, delay: float = 0.15):
        self.delay = delay
        self.inflight = 0
        self.max_inflight = 0
        self.finished: list[str] = []

    async def __call__(self, text: str) -> str:
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(self.delay)
            self.finished.append(text)
            return f"answer:{text}"
        finally:
            self.inflight -= 1


def test_no_configured_cap_is_a_no_op_context_manager():
    limiter = TenantConcurrencyLimiter()
    slot = limiter.slot("dental-city", "voice", None)

    async def run():
        async with slot:
            return "ok"

    assert asyncio.run(run()) == "ok"


def test_same_tenant_and_channel_share_one_semaphore_across_calls():
    limiter = TenantConcurrencyLimiter()
    a = limiter.slot("dental-city", "voice", 2)
    b = limiter.slot("dental-city", "voice", 2)
    assert a is b  # same key, same limit: the same semaphore, not a fresh one per call


def test_different_tenants_or_channels_never_share_a_semaphore():
    limiter = TenantConcurrencyLimiter()
    voice_a = limiter.slot("dental-city", "voice", 1)
    voice_b = limiter.slot("quiet-spa", "voice", 1)
    chat_a = limiter.slot("dental-city", "chat", 1)
    assert voice_a is not voice_b
    assert voice_a is not chat_a


def test_changing_the_limit_replaces_the_semaphore():
    limiter = TenantConcurrencyLimiter()
    before = limiter.slot("dental-city", "voice", 1)
    after = limiter.slot("dental-city", "voice", 2)
    assert before is not after
    assert limiter.slot("dental-city", "voice", 2) is after  # stable once unchanged again


async def test_one_tenant_at_cap_one_never_exceeds_it_even_under_a_higher_environment_ceiling():
    limiter = TenantConcurrencyLimiter()
    coalescer, work = TurnCoalescer(debounce_s=0.0, max_concurrent_runs=5), _Work()
    await asyncio.gather(
        *[
            coalescer.submit(
                f"conv-{i}", 1, "q", work, _never_gone, slot=limiter.slot("dental-city", "voice", 1)
            )
            for i in range(4)
        ]
    )
    assert work.max_inflight == 1  # the environment ceiling (5) never mattered; the tenant cap did
    assert len(work.finished) == 4


async def test_two_tenants_competing_for_slots_never_cross_into_each_others_cap():
    """The scenario the brief calls out by name: dental-city and quiet-spa each capped at 1,
    both hammering the gateway at once, under an environment ceiling of 2 (stage's own ceiling,
    per A2, stays 1 -- this proves the mechanism holds at any environment ceiling >= the sum of
    the tenant caps in play). Each tenant's own max_inflight must never exceed ITS OWN cap,
    and the two tenants must run genuinely concurrently -- one is never blocked waiting on the
    other's slot."""
    limiter = TenantConcurrencyLimiter()
    coalescer = TurnCoalescer(debounce_s=0.0, max_concurrent_runs=2)
    dental_work, spa_work = _Work(delay=0.2), _Work(delay=0.2)

    start = time.monotonic()
    await asyncio.gather(
        *[
            coalescer.submit(
                f"dental-conv-{i}",
                1,
                "q",
                dental_work,
                _never_gone,
                slot=limiter.slot("dental-city", "voice", 1),
            )
            for i in range(3)
        ],
        *[
            coalescer.submit(
                f"spa-conv-{i}",
                1,
                "q",
                spa_work,
                _never_gone,
                slot=limiter.slot("quiet-spa", "voice", 1),
            )
            for i in range(3)
        ],
    )
    elapsed = time.monotonic() - start

    assert dental_work.max_inflight == 1  # never more than dental-city's own cap
    assert spa_work.max_inflight == 1  # never more than quiet-spa's own cap
    assert len(dental_work.finished) == 3 and len(spa_work.finished) == 3
    # If the two tenants shared one slot, six 0.2s runs (three serialised per tenant, but the two
    # tenants ALSO serialised against each other) would take ~6 * 0.2s. They only share the
    # environment ceiling of 2, so dental-city's and quiet-spa's own runs proceed in parallel with
    # each other; only same-tenant runs queue behind their own cap of 1.
    assert elapsed < 3 * 0.2 + 0.15  # well under the fully-serialised bound, with slack


async def test_a_tenant_at_its_own_cap_does_not_starve_another_tenants_environment_slot():
    # A stricter version of the above: dental-city floods far past its cap of 1 while quiet-spa
    # sends a single request under an environment ceiling of 1 (stage's actual ceiling). Since
    # the tenant slot is OUTER (see coalescer._run), dental-city's queued requests wait on ITS OWN
    # semaphore without holding the shared environment slot, so quiet-spa is never starved by it.
    limiter = TenantConcurrencyLimiter()
    coalescer = TurnCoalescer(debounce_s=0.0, max_concurrent_runs=1)
    dental_work, spa_work = _Work(delay=0.2), _Work(delay=0.05)

    dental_tasks = [
        asyncio.create_task(
            coalescer.submit(
                f"dental-conv-{i}",
                1,
                "q",
                dental_work,
                _never_gone,
                slot=limiter.slot("dental-city", "voice", 1),
            )
        )
        for i in range(3)
    ]
    await asyncio.sleep(0.05)  # let dental-city's first run commit and take the env slot
    spa_start = time.monotonic()
    await coalescer.submit(
        "spa-conv-1", 1, "q", spa_work, _never_gone, slot=limiter.slot("quiet-spa", "voice", 1)
    )
    spa_elapsed = time.monotonic() - spa_start
    await asyncio.gather(*dental_tasks)

    assert dental_work.max_inflight == 1
    # quiet-spa still had to wait for the ONE shared environment slot behind dental-city's
    # in-flight run, but not for dental-city's other two QUEUED (not yet running) requests --
    # those wait on dental-city's own semaphore, never on the environment slot.
    assert spa_elapsed < 0.2 + 0.1
