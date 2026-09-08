"""The one-shot collapse of test_runs onto tests + test_versions.

Every assertion here mirrors a measured fact about the live database
(46 runs -> 21 tests, 45 versions) or a rule from spec section 4.
"""
import threading
import uuid

import psycopg
import pytest

from src.backend.core.config import settings
from src.backend.core.run_registry import _MIGRATION_SCALE_LIMIT

pytestmark = pytest.mark.integration

OWNER = {"user_id": "u1", "org_id": "org-a", "email": "u1@x.com"}


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
    """NULL never equals NULL, so two pasted runs stay two tests.

    A row-count assertion alone is blind to a swallow: if the only_run
    guard were ever deleted from the NULL branch, p1 and p2 would BOTH
    attach to the first test the loop builds and the second INSERT would
    still fire (its inner loop just finds nothing to attach), leaving 2
    rows in `tests` but only one of them actually used. Pin the real
    attachment, not just the shell count.
    """
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
    p1_test = admin.execute(
        f"SELECT test_id FROM {name}.test_runs WHERE run_id = 'p1'").fetchone()[0]
    p2_test = admin.execute(
        f"SELECT test_id FROM {name}.test_runs WHERE run_id = 'p2'").fetchone()[0]
    assert p1_test is not None and p2_test is not None
    assert p1_test != p2_test
    assert admin.execute(
        f"SELECT count(DISTINCT test_id) FROM {name}.test_runs").fetchone()[0] == 2


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


def test_org_less_runs_get_sequential_keys_in_their_own_bucket(scratch):
    """The bench and every AUTH_ENFORCED=false run write org_id NULL. Key
    allocation depends on `org_id IS NOT DISTINCT FROM g.org_id` rather than
    `org_id = g.org_id`: NULL = NULL is unknown, so `=` would make every
    org-less group compute key_n 1, collide on idx_tests_org_key (NULLS NOT
    DISTINCT), abort the DO block, and make RunRegistry.__init__ raise --
    the app stops starting, with the rest of the suite still green."""
    name, dsn, admin = scratch
    _bare_schema(admin, name)
    _seed(admin, name, [
        {"run_id": "d1", "org_id": None, "user_query": "q1",
         "robot_code": "c", "created_at": "2026-01-01T10:00:00Z"},
        {"run_id": "d2", "org_id": None, "user_query": "q2",
         "robot_code": "c", "created_at": "2026-01-02T10:00:00Z"},
    ])
    _migrate(dsn)

    assert admin.execute(f"SELECT count(*) FROM {name}.tests").fetchone()[0] == 2
    assert sorted(r[0] for r in admin.execute(
        f"SELECT key_n FROM {name}.tests WHERE org_id IS NULL").fetchall()) == [1, 2]


def test_the_oldest_group_gets_key_n_one(scratch):
    """key_n has to be assigned in a fixed order or it is arbitrary --
    HashAggregate order today, something else after the next planner
    upgrade -- and it is user-visible (rendered TC-<key_n> in P2), so
    fixing it after the fact means renumbering live keys. Queries are
    seeded so alphabetical order is the OPPOSITE of chronological order:
    if key_n ever tracked query text instead of created_at, this would
    catch that too."""
    name, dsn, admin = scratch
    _bare_schema(admin, name)
    _seed(admin, name, [
        {"run_id": "r_z", "user_query": "zzz-query", "robot_code": "c",
         "created_at": "2026-01-01T10:00:00Z"},
        {"run_id": "r_m", "user_query": "mmm-query", "robot_code": "c",
         "created_at": "2026-01-02T10:00:00Z"},
        {"run_id": "r_a", "user_query": "aaa-query", "robot_code": "c",
         "created_at": "2026-01-03T10:00:00Z"},
    ])
    _migrate(dsn)

    def _key_for(query):
        return admin.execute(
            f"SELECT key_n FROM {name}.tests WHERE user_query = %s",
            (query,)).fetchone()[0]

    assert _key_for("zzz-query") == 1
    assert _key_for("mmm-query") == 2
    assert _key_for("aaa-query") == 3


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


def test_a_fresh_install_does_not_collapse_a_run_that_has_no_code_yet(scratch):
    """The guard is armed until the FIRST test exists, not "once in a
    database's life" — and on a fresh install that window stays open across
    every process start until a generation SUCCEEDS.

    record_start opens each run's row at generation START with robot_code
    NULL (workflow_service.py:1067, :1086) and _SCHEMA_DDL re-runs on EVERY
    RunRegistry() construction, so a second process booting inside that
    window used to collapse a code-less run into a test — exactly what
    _attach_test is written never to do under D8.

    The stray row is not the damage. _attach_test short-circuits on an
    existing test_id, so when the generation then succeeds the code never
    becomes a version: the test carries current_version 1 against zero
    test_versions and the run's test_version_id stays NULL forever, with
    robot_code sitting on the run where every re-run resolves version_id
    None.
    """
    name, dsn, admin = scratch
    from src.backend.core.run_registry import RunRegistry

    reg = RunRegistry(dsn=dsn)          # fresh install: creates the schema
    try:
        # Generation STARTS. No code exists yet, so D8 leaves test_id NULL.
        reg.record_start("w1", OWNER, "search shoes", "running")
        assert admin.execute(
            f"SELECT test_id FROM {name}.test_runs WHERE run_id = 'w1'"
        ).fetchone()[0] is None, "premise: the run starts with no test"

        # A second process boots inside that window and re-runs _SCHEMA_DDL.
        RunRegistry(dsn=dsn).close()
        assert admin.execute(
            f"SELECT count(*) FROM {name}.tests").fetchone()[0] == 0, (
            "the collapse minted a phantom test from a run that has no code")

        # Generation SUCCEEDS: same run id, now carrying the code.
        reg.record_start("w1", OWNER, "search shoes", "generated",
                         robot_code="code-1")
    finally:
        reg.close()

    assert admin.execute(
        f"SELECT count(*) FROM {name}.tests").fetchone()[0] == 1
    version_id = admin.execute(
        f"SELECT test_version_id FROM {name}.test_runs WHERE run_id = 'w1'"
    ).fetchone()[0]
    assert version_id is not None, (
        "the generated code never became a version — the run points at no "
        "version at all")
    assert admin.execute(
        f"SELECT robot_code FROM {name}.test_versions WHERE version_id = %s",
        (version_id,)).fetchone()[0] == "code-1"
    cv, n_versions = admin.execute(
        f"SELECT t.current_version, (SELECT count(*) FROM {name}.test_versions"
        f" v WHERE v.test_id = t.test_id) FROM {name}.tests t").fetchone()
    assert (cv, n_versions) == (1, 1), (
        "current_version must name a version that exists")


def test_a_group_whose_runs_all_lack_code_is_skipped_not_minted(scratch):
    """The same rule on pre-split data: a group with nothing to version
    produces no test, and its runs keep test_id NULL — a permanently legal
    state under D8. Measured read-only against the owner's database before
    the change: 0 such groups, so the migration's 21/45/46 do not move.
    """
    name, dsn, admin = scratch
    _bare_schema(admin, name)
    _seed(admin, name, [
        {"run_id": "dead1", "user_query": "never generated", "robot_code": None,
         "status": "error", "created_at": "2026-01-01T10:00:00Z"},
        {"run_id": "dead2", "user_query": "never generated", "robot_code": None,
         "status": "error", "created_at": "2026-01-02T10:00:00Z"},
        {"run_id": "live1", "user_query": "real one", "robot_code": "c",
         "created_at": "2026-01-03T10:00:00Z"},
    ])
    _migrate(dsn)

    assert [r[0] for r in admin.execute(
        f"SELECT user_query FROM {name}.tests").fetchall()] == ["real one"]
    assert sorted(r[0] for r in admin.execute(
        f"SELECT run_id FROM {name}.test_runs WHERE test_id IS NULL").fetchall()
    ) == ["dead1", "dead2"]


def test_a_code_less_run_still_attaches_when_a_sibling_has_code(scratch):
    """The other half of D8, and the shape the owner's database actually
    holds (books.toscrape: 3 runs, 2 with code). Skipping a group must key
    on the GROUP having no code, never on the individual run — the run with
    no code still attaches to the test its siblings built.
    """
    name, dsn, admin = scratch
    _bare_schema(admin, name)
    _seed(admin, name, [
        {"run_id": "ok1", "user_query": "q", "robot_code": "c1",
         "created_at": "2026-01-01T10:00:00Z"},
        {"run_id": "bad", "user_query": "q", "robot_code": None,
         "status": "error", "created_at": "2026-01-02T10:00:00Z"},
        {"run_id": "ok2", "user_query": "q", "robot_code": "c2",
         "created_at": "2026-01-03T10:00:00Z"},
    ])
    _migrate(dsn)

    assert admin.execute(
        f"SELECT count(*) FROM {name}.test_runs WHERE test_id IS NULL"
    ).fetchone()[0] == 0
    assert admin.execute(
        f"SELECT count(DISTINCT test_id) FROM {name}.test_runs").fetchone()[0] == 1
    assert admin.execute(
        f"SELECT test_version_id FROM {name}.test_runs WHERE run_id = 'bad'"
    ).fetchone()[0] is None


def test_a_null_query_run_with_no_code_mints_no_test(scratch):
    """The NULL-query branch keys one test per run, so a code-less pasted
    run has no sibling that could ever supply a version. It must be skipped
    on that branch too, or it mints a test that can never hold one.
    """
    name, dsn, admin = scratch
    _bare_schema(admin, name)
    _seed(admin, name, [
        {"run_id": "p1", "user_query": None, "robot_code": "c1",
         "created_at": "2026-01-01T10:00:00Z"},
        {"run_id": "p2", "user_query": None, "robot_code": None,
         "status": "error", "created_at": "2026-01-02T10:00:00Z"},
    ])
    _migrate(dsn)

    assert admin.execute(f"SELECT count(*) FROM {name}.tests").fetchone()[0] == 1
    assert admin.execute(
        f"SELECT test_id FROM {name}.test_runs WHERE run_id = 'p2'"
    ).fetchone()[0] is None


# Enough rounds that the pre-fix race is caught with near-certainty rather
# than 4 times in 10. The owner measured the unguarded collapse raising
# UniqueViolation on idx_tests_org_key in 4 of 10 two-thread rounds; at that
# rate 8 rounds miss it once in ~60 runs. After the advisory lock the outcome
# is deterministic, so the only cost of the extra rounds is wall time.
_RACE_ROUNDS = 8


def _reset_collapse(admin, name):
    """Return the schema to its pre-collapse state so the next round re-arms.

    test_runs.test_id is cleared FIRST: fk_test_runs_test is ON DELETE
    CASCADE, so deleting `tests` while runs still point at it would take the
    seeded runs with it and every later round would collapse nothing."""
    admin.execute(
        f"UPDATE {name}.test_runs SET test_id = NULL, test_version_id = NULL")
    admin.execute(f"DELETE FROM {name}.test_versions")
    admin.execute(f"DELETE FROM {name}.tests")


def test_two_concurrent_constructions_collapse_once_without_raising(scratch):
    """THE race the entry guard cannot win on its own.

    `IF EXISTS (SELECT 1 FROM tests) THEN RETURN` is a check-then-act, not a
    lock: two RunRegistry() constructions against the same database both pass
    it, both walk the same groups, and the loser's INSERT collides on
    idx_tests_org_key. The DO block is atomic so the DATA survives, but the
    losing construction raises out of __init__ — in production a bare 500 on
    whichever request got there first, and on a fresh deploy a failed boot.

    Two threads released by a barrier, over a schema already provisioned so
    the DDL statements ahead of the collapse are no-ops and both threads
    arrive at the DO block together."""
    name, dsn, admin = scratch
    _migrate(dsn)   # provision: test_runs is empty, so the collapse no-ops
    assert admin.execute(f"SELECT count(*) FROM {name}.tests").fetchone()[0] == 0

    # ~20 groups x 2 runs is the shape that reproduced it: enough groups that
    # the loop stays inside the DO block long enough for the second thread to
    # pass the guard.
    _seed(admin, name, [
        {"run_id": f"r{g:02d}-{i}", "user_query": f"query {g}",
         "robot_code": f"code-{g}-{i}",
         "created_at": f"2026-01-{g + 1:02d}T1{i}:00:00Z"}
        for g in range(20) for i in range(2)
    ])

    for round_n in range(_RACE_ROUNDS):
        barrier = threading.Barrier(2)
        failures: list[BaseException] = []

        def _construct():
            try:
                barrier.wait()
                _migrate(dsn)
            except BaseException as exc:      # noqa: BLE001 - reported below
                failures.append(exc)

        threads = [threading.Thread(target=_construct) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert not any(t.is_alive() for t in threads), (
            f"round {round_n}: a construction never finished — the advisory "
            f"lock is held by something that did not release it")
        assert failures == [], (
            f"round {round_n}: a concurrent RunRegistry() construction "
            f"raised {failures[0]!r}")

        # Exactly what ONE construction produces, no more and no less.
        assert admin.execute(
            f"SELECT count(*) FROM {name}.tests").fetchone()[0] == 20, (
            f"round {round_n}: the collapse ran twice or half-ran")
        assert admin.execute(
            f"SELECT count(*) FROM {name}.test_versions").fetchone()[0] == 40
        assert admin.execute(
            f"SELECT count(*) FROM {name}.test_runs WHERE test_id IS NULL"
        ).fetchone()[0] == 0
        assert admin.execute(
            f"SELECT count(*) FROM {name}.tests t WHERE NOT EXISTS ("
            f"SELECT 1 FROM {name}.test_versions v WHERE v.test_id = t.test_id)"
        ).fetchone()[0] == 0, (
            f"round {round_n}: a phantom test with current_version 1 and no "
            f"version at all")
        assert admin.execute(
            f"SELECT count(*) FROM {name}.tests t WHERE NOT EXISTS ("
            f"SELECT 1 FROM {name}.test_runs r WHERE r.test_id = t.test_id)"
        ).fetchone()[0] == 0, (
            f"round {round_n}: a test nothing points at")

        _reset_collapse(admin, name)


def test_the_collapse_never_leaves_a_test_without_a_version(scratch):
    """current_version is set to greatest(v_n, 1), so a test whose inner loop
    attached nothing still claims version 1 while `test_versions` holds none —
    an unrenderable row that no later code path can repair. The outer grouping
    query is what has to make that impossible, by selecting only runs that are
    still unattached."""
    name, dsn, admin = scratch
    _bare_schema(admin, name)
    _seed(admin, name, [
        {"run_id": "a1", "user_query": "q1", "robot_code": "c1",
         "created_at": "2026-01-01T10:00:00Z"},
        {"run_id": "a2", "user_query": "q1", "robot_code": "c2",
         "created_at": "2026-01-02T10:00:00Z"},
        {"run_id": "a3", "user_query": "q1", "robot_code": None,
         "status": "error", "created_at": "2026-01-03T10:00:00Z"},
        {"run_id": "b1", "user_query": None, "robot_code": "c3",
         "created_at": "2026-01-04T10:00:00Z"},
        {"run_id": "c1", "org_id": None, "user_query": "q2",
         "robot_code": "c4", "created_at": "2026-01-05T10:00:00Z"},
    ])
    _migrate(dsn)

    assert admin.execute(
        f"SELECT count(*) FROM {name}.tests t WHERE NOT EXISTS ("
        f"SELECT 1 FROM {name}.test_versions v WHERE v.test_id = t.test_id)"
    ).fetchone()[0] == 0
    # current_version must name a version that exists, for every test.
    assert admin.execute(
        f"SELECT count(*) FROM {name}.tests t WHERE NOT EXISTS ("
        f"SELECT 1 FROM {name}.test_versions v WHERE v.test_id = t.test_id"
        f" AND v.n = t.current_version)").fetchone()[0] == 0
