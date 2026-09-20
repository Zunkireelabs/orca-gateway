"""Migration runner: plain, checked-in .sql files applied in order to schema `orca_gw`.

    python -m orca_gateway.migrate [--dir migrations] [--bootstrap config/bootstrap-tenants]

Each file is applied once, in its own transaction, under an advisory lock, and recorded with a
checksum; editing an already-applied file is an error (write a new migration instead).
Bootstrap only INSERTS tenants whose slug is absent, so it can never overwrite console edits.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import psycopg

from orca_gateway.tenant_repo import PgTenantRepository
from orca_gateway.tenants import TenantConfig

_LOCK_KEY = 7_204_112_001  # arbitrary, stable; serialises concurrent runners


def migrate(database_url: str, directory: Path) -> list[str]:
    """Runs under a session-level advisory lock, so concurrent deploys serialise rather than
    race. MUST be autocommit: a non-autocommit connection leaves pg_advisory_lock's implicit
    transaction open while it blocks, and the read of `schema_migrations` taken once the lock is
    granted can still see the pre-block snapshot rather than the lock holder's just-committed
    rows -- a waiter then tries to re-run an already-applied migration and crashes on the
    duplicate CREATE TABLE. Reproduced and fixed 2026-09-20 (see test_migrate_concurrency.py);
    autocommit=True (a fresh statement, hence a fresh snapshot, on every call) closes it."""
    applied_now: list[str] = []
    with psycopg.connect(
        database_url, prepare_threshold=None, connect_timeout=10, autocommit=True
    ) as conn:
        conn.execute("select pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            conn.execute("create schema if not exists orca_gw")
            conn.execute(
                "create table if not exists orca_gw.schema_migrations ("
                "version text primary key, checksum text not null, "
                "applied_at timestamptz not null default now())"
            )
            done = dict(conn.execute("select version, checksum from orca_gw.schema_migrations"))
            for path in sorted(directory.glob("*.sql")):
                sql = path.read_text()
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                if path.name in done:
                    if done[path.name] != checksum:
                        raise SystemExit(f"migration {path.name} was edited after being applied")
                    continue
                with conn.transaction():
                    conn.execute(sql)
                    conn.execute(
                        "insert into orca_gw.schema_migrations (version, checksum) values (%s, %s)",
                        (path.name, checksum),
                    )
                applied_now.append(path.name)
        finally:
            conn.execute("select pg_advisory_unlock(%s)", (_LOCK_KEY,))
    return applied_now


async def bootstrap(database_url: str, directory: Path) -> list[str]:
    repo = PgTenantRepository(database_url)
    created: list[str] = []
    for path in sorted(directory.glob("*.json")):
        cfg = TenantConfig.model_validate(json.loads(path.read_text()))
        if await repo.insert_if_missing(cfg):
            created.append(cfg.slug)
    return created


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="migrations")
    parser.add_argument("--bootstrap", default=None)
    args = parser.parse_args()
    url = os.environ.get("ORCA_DATABASE_URL")
    if not url:
        sys.exit("ORCA_DATABASE_URL is not set")
    applied = migrate(url, Path(args.dir))
    print(f"applied migrations: {applied or 'none (up to date)'}")
    if args.bootstrap:
        created = asyncio.run(bootstrap(url, Path(args.bootstrap)))
        print(f"bootstrapped tenants: {created or 'none (all already present)'}")


if __name__ == "__main__":
    main()
