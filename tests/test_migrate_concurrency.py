"""Two deploys can race (no branch protection serialises PRs, and the workflow's own
concurrency group does not cover a manual re-run or a second repo). The advisory lock must
make that safe: a waiter must never see a stale (pre-commit) view of schema_migrations and
try to re-apply what the lock holder just applied."""

import concurrent.futures

import psycopg

from orca_gateway.migrate import migrate


def test_concurrent_migrate_runs_never_double_apply(pg_url, tmp_path):
    # `pg_url` already ran the real migrations from empty; race a NEW, still-unapplied one, with
    # a hold so the first runner is still holding the lock when the others start (without a hold
    # every runner but one always found the work already done, and the race never fired).
    (tmp_path / "0002_slow.sql").write_text("select pg_sleep(1);")

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: migrate(pg_url, tmp_path), range(3)))

    # Exactly one runner did the work; the other two found it already done. No exception, no
    # double CREATE TABLE, no double INSERT into schema_migrations.
    assert sorted(results, key=len) == [[], [], ["0002_slow.sql"]]
    with psycopg.connect(pg_url) as conn:
        versions = {r[0] for r in conn.execute("select version from orca_gw.schema_migrations")}
    assert versions == {"0001_tenants.sql", "0002_calls.sql", "0002_slow.sql"}
