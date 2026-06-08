"""
Auth database — PostgreSQL connection pool + users-table bootstrap.

Owns the ONLY Postgres connection in the app (the learning stack still uses
SQLite/ChromaDB; that migration is a separate phase). A lazily-opened psycopg
ConnectionPool is shared by the UserRepository. Importing this module never
touches the database, so the app and unit tests import cleanly even when
Postgres is down — only the first actual query opens the pool.

Referenced by: auth/repository.py, auth/endpoints.py, main.py (init_auth_db/close_pool).
Depends on: src/backend/core/config.py (DATABASE_URL), psycopg, psycopg_pool.
"""

import logging
import threading

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from src.backend.core.config import settings

logger = logging.getLogger(__name__)

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> ConnectionPool:
    """Return the shared connection pool, opening it on first use.

    Thread-safe lazy init: the pool is created once across FastAPI request
    threads and the CrewAI background thread. Every pooled connection uses the
    dict_row factory so rows read back as plain dicts.
    """
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            pool = ConnectionPool(
                conninfo=settings.DATABASE_URL,
                min_size=1,
                max_size=10,
                kwargs={"row_factory": dict_row},
                open=False,
            )
            pool.open()
            _pool = pool
            logger.info("[AUTH] Postgres connection pool opened")
    return _pool


_USERS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           TEXT UNIQUE NOT NULL,
    hashed_password TEXT,
    display_name    TEXT NOT NULL DEFAULT '',
    role            TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    auth_provider   TEXT NOT NULL DEFAULT 'password',
    google_sub      TEXT UNIQUE,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login      TIMESTAMPTZ
)
"""

_INDEXES_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_users_email ON users (email)",
    "CREATE INDEX IF NOT EXISTS idx_users_google_sub ON users (google_sub)",
)


def init_auth_db() -> None:
    """Create the users table + indexes if absent. Idempotent; run at startup.

    gen_random_uuid() is built into PostgreSQL 13+ (no pgcrypto extension needed
    on postgres:16). Raises if Postgres is unreachable — the caller (main.py)
    wraps this so an unavailable DB degrades gracefully instead of blocking boot.
    """
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(_USERS_TABLE_DDL)
        for ddl in _INDEXES_DDL:
            conn.execute(ddl)
        conn.commit()
    logger.info("[AUTH] users table ready")


def close_pool() -> None:
    """Close the pool on shutdown (best-effort)."""
    global _pool
    if _pool is not None:
        try:
            _pool.close()
        finally:
            _pool = None
            logger.info("[AUTH] Postgres connection pool closed")
