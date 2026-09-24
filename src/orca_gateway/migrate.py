"""Migration runner: plain, checked-in .sql files applied in order to a schema (`orca_gw` by
default; P2 brief D2 gives prod its own, `orca_gw_prod`, via `ORCA_DB_SCHEMA`).

    python -m orca_gateway.migrate [--dir migrations] [--bootstrap config/bootstrap-tenants]

Each file is applied once, in its own transaction, under an advisory lock, and recorded with a
checksum; editing an already-applied file is an error (write a new migration instead). The
checksum is always taken over the file's UNCHANGED bytes (the `orca_gw` literal in each .sql
file is never edited) -- only the SQL actually sent to Postgres is rewritten per schema, via
`db_schema.rewrite()`. That keeps a schema's already-applied checksums valid regardless of which
other schema this runner has also been pointed at, and means stage's checksums are unaffected
by prod existing at all.
Bootstrap only INSERTS tenants whose slug is absent, so it can never overwrite console edits.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import zlib
from pathlib import Path

import psycopg

from orca_gateway.db_schema import rewrite, validate_schema
from orca_gateway.tenant_repo import PgTenantRepository
from orca_gateway.tenants import TenantConfig

# Arbitrary, stable; serialises concurrent runners. Must fit int4 (both args of the two-key
# pg_advisory_lock(int, int) overload are int4, not bigint).
_LOCK_KEY = 720_411


def _lock_id_for_schema(schema: str) -> int:
    """The two-key form of the advisory lock: `_LOCK_KEY` identifies "this migrator" and this
    second key identifies the schema, so a stage deploy and a prod deploy (same Supabase
    project, different schema) never block on each other, while two runners against the SAME
    schema still serialise exactly as before."""
    return zlib.crc32(schema.encode()) & 0x7FFFFFFF


def migrate(database_url: str, directory: Path, schema: str = "orca_gw") -> list[str]:
    """Runs under a session-level advisory lock, so concurrent deploys serialise rather than
    race. MUST be autocommit: a non-autocommit connection leaves pg_advisory_lock's implicit
    transaction open while it blocks, and the read of `schema_migrations` taken once the lock is
    granted can still see the pre-block snapshot rather than the lock holder's just-committed
    rows -- a waiter then tries to re-run an already-applied migration and crashes on the
    duplicate CREATE TABLE. Reproduced and fixed 2026-09-20 (see test_migrate_concurrency.py);
    autocommit=True (a fresh statement, hence a fresh snapshot, on every call) closes it."""
    validate_schema(schema)
    applied_now: list[str] = []
    with psycopg.connect(
        database_url, prepare_threshold=None, connect_timeout=10, autocommit=True
    ) as conn:
        conn.execute("select pg_advisory_lock(%s, %s)", (_LOCK_KEY, _lock_id_for_schema(schema)))
        try:
            conn.execute(rewrite("create schema if not exists orca_gw", schema))
            conn.execute(
                rewrite(
                    "create table if not exists orca_gw.schema_migrations ("
                    "version text primary key, checksum text not null, "
                    "applied_at timestamptz not null default now())",
                    schema,
                )
            )
            done = dict(
                conn.execute(
                    rewrite("select version, checksum from orca_gw.schema_migrations", schema)
                )
            )
            for path in sorted(directory.glob("*.sql")):
                sql = path.read_text()
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                if path.name in done:
                    if done[path.name] != checksum:
                        raise SystemExit(f"migration {path.name} was edited after being applied")
                    continue
                with conn.transaction():
                    conn.execute(rewrite(sql, schema))
                    conn.execute(
                        rewrite(
                            "insert into orca_gw.schema_migrations (version, checksum) "
                            "values (%s, %s)",
                            schema,
                        ),
                        (path.name, checksum),
                    )
                applied_now.append(path.name)
        finally:
            conn.execute(
                "select pg_advisory_unlock(%s, %s)", (_LOCK_KEY, _lock_id_for_schema(schema))
            )
    return applied_now


async def bootstrap(
    database_url: str, directory: Path, schema: str = "orca_gw"
) -> tuple[list[str], list[str]]:
    """Returns `(created tenants, added channels)`. A tenant is created only if its slug was
    absent (unchanged from before); for a tenant that already existed, any channel in its JSON
    that has no row yet is added (`slug/channel` entries) -- see `insert_missing_channels`.
    Tenant-level fields of an existing tenant are never touched; edits go through the console."""
    repo = PgTenantRepository(database_url, schema=schema)
    created: list[str] = []
    added: list[str] = []
    for path in sorted(directory.glob("*.json")):
        cfg = TenantConfig.model_validate(json.loads(path.read_text()))
        if await repo.insert_if_missing(cfg):
            created.append(cfg.slug)
        else:
            for channel in await repo.insert_missing_channels(cfg):
                added.append(f"{cfg.slug}/{channel}")
    return created, added


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="migrations")
    parser.add_argument("--bootstrap", default=None)
    args = parser.parse_args()
    url = os.environ.get("ORCA_DATABASE_URL")
    if not url:
        sys.exit("ORCA_DATABASE_URL is not set")
    schema = os.environ.get("ORCA_DB_SCHEMA", "orca_gw")
    applied = migrate(url, Path(args.dir), schema)
    print(f"applied migrations to schema {schema!r}: {applied or 'none (up to date)'}")
    if args.bootstrap:
        created, added = asyncio.run(bootstrap(url, Path(args.bootstrap), schema))
        print(f"created tenants: {created or 'none'}")
        print(f"added channels: {added or 'none'}")


if __name__ == "__main__":
    main()
