"""Integration: users.status column exists, is constrained, and backfills."""
import pytest
from src.backend.auth.db import get_pool, init_auth_db

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


def test_status_column_exists_with_constraint_and_backfill():
    init_auth_db()
    with get_pool().connection() as conn:
        col = conn.execute(
            "SELECT data_type, column_default FROM information_schema.columns "
            "WHERE table_name='users' AND column_name='status' "
            "AND table_schema = current_schema()"
        ).fetchone()
        assert col is not None
        assert col["column_default"] is not None and "active" in col["column_default"]
        # Assert the CHECK constraint itself, not just the default — a broken or
        # missing constraint would let arbitrary status values through unnoticed.
        con = conn.execute(
            "SELECT pg_get_constraintdef(c.oid) AS def "
            "FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE t.relname = 'users' AND c.contype = 'c' "
            "AND n.nspname = current_schema() "
            "AND pg_get_constraintdef(c.oid) LIKE '%status%'"
        ).fetchall()
        assert any(
            all(s in r["def"] for s in ("'pending'", "'active'", "'suspended'", "'rejected'"))
            for r in con
        ), f"status CHECK constraint missing or wrong: {[r['def'] for r in con]}"
        # A row inserted inactive must read back as suspended after re-running init.
        conn.execute(
            "INSERT INTO users (email, hashed_password, is_active, status) "
            "VALUES ('backfill-probe@x.com', 'x', FALSE, 'active')"
        )
        conn.commit()
    init_auth_db()  # idempotent; runs the backfill UPDATE
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status FROM users WHERE email='backfill-probe@x.com'"
        ).fetchone()
        assert row["status"] == "suspended"
        conn.execute("DELETE FROM users WHERE email='backfill-probe@x.com'")
        conn.commit()
