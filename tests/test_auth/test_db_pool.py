"""Unit tests for the auth connection-pool lifecycle (no live database).

get_pool/close_pool manage the module-global pool; the pool class is mocked so
these run (and count for coverage) without Postgres.
"""

from unittest.mock import MagicMock, patch

from src.backend.auth import db as auth_db


def test_get_pool_creates_once_and_close_resets():
    saved = auth_db._pool
    auth_db._pool = None
    try:
        fake_pool = MagicMock()
        with patch.object(auth_db, "ConnectionPool", return_value=fake_pool) as cls:
            assert auth_db.get_pool() is fake_pool
            assert auth_db.get_pool() is fake_pool  # cached — no second construction
            assert cls.call_count == 1
            fake_pool.open.assert_called_once()
        auth_db.close_pool()
        fake_pool.close.assert_called_once()
        assert auth_db._pool is None
        auth_db.close_pool()  # idempotent when nothing is open
    finally:
        auth_db._pool = saved
