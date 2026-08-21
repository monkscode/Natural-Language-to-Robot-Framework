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
