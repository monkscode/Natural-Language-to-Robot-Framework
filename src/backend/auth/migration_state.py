"""One-shot markers for data migrations that must run exactly once.

The pre-tenancy backfills — personal-org provisioning (Phase 1a) and data
org_id attribution (Phase 1b/1c) — are one-time migrations, not per-boot work.
They were re-running on every startup, re-scanning every table each boot. The
org_id backfill was also actively harmful on repeat: re-running it after a hint
was promoted cross-org would re-attribute the promoted hint's anchor and
silently un-share it (the cross-org similarity filter keys on org_id IS NULL).

Recording completion in `data_migrations` makes each backfill run once and skip
thereafter — the same intent as schema_version/trace_schema_version, but for
data migrations rather than DDL. Shares the auth/users pool (get_pool): these
markers live in the identity DB alongside the org tables the backfills populate.

Referenced by: main.py (data org_id backfill), auth/org_db.py (personal orgs).
Depends on: auth/db.py (pool).
"""
import logging

from src.backend.auth.db import get_pool

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS data_migrations (
    name         TEXT PRIMARY KEY,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def is_migration_done(name: str) -> bool:
    """True if the named one-shot data migration has already completed.

    Creates the marker table on first use (idempotent). Raises if Postgres is
    unreachable — callers run at startup and already guard for that, treating a
    failure as "skip this boot, retry next".
    """
    with get_pool().connection() as conn:
        conn.execute(_DDL)
        row = conn.execute(
            "SELECT 1 FROM data_migrations WHERE name = %s", (name,)
        ).fetchone()
        conn.commit()
        return row is not None


def mark_migration_done(name: str) -> None:
    """Record the named one-shot data migration as completed. Idempotent."""
    with get_pool().connection() as conn:
        conn.execute(_DDL)
        conn.execute(
            "INSERT INTO data_migrations (name) VALUES (%s) ON CONFLICT DO NOTHING",
            (name,),
        )
        conn.commit()
