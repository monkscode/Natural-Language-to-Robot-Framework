"""ensure_schema must UPGRADE an existing pre-1c (v16) database to v17.

Regression for the boot failure where a real, already-populated database could
not start under Phase 1c: the baseline DDL ran `CREATE INDEX ... ON
nl_feedback_corrections(org_id, is_shared)` before the column-adding migration,
so on existing tables (where `CREATE TABLE IF NOT EXISTS` cannot add columns)
the index creation failed with "column org_id does not exist", aborting the
upgrade and leaving the learning subsystem dead.

The whole suite never caught this because every other test builds a FRESH
schema (where the baseline CREATE TABLE makes org_id from the first statement,
so the baseline index succeeds). This test exercises the upgrade journey from
an existing v16 schema, which is what real deployments hit.
"""

import psycopg
import pytest

from src.backend.core.config import settings
from src.backend.crew_ai.optimization import pg_schema

pytestmark = pytest.mark.integration

_UPGRADE_SCHEMA = "schema_upgrade_test"

# The 1c additions, per table — what an existing v16 DB is missing.
_V17_DROPS = (
    ("nl_feedback_corrections", ("org_id", "is_shared")),  # drop org_id first (composite index), then is_shared
    ("execution_records", ("org_id",)),
    ("anti_patterns", ("org_id",)),
    ("learning_anchors", ("org_id",)),
    ("execution_embeddings", ("org_id",)),
)


def _columns(conn, table):
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s",
        (_UPGRADE_SCHEMA, table),
    ).fetchall()
    return {r[0] for r in rows}


def test_ensure_schema_upgrades_existing_v16_db():
    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    try:
        admin.execute(f"DROP SCHEMA IF EXISTS {_UPGRADE_SCHEMA} CASCADE")
        admin.execute(f"CREATE SCHEMA {_UPGRADE_SCHEMA}")
        # public is on the path so the pgvector `vector` type resolves.
        dsn = settings.DATABASE_URL + f"?options=-c%20search_path%3D{_UPGRADE_SCHEMA},public"
        raw = psycopg.connect(dsn, autocommit=True)
        try:
            # 1) Build the current schema, then rewind it to a faithful v16 state:
            #    drop the 1c columns (their org_id indexes drop with them) and
            #    record the schema as v16 — exactly the shape of a pre-1c database.
            pg_schema.ensure_schema(raw)
            for table, cols in _V17_DROPS:
                for col in cols:
                    raw.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {col}")
            raw.execute("DELETE FROM schema_version")
            raw.execute(
                "INSERT INTO schema_version (version, description, applied_at) "
                "VALUES (16, 'pre-1c baseline', now()::text)"
            )
            assert "org_id" not in _columns(raw, "nl_feedback_corrections"), "test setup: org_id should be gone"

            # 2) The upgrade. On the buggy code this raises at the baseline
            #    CREATE INDEX on the missing org_id column.
            pg_schema.ensure_schema(raw)

            # 3) Every scoped table regained org_id, hints regained is_shared,
            #    and the schema advanced to 17.
            nlfc = _columns(raw, "nl_feedback_corrections")
            assert "org_id" in nlfc, "v17 migration did not add org_id to the existing nl_feedback_corrections"
            assert "is_shared" in nlfc, "v17 migration did not add is_shared"
            for table, _ in _V17_DROPS:
                assert "org_id" in _columns(raw, table), f"v17 migration did not add org_id to existing {table}"
            versions = {r[0] for r in raw.execute("SELECT version FROM schema_version").fetchall()}
            assert 17 in versions, f"schema_version did not advance to 17 (got {sorted(versions)})"
        finally:
            raw.close()
    finally:
        admin.execute(f"DROP SCHEMA IF EXISTS {_UPGRADE_SCHEMA} CASCADE")
        admin.close()
