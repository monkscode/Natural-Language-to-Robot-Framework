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


def _insert_hint(conn, text, domain, scope, org_id, url=None):
    conn.execute(
        "INSERT INTO nl_feedback_corrections "
        "(feedback_text, category, scope, domain, url, evidence_count, "
        " org_id, created_at, last_seen) "
        "VALUES (%s, 'timing', %s, %s, %s, 1, %s, now()::text, now()::text)",
        (text, scope, domain, url, org_id),
    )


def test_ensure_schema_upgrades_existing_v17_db_to_org_aware_dedup():
    """v18 on an EXISTING, populated v17 database: the org-blind UNIQUE
    constraint must be dropped and replaced by the org-aware unique indexes,
    with legacy rows in place.

    The faithfulness of the rewound v17 state is proven inside the test: before
    the upgrade, a second org's identical (text, domain, scope) insert must FAIL
    on the old constraint — the exact production defect — and succeed after.
    """
    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    try:
        admin.execute(f"DROP SCHEMA IF EXISTS {_UPGRADE_SCHEMA} CASCADE")
        admin.execute(f"CREATE SCHEMA {_UPGRADE_SCHEMA}")
        dsn = settings.DATABASE_URL + f"?options=-c%20search_path%3D{_UPGRADE_SCHEMA},public"
        raw = psycopg.connect(dsn, autocommit=True)
        try:
            # 1) Build current schema, then rewind to a faithful v17: re-add the
            #    old org-blind constraint, drop the v18 indexes, record <= v17.
            pg_schema.ensure_schema(raw)
            raw.execute("DROP INDEX IF EXISTS uq_nlfc_dedup_general")
            raw.execute("DROP INDEX IF EXISTS uq_nlfc_dedup_url")
            raw.execute(
                "ALTER TABLE nl_feedback_corrections "
                "ADD CONSTRAINT nl_feedback_corrections_feedback_text_domain_scope_key "
                "UNIQUE (feedback_text, domain, scope)"
            )
            raw.execute("DELETE FROM schema_version WHERE version > 17")

            # 2) Seed pre-upgrade data and prove the rewound state reproduces
            #    the defect: org-B's identical key is rejected by the constraint.
            _insert_hint(raw, "wait for spinner", "shop.test", "domain", "org-A")
            with pytest.raises(psycopg.errors.UniqueViolation):
                _insert_hint(raw, "wait for spinner", "shop.test", "domain", "org-B")

            # 3) Upgrade.
            pg_schema.ensure_schema(raw)

            # 4) Constraint gone, both v18 indexes present, v18 recorded.
            constraints = {r[0] for r in raw.execute(
                "SELECT constraint_name FROM information_schema.table_constraints "
                "WHERE table_schema = %s AND table_name = 'nl_feedback_corrections'",
                (_UPGRADE_SCHEMA,),
            ).fetchall()}
            assert "nl_feedback_corrections_feedback_text_domain_scope_key" not in constraints, (
                "v18 did not drop the org-blind UNIQUE constraint on an existing DB"
            )
            indexes = {r[0] for r in raw.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = %s AND tablename = 'nl_feedback_corrections'",
                (_UPGRADE_SCHEMA,),
            ).fetchall()}
            assert "uq_nlfc_dedup_general" in indexes and "uq_nlfc_dedup_url" in indexes, (
                f"v18 unique indexes missing after upgrade (got {sorted(indexes)})"
            )
            versions = {r[0] for r in raw.execute("SELECT version FROM schema_version").fetchall()}
            assert 18 in versions, f"schema_version did not advance to 18 (got {sorted(versions)})"

            # 5) Behaviour on the upgraded DB with the legacy row still present:
            #    another org's identical key now inserts...
            _insert_hint(raw, "wait for spinner", "shop.test", "domain", "org-B")
            #    ...while a same-org duplicate is still rejected (dedup intact)...
            with pytest.raises(psycopg.errors.UniqueViolation):
                _insert_hint(raw, "wait for spinner", "shop.test", "domain", "org-A")
            #    ...and url-scoped identical text on two pages of one domain is
            #    now allowed (the old constraint silently blocked page two).
            _insert_hint(raw, "use the id locator", "shop.test", "url", "org-A",
                         url="https://shop.test/cart")
            _insert_hint(raw, "use the id locator", "shop.test", "url", "org-A",
                         url="https://shop.test/checkout")
            with pytest.raises(psycopg.errors.UniqueViolation):
                _insert_hint(raw, "use the id locator", "shop.test", "url", "org-A",
                             url="https://shop.test/cart")
        finally:
            raw.close()
    finally:
        admin.execute(f"DROP SCHEMA IF EXISTS {_UPGRADE_SCHEMA} CASCADE")
        admin.close()


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

            # 3) Every scoped table regained org_id and the schema advanced
            #    past 17. is_shared is added by v17 and dropped again by v20,
            #    so the end state must NOT have it — asserting its presence
            #    here would pin the column this plan removed.
            nlfc = _columns(raw, "nl_feedback_corrections")
            assert "org_id" in nlfc, "v17 migration did not add org_id to the existing nl_feedback_corrections"
            assert "is_shared" not in nlfc, "v20 migration did not drop is_shared"
            for table, _ in _V17_DROPS:
                assert "org_id" in _columns(raw, table), f"v17 migration did not add org_id to existing {table}"
            versions = {r[0] for r in raw.execute("SELECT version FROM schema_version").fetchall()}
            assert 17 in versions, f"schema_version did not advance to 17 (got {sorted(versions)})"
        finally:
            raw.close()
    finally:
        admin.execute(f"DROP SCHEMA IF EXISTS {_UPGRADE_SCHEMA} CASCADE")
        admin.close()
