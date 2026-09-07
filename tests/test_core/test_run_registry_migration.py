"""The one-shot collapse of test_runs onto tests + test_versions.

Every assertion here mirrors a measured fact about the live database
(46 runs -> 21 tests, 45 versions) or a rule from spec section 4.
"""
import uuid

import psycopg
import pytest

from src.backend.core.config import settings
from src.backend.core.run_registry import _MIGRATION_SCALE_LIMIT

pytestmark = pytest.mark.integration


@pytest.fixture
def scratch():
    name = f"tests_mig_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    admin.execute(f"CREATE SCHEMA {name}")
    sep = "&" if "?" in settings.DATABASE_URL else "?"
    dsn = settings.DATABASE_URL + f"{sep}options=-c%20search_path%3D{name}"
    try:
        yield name, dsn, admin
    finally:
        admin.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
        admin.close()


def _bare_schema(admin, name):
    """test_runs as it exists BEFORE this migration, so the seeded rows look
    like real pre-split history."""
    admin.execute(f"""
        CREATE TABLE {name}.test_runs (
            run_id     TEXT PRIMARY KEY,
            user_id    TEXT,
            user_email TEXT,
            user_query TEXT,
            robot_code TEXT,
            rerun_of   TEXT,
            status     TEXT NOT NULL,
            org_id     TEXT,
            error_message TEXT,
            group_id   TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def _seed(admin, name, rows):
    for r in rows:
        admin.execute(
            f"INSERT INTO {name}.test_runs (run_id, user_id, user_query, robot_code,"
            " status, org_id, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (r["run_id"], r.get("user_id", "u1"), r.get("user_query"),
             r.get("robot_code"), r.get("status", "passed"),
             r.get("org_id", "org-a"), r["created_at"]))


def _migrate(dsn):
    from src.backend.core.run_registry import RunRegistry
    RunRegistry(dsn=dsn).close()


def test_runs_sharing_a_query_collapse_into_one_test_with_versions(scratch):
    name, dsn, admin = scratch
    _bare_schema(admin, name)
    _seed(admin, name, [
        {"run_id": "r1", "user_query": "search shoes", "robot_code": "code-1",
         "created_at": "2026-01-01T10:00:00Z"},
        {"run_id": "r2", "user_query": "search shoes", "robot_code": "code-2",
         "created_at": "2026-01-02T10:00:00Z"},
        {"run_id": "r3", "user_query": "open cart", "robot_code": "code-3",
         "created_at": "2026-01-03T10:00:00Z"},
    ])
    _migrate(dsn)

    assert admin.execute(f"SELECT count(*) FROM {name}.tests").fetchone()[0] == 2
    assert admin.execute(f"SELECT count(*) FROM {name}.test_versions").fetchone()[0] == 3
    assert admin.execute(
        f"SELECT count(*) FROM {name}.test_runs WHERE test_id IS NULL").fetchone()[0] == 0
    cv = admin.execute(
        f"SELECT current_version FROM {name}.tests WHERE user_query = 'search shoes'"
    ).fetchone()[0]
    assert cv == 2


def test_each_run_points_at_the_version_built_from_its_own_code(scratch):
    """The collapse is lossless: a result that failed on attempt two still
    names attempt two's code, not the newest."""
    name, dsn, admin = scratch
    _bare_schema(admin, name)
    _seed(admin, name, [
        {"run_id": "r1", "user_query": "q", "robot_code": "code-1",
         "created_at": "2026-01-01T10:00:00Z"},
        {"run_id": "r2", "user_query": "q", "robot_code": "code-2",
         "created_at": "2026-01-02T10:00:00Z"},
    ])
    _migrate(dsn)

    rows = dict(admin.execute(
        f"SELECT r.run_id, v.robot_code FROM {name}.test_runs r "
        f"JOIN {name}.test_versions v ON v.version_id = r.test_version_id").fetchall())
    assert rows == {"r1": "code-1", "r2": "code-2"}


def test_a_code_less_run_attaches_but_produces_no_version(scratch):
    """Owner decision D8. Mirrors the live database's single error row, which
    shares its query with two successful runs."""
    name, dsn, admin = scratch
    _bare_schema(admin, name)
    _seed(admin, name, [
        {"run_id": "ok1", "user_query": "q", "robot_code": "code-1",
         "created_at": "2026-01-01T10:00:00Z"},
        {"run_id": "ok2", "user_query": "q", "robot_code": "code-2",
         "created_at": "2026-01-02T10:00:00Z"},
        {"run_id": "bad", "user_query": "q", "robot_code": None,
         "status": "error", "created_at": "2026-01-03T10:00:00Z"},
    ])
    _migrate(dsn)

    assert admin.execute(f"SELECT count(*) FROM {name}.tests").fetchone()[0] == 1
    assert admin.execute(f"SELECT count(*) FROM {name}.test_versions").fetchone()[0] == 2
    test_id, version_id = admin.execute(
        f"SELECT test_id, test_version_id FROM {name}.test_runs WHERE run_id = 'bad'"
    ).fetchone()
    assert test_id is not None      # it belongs to the test
    assert version_id is None       # but contributed no code


def test_null_queries_never_group_with_each_other(scratch):
    """NULL never equals NULL, so two pasted runs stay two tests."""
    name, dsn, admin = scratch
    _bare_schema(admin, name)
    _seed(admin, name, [
        {"run_id": "p1", "user_query": None, "robot_code": "c1",
         "created_at": "2026-01-01T10:00:00Z"},
        {"run_id": "p2", "user_query": None, "robot_code": "c2",
         "created_at": "2026-01-02T10:00:00Z"},
    ])
    _migrate(dsn)

    assert admin.execute(f"SELECT count(*) FROM {name}.tests").fetchone()[0] == 2


def test_the_same_sentence_from_two_authors_stays_two_tests(scratch):
    name, dsn, admin = scratch
    _bare_schema(admin, name)
    _seed(admin, name, [
        {"run_id": "a1", "user_id": "alice", "user_query": "q",
         "robot_code": "c", "created_at": "2026-01-01T10:00:00Z"},
        {"run_id": "b1", "user_id": "bob", "user_query": "q",
         "robot_code": "c", "created_at": "2026-01-02T10:00:00Z"},
    ])
    _migrate(dsn)

    assert admin.execute(f"SELECT count(*) FROM {name}.tests").fetchone()[0] == 2


def test_keys_are_sequential_per_org_starting_at_one(scratch):
    name, dsn, admin = scratch
    _bare_schema(admin, name)
    _seed(admin, name, [
        {"run_id": "a1", "org_id": "org-a", "user_query": "q1",
         "robot_code": "c", "created_at": "2026-01-01T10:00:00Z"},
        {"run_id": "a2", "org_id": "org-a", "user_query": "q2",
         "robot_code": "c", "created_at": "2026-01-02T10:00:00Z"},
        {"run_id": "b1", "org_id": "org-b", "user_query": "q3",
         "robot_code": "c", "created_at": "2026-01-03T10:00:00Z"},
    ])
    _migrate(dsn)

    assert sorted(r[0] for r in admin.execute(
        f"SELECT key_n FROM {name}.tests WHERE org_id = 'org-a'").fetchall()) == [1, 2]
    assert [r[0] for r in admin.execute(
        f"SELECT key_n FROM {name}.tests WHERE org_id = 'org-b'").fetchall()] == [1]


def test_the_folder_comes_from_the_newest_run_that_has_one(scratch):
    name, dsn, admin = scratch
    _bare_schema(admin, name)
    admin.execute(f"CREATE TABLE {name}.run_groups (group_id TEXT PRIMARY KEY,"
                  " name TEXT NOT NULL, org_id TEXT NOT NULL,"
                  " created_by TEXT, created_at TIMESTAMPTZ DEFAULT now(),"
                  " updated_at TIMESTAMPTZ DEFAULT now())")
    admin.execute(f"INSERT INTO {name}.run_groups (group_id, name, org_id)"
                  " VALUES ('g-old','Old','org-a'), ('g-new','New','org-a')")
    _seed(admin, name, [
        {"run_id": "r1", "user_query": "q", "robot_code": "c",
         "created_at": "2026-01-01T10:00:00Z"},
        {"run_id": "r2", "user_query": "q", "robot_code": "c",
         "created_at": "2026-01-02T10:00:00Z"},
    ])
    admin.execute(f"UPDATE {name}.test_runs SET group_id='g-old' WHERE run_id='r1'")
    admin.execute(f"UPDATE {name}.test_runs SET group_id='g-new' WHERE run_id='r2'")
    _migrate(dsn)

    assert admin.execute(f"SELECT group_id FROM {name}.tests").fetchone()[0] == "g-new"


def test_migration_is_one_shot_and_a_later_null_row_cannot_re_arm_it(scratch):
    """THE defect this guard exists for. _SCHEMA_DDL re-runs on every
    RunRegistry() construction, and a generation failure writes a test_id NULL
    row at any time. Guarding on that column would re-collapse live data and
    silently enact the go-forward dedup the design declined."""
    name, dsn, admin = scratch
    _bare_schema(admin, name)
    _seed(admin, name, [
        {"run_id": "r1", "user_query": "q", "robot_code": "c",
         "created_at": "2026-01-01T10:00:00Z"},
    ])
    _migrate(dsn)
    assert admin.execute(f"SELECT count(*) FROM {name}.tests").fetchone()[0] == 1

    # A later generation failure: a run row with no test.
    admin.execute(
        f"INSERT INTO {name}.test_runs (run_id, user_id, user_query, status, org_id,"
        " created_at) VALUES ('later','u1','q','error','org-a', now())")
    _migrate(dsn)   # second construction, as a process restart would do

    # Still exactly one test. The new row was NOT collapsed into it.
    assert admin.execute(f"SELECT count(*) FROM {name}.tests").fetchone()[0] == 1
    assert admin.execute(
        f"SELECT test_id FROM {name}.test_runs WHERE run_id = 'later'").fetchone()[0] is None


def test_the_scale_guard_aborts_rather_than_merging_an_uninspected_database(scratch):
    name, dsn, admin = scratch
    _bare_schema(admin, name)
    admin.execute(
        f"INSERT INTO {name}.test_runs (run_id, user_id, user_query, robot_code,"
        " status, org_id, created_at) "
        "SELECT 'r'||i, 'u1', 'q'||i, 'c', 'passed', 'org-a', now() "
        f"FROM generate_series(1, {_MIGRATION_SCALE_LIMIT + 1}) i")
    _migrate(dsn)

    # Nothing collapsed; the database is left exactly as it was.
    assert admin.execute(f"SELECT count(*) FROM {name}.tests").fetchone()[0] == 0
    assert admin.execute(
        f"SELECT count(*) FROM {name}.test_runs WHERE test_id IS NULL"
    ).fetchone()[0] == _MIGRATION_SCALE_LIMIT + 1
