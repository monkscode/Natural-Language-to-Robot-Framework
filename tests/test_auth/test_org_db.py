"""Integration tests for the org-tenancy schema bootstrap.

Skipped when Postgres is unreachable. Uses the auth_test isolated schema.
"""

import pytest

from src.backend.auth import db as auth_db
from src.backend.auth.org_db import init_org_db

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


def _table_exists(table: str) -> bool:
    with auth_db.get_pool().connection() as conn:
        row = conn.execute("SELECT to_regclass(%s) AS t", (table,)).fetchone()
        return row["t"] is not None


def test_init_org_db_creates_tables():
    init_org_db()
    assert _table_exists("organizations")
    assert _table_exists("org_members")


def test_init_org_db_is_idempotent():
    init_org_db()
    init_org_db()  # second call must not raise
    assert _table_exists("org_members")
