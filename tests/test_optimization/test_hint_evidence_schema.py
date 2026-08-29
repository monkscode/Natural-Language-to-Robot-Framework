"""`hint_evidence` — the independent-source table (T2, schema v19).

The hint lifecycle rules count EVENTS today, so one query re-run five times
looks identical to five different queries agreeing. This table records which
distinct source produced each outcome; T3/T4/T5 read it for diversity while the
event counters keep counting events (they are LLM-prompt and KPI inputs).

Two journeys are pinned, because only one of them exercises migration 19: a
FRESH database (baseline DDL) and an EXISTING v18 database (the migration).
`CREATE ... IF NOT EXISTS` never alters a table that already exists, so a
baseline-only test passes even when the migration entry is missing entirely.

The index set is asserted exactly. `uq_hint_evidence` leads with `hint_id`, so
it already serves every `hint_id`-leading lookup (`_source_counts`, T5's ON
CONFLICT probe, T12's `hint_id IN (...)`) — index-only for the hot
COUNT(DISTINCT). `idx_hint_evidence_source` (schema v22) is a second, partial
index on `(source_hash, bucket) WHERE source_kind = 'workflow'`, added because
`get_corrections_for_run` (`nl_feedback_engine.py`) filters on
`source_hash`/`bucket`/`source_kind` with no `hint_id` and was seq-scanning on
every FeedbackPanel mount — `uq_hint_evidence` cannot serve that lookup since
it does not lead with those columns. Beyond these two, the test fails if a
redundant index is added back.

Referenced by: docs/superpowers/plans/2026-08-26-feedback-integrity-and-org-isolation.md (T2)
Depends on: src/backend/crew_ai/optimization/pg_schema.py
"""

import psycopg
import pytest

from src.backend.core.config import settings
from src.backend.crew_ai.optimization import pg_schema

pytestmark = pytest.mark.integration

_FRESH_SCHEMA = "hint_evidence_fresh_test"
_UPGRADE_SCHEMA = "hint_evidence_upgrade_test"

_EXPECTED_COLUMNS = {
    "id", "hint_id", "source_kind", "source_key", "source_hash",
    "bucket", "created_at",
}


def _schema_conn(admin, name):
    admin.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
    admin.execute(f"CREATE SCHEMA {name}")
    dsn = settings.DATABASE_URL + f"?options=-c%20search_path%3D{name},public"
    return psycopg.connect(dsn, autocommit=True)


def _columns(conn, schema, table):
    return {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s", (schema, table),
    ).fetchall()}


def _indexes(conn, schema, table):
    return {r[0]: r[1] for r in conn.execute(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = %s AND tablename = %s", (schema, table),
    ).fetchall()}


def _insert(conn, hint_id, kind, key, hash_, bucket):
    conn.execute(
        "INSERT INTO hint_evidence "
        "(hint_id, source_kind, source_key, source_hash, bucket, created_at) "
        "VALUES (%s, %s, %s, %s, %s, now()::text)",
        (hint_id, kind, key, hash_, bucket),
    )


def test_schema_version_is_22():
    assert pg_schema.SCHEMA_VERSION == 22


def test_fresh_database_has_hint_evidence():
    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    try:
        conn = _schema_conn(admin, _FRESH_SCHEMA)
        try:
            assert pg_schema.ensure_schema(conn) == 22
            assert _columns(conn, _FRESH_SCHEMA, "hint_evidence") == _EXPECTED_COLUMNS
        finally:
            conn.close()
    finally:
        admin.execute(f"DROP SCHEMA IF EXISTS {_FRESH_SCHEMA} CASCADE")
        admin.close()


def test_existing_v18_database_gains_hint_evidence_via_migration():
    """The journey a real deployment takes. The v18 state is rewound faithfully
    by dropping the table and un-recording v19, so only migration 19 can
    restore it — the baseline DDL cannot alter what already exists."""
    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    try:
        conn = _schema_conn(admin, _UPGRADE_SCHEMA)
        try:
            pg_schema.ensure_schema(conn)
            conn.execute("DROP TABLE IF EXISTS hint_evidence")
            conn.execute("DELETE FROM schema_version WHERE version >= 19")
            assert "hint_evidence" not in {r[0] for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s", (_UPGRADE_SCHEMA,),
            ).fetchall()}

            assert pg_schema.ensure_schema(conn) == 22

            assert _columns(conn, _UPGRADE_SCHEMA, "hint_evidence") == _EXPECTED_COLUMNS
            assert 19 in {r[0] for r in conn.execute(
                "SELECT version FROM schema_version").fetchall()}
        finally:
            conn.close()
    finally:
        admin.execute(f"DROP SCHEMA IF EXISTS {_UPGRADE_SCHEMA} CASCADE")
        admin.close()


def test_one_source_counts_once_per_bucket_but_buckets_are_independent():
    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    try:
        conn = _schema_conn(admin, _FRESH_SCHEMA)
        try:
            pg_schema.ensure_schema(conn)
            _insert(conn, 7, "query", "search for shoes", "h-shoes", "used")

            with pytest.raises(psycopg.errors.UniqueViolation):
                _insert(conn, 7, "query", "search for shoes", "h-shoes", "used")

            # An early failure must not freeze a later success for one source,
            # and a different hint or a different source is a different row.
            _insert(conn, 7, "query", "search for shoes", "h-shoes", "failure")
            _insert(conn, 7, "query", "book a flight", "h-flight", "used")
            _insert(conn, 8, "query", "search for shoes", "h-shoes", "used")
            assert conn.execute(
                "SELECT count(*) FROM hint_evidence").fetchone()[0] == 4
        finally:
            conn.close()
    finally:
        admin.execute(f"DROP SCHEMA IF EXISTS {_FRESH_SCHEMA} CASCADE")
        admin.close()


def test_index_set_is_exactly_the_primary_key_and_the_two_named_indexes():
    """Pins the index-set decision (2026-08-27, revised 2026-08-28 for F3):
    the pkey, the `hint_id`-leading unique index, and the v22 partial index
    that serves `get_corrections_for_run`'s hint_id-less lookup. No other
    index is allowed to reappear."""
    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    try:
        conn = _schema_conn(admin, _FRESH_SCHEMA)
        try:
            pg_schema.ensure_schema(conn)
            idx = _indexes(conn, _FRESH_SCHEMA, "hint_evidence")
            assert set(idx) == {
                "hint_evidence_pkey", "uq_hint_evidence",
                "idx_hint_evidence_source",
            }
            definition = idx["uq_hint_evidence"]
            assert "UNIQUE" in definition
            assert "(hint_id, source_kind, source_hash, bucket)" in definition

            source_idx = idx["idx_hint_evidence_source"]
            assert "(source_hash, bucket)" in source_idx
            assert "WHERE" in source_idx
            assert "source_kind = 'workflow'" in source_idx
        finally:
            conn.close()
    finally:
        admin.execute(f"DROP SCHEMA IF EXISTS {_FRESH_SCHEMA} CASCADE")
        admin.close()
