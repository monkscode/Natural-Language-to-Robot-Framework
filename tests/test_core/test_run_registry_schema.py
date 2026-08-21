"""_SCHEMA_DDL upgrade guards.

The tuple runs on EVERY RunRegistry() construction, so anything in it must be
idempotent and must never destroy data. These tests build a scratch schema by
hand, put it in a state a real database could be in, and construct the registry
against it.
"""
import uuid

import psycopg
import pytest

from src.backend.core.config import settings

pytestmark = pytest.mark.integration

_OLD_SHAPE = """
    CREATE TABLE run_groups (
        group_id   TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        user_id    TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
"""


@pytest.fixture
def scratch():
    """An empty schema plus a DSN whose search_path points at it alone."""
    name = f"schema_guard_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    admin.execute(f"CREATE SCHEMA {name}")
    sep = "&" if "?" in settings.DATABASE_URL else "?"
    dsn = settings.DATABASE_URL + f"{sep}options=-c%20search_path%3D{name}"
    try:
        yield name, dsn, admin
    finally:
        admin.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
        admin.close()


def _columns(admin, schema):
    return sorted(r[0] for r in admin.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'run_groups'", (schema,)
    ).fetchall())


def test_empty_pre_release_table_is_replaced(scratch):
    """A database that ran PR #94's branch keeps the per-user table. CREATE
    TABLE IF NOT EXISTS is a no-op there, so the partial indexes fail with
    UndefinedColumn and the whole registry becomes unconstructable."""
    from src.backend.core.run_registry import RunRegistry
    schema, dsn, admin = scratch
    admin.execute(f"SET search_path TO {schema}")
    admin.execute(_OLD_SHAPE)
    assert "user_id" in _columns(admin, schema)

    reg = RunRegistry(dsn=dsn)
    try:
        cols = _columns(admin, schema)
        assert "org_id" in cols and "visibility" in cols and "created_by" in cols
        assert "user_id" not in cols
    finally:
        reg.close()


def test_a_non_empty_pre_release_table_is_refused_not_dropped(scratch):
    """The guard is 'old shape AND empty'. A row means someone's folders are
    in there and a human has to decide — never a silent delete."""
    from src.backend.core.run_registry import RunRegistry
    schema, dsn, admin = scratch
    admin.execute(f"SET search_path TO {schema}")
    admin.execute(_OLD_SHAPE)
    admin.execute(
        "INSERT INTO run_groups (group_id, name, user_id) VALUES (%s, %s, %s)",
        (str(uuid.uuid4()), "Precious", "u1"))

    with pytest.raises(Exception) as exc:
        RunRegistry(dsn=dsn).close()
    assert "pre-release" in str(exc.value)
    assert admin.execute("SELECT count(*) FROM run_groups").fetchone()[0] == 1


def test_dropping_the_pre_release_table_logs_a_warning(scratch, caplog):
    """The DROP is the guarded block's only branch that changes the
    database. Without a registered notice handler, psycopg discards its
    RAISE NOTICE and the drop leaves no trace anywhere."""
    from src.backend.core.run_registry import RunRegistry
    schema, dsn, admin = scratch
    admin.execute(f"SET search_path TO {schema}")
    admin.execute(_OLD_SHAPE)

    with caplog.at_level("WARNING", logger="src.backend.core.run_registry"):
        reg = RunRegistry(dsn=dsn)
    try:
        warnings = [r.getMessage() for r in caplog.records
                    if r.levelname == "WARNING"]
        assert any("run_groups" in m for m in warnings), warnings
    finally:
        reg.close()


def test_current_shape_is_left_alone_and_construction_is_idempotent(scratch):
    """The common case: nothing to upgrade, twice in a row, no raise."""
    from src.backend.core.run_registry import RunRegistry
    schema, dsn, admin = scratch
    r1 = RunRegistry(dsn=dsn)
    before = _columns(admin, schema)
    r1.close()
    r2 = RunRegistry(dsn=dsn)
    try:
        assert _columns(admin, schema) == before
    finally:
        r2.close()


def test_a_dangling_group_id_is_repaired_before_the_constraint_lands(scratch):
    """A database carrying a stale test_runs.group_id could not take the
    foreign key: ADD CONSTRAINT raised, RunRegistry.__init__ raised,
    main.py's startup guard logged a WARNING, and every later history,
    groups and report request 500'd with nothing naming the cause.

    The repair sits INSIDE the 'constraint does not exist yet' branch, so it
    runs at most once per schema — after that the constraint makes dangling
    rows impossible. Nothing is lost: such a run already reads as Ungrouped
    through the visibility join, because its folder is gone.
    """
    from src.backend.core.run_registry import RunRegistry
    schema, dsn, admin = scratch
    rid = str(uuid.uuid4())

    reg = RunRegistry(dsn=dsn)          # builds everything, including the FK
    reg.record_start(rid, {"user_id": "u1", "org_id": "org-a",
                           "email": "u1@e.com"}, "q", "passed")
    reg.close()

    admin.execute(f"SET search_path TO {schema}")
    admin.execute("ALTER TABLE test_runs DROP CONSTRAINT fk_test_runs_group")
    admin.execute("UPDATE test_runs SET group_id = %s WHERE run_id = %s",
                  (str(uuid.uuid4()), rid))
    assert admin.execute(
        "SELECT count(*) FROM test_runs WHERE group_id IS NOT NULL"
    ).fetchone()[0] == 1, "the dangling row was never seeded — the test cannot bite"

    reg2 = RunRegistry(dsn=dsn)         # must repair, not raise
    try:
        assert admin.execute(
            "SELECT count(*) FROM test_runs WHERE run_id = %s", (rid,)
        ).fetchone()[0] == 1, "the run row must survive the repair"
        assert admin.execute(
            "SELECT group_id FROM test_runs WHERE run_id = %s", (rid,)
        ).fetchone()[0] is None
        assert admin.execute(
            "SELECT count(*) FROM pg_constraint WHERE conname = 'fk_test_runs_group'"
            " AND conrelid = 'test_runs'::regclass"
        ).fetchone()[0] == 1
    finally:
        reg2.close()


def test_the_repair_logs_a_warning_naming_the_row_count(scratch, caplog):
    """The repair is a data mutation, permitted inside _SCHEMA_DDL only
    because RAISE NOTICE keeps it from being silent (owner decision 6). If
    the RAISE NOTICE line were deleted, the repair would still run — this
    test is the only thing that would catch that."""
    from src.backend.core.run_registry import RunRegistry
    schema, dsn, admin = scratch
    rid = str(uuid.uuid4())

    reg = RunRegistry(dsn=dsn)
    reg.record_start(rid, {"user_id": "u1", "org_id": "org-a",
                           "email": "u1@e.com"}, "q", "passed")
    reg.close()

    admin.execute(f"SET search_path TO {schema}")
    admin.execute("ALTER TABLE test_runs DROP CONSTRAINT fk_test_runs_group")
    admin.execute("UPDATE test_runs SET group_id = %s WHERE run_id = %s",
                  (str(uuid.uuid4()), rid))

    with caplog.at_level("WARNING", logger="src.backend.core.run_registry"):
        reg2 = RunRegistry(dsn=dsn)
    try:
        warnings = [r.getMessage() for r in caplog.records
                    if r.levelname == "WARNING"]
        assert any("test_runs" in m and "1" in m for m in warnings), warnings
    finally:
        reg2.close()
