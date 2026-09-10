"""tests / test_versions DDL — shape, idempotency and the destructive-statement ban.

_SCHEMA_DDL runs on EVERY RunRegistry() construction, so these statements must
be safe to execute repeatedly against a database that already has data.
"""
import uuid

import psycopg
import pytest

from src.backend.core.config import settings

pytestmark = pytest.mark.integration


@pytest.fixture
def scratch():
    """An empty schema plus a DSN whose search_path points at it alone."""
    name = f"tests_ddl_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    admin.execute(f"CREATE SCHEMA {name}")
    sep = "&" if "?" in settings.DATABASE_URL else "?"
    dsn = settings.DATABASE_URL + f"{sep}options=-c%20search_path%3D{name}"
    try:
        yield name, dsn, admin
    finally:
        admin.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
        admin.close()


def _columns(admin, schema, table):
    return sorted(r[0] for r in admin.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s", (schema, table)
    ).fetchall())


def test_tables_are_created_with_the_specified_columns(scratch):
    name, dsn, admin = scratch
    from src.backend.core.run_registry import RunRegistry
    RunRegistry(dsn=dsn).close()

    assert _columns(admin, name, "tests") == [
        "created_at", "current_version", "group_id", "key_n", "name",
        "org_id", "test_id", "updated_at", "user_email", "user_id",
        "user_query",
    ]
    assert _columns(admin, name, "test_versions") == [
        "created_at", "created_by", "created_by_email", "n", "reason",
        "robot_code", "test_id", "user_query", "version_id",
    ]
    for col in ("test_id", "test_version_id", "ran_as_platform_admin"):
        assert col in _columns(admin, name, "test_runs")


def test_org_id_is_nullable_because_bench_runs_are_token_less(scratch):
    """AUTH_ENFORCED=false writes org_id NULL. A NOT NULL column would make
    test creation raise, and record_start swallows exceptions, so it would
    fail silently and leave an empty Tests page in dev."""
    name, dsn, admin = scratch
    from src.backend.core.run_registry import RunRegistry
    RunRegistry(dsn=dsn).close()

    nullable = admin.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'tests' AND column_name = 'org_id'",
        (name,)).fetchone()[0]
    assert nullable == "YES"


def test_key_is_unique_within_the_org_less_bucket_too(scratch):
    """NULLS NOT DISTINCT — without it (NULL, 1) twice is permitted and key_n
    stops being a key for every bench and dev run."""
    name, dsn, admin = scratch
    from src.backend.core.run_registry import RunRegistry
    RunRegistry(dsn=dsn).close()

    admin.execute(
        f"INSERT INTO {name}.tests (test_id, org_id, key_n)"
        " VALUES ('t1', NULL, 1)")
    with pytest.raises(psycopg.errors.UniqueViolation):
        admin.execute(
            f"INSERT INTO {name}.tests (test_id, org_id, key_n)"
            " VALUES ('t2', NULL, 1)")


def test_ddl_is_idempotent_and_keeps_its_rows(scratch):
    """Two constructions, as a process restart or a second fixture would do."""
    name, dsn, admin = scratch
    from src.backend.core.run_registry import RunRegistry
    RunRegistry(dsn=dsn).close()
    admin.execute(
        f"INSERT INTO {name}.tests (test_id, org_id, key_n, user_query) "
        "VALUES ('keep-me', 'org-a', 1, 'q')")
    RunRegistry(dsn=dsn).close()

    assert admin.execute(
        f"SELECT count(*) FROM {name}.tests"
        " WHERE test_id = 'keep-me'").fetchone()[0] == 1


def test_schema_ddl_never_drops_the_new_tables():
    """A DROP here would delete every customer test on each process start.

    The tuple already carries exactly ONE DROP TABLE and it must stay: the
    guarded pre-release run_groups drop, which fires only on an old-shape
    table holding zero rows. It is written as format('DROP TABLE %s', t)
    with t bound to to_regclass('run_groups'), so it can never name these
    tables. Pinning the count is what makes a NEW one fail loudly here
    instead of silently emptying the Tests page on the next process start.
    """
    from src.backend.core.run_registry import _SCHEMA_DDL
    joined = " ".join(_SCHEMA_DDL).lower()
    assert joined.count("drop table") == 1
    assert "drop table %s', t)" in joined
    for table in ("tests", "test_versions"):
        assert f"drop table {table}" not in joined
        assert f"drop table if exists {table}" not in joined


def test_deleting_a_test_cascades_to_its_versions(scratch):
    name, dsn, admin = scratch
    from src.backend.core.run_registry import RunRegistry
    RunRegistry(dsn=dsn).close()
    admin.execute(
        f"INSERT INTO {name}.tests (test_id, org_id, key_n)"
        " VALUES ('t1', 'o', 1)")
    admin.execute(
        f"INSERT INTO {name}.test_versions (version_id, test_id, n)"
        " VALUES ('v1', 't1', 1)")
    admin.execute(f"DELETE FROM {name}.tests WHERE test_id = 't1'")

    assert admin.execute(
        f"SELECT count(*) FROM {name}.test_versions").fetchone()[0] == 0
