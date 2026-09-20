import os
from pathlib import Path

import psycopg
import pytest

from orca_gateway.migrate import migrate

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


@pytest.fixture
def pg_url():
    """A real Postgres, migrated FROM EMPTY for every test. Set ORCA_TEST_DATABASE_URL (CI does);
    without it the database tests are skipped rather than faked."""
    url = os.environ.get("ORCA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("ORCA_TEST_DATABASE_URL not set")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("drop schema if exists orca_gw cascade")
    migrate(url, MIGRATIONS)
    return url
