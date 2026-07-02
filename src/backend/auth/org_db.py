"""Org-tenancy schema bootstrap: organizations + org_members.

Phase 1a of the architecture-evolution spec (docs/superpowers/specs/
2026-06-18-architecture-evolution-design.md). Seeds multi-tenancy structurally
without changing any user-visible behaviour: every user owns a personal org and
is its org_admin. org-scoped reads arrive in a later sub-plan.

Shares the auth/users Postgres pool — org tenancy is part of the identity
domain. Importing this module never touches the database; init_org_db() runs at
startup, after init_auth_db() (org_members FKs reference users).

Referenced by: main.py (init_org_db), auth/org_repository.py, tests/test_auth.
Depends on: auth/db.py (pool), src/backend/core/config.py (DATABASE_URL).
"""

import logging

from src.backend.auth.db import get_pool

logger = logging.getLogger(__name__)

_ORGANIZATIONS_DDL = """
CREATE TABLE IF NOT EXISTS organizations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'personal' CHECK (kind IN ('personal', 'team')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_ORG_MEMBERS_DDL = """
CREATE TABLE IF NOT EXISTS org_members (
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_role    TEXT NOT NULL DEFAULT 'org_member'
                CHECK (org_role IN ('org_admin', 'org_member')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, user_id)
)
"""

_INDEXES_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_org_members_user ON org_members (user_id)",
)


def init_org_db() -> None:
    """Create the organizations + org_members tables and index if absent.

    Idempotent; run at startup AFTER init_auth_db() (org_members references
    users). Raises if Postgres is unreachable — the caller (main.py) wraps this
    so an unavailable DB degrades gracefully instead of blocking boot.
    """
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(_ORGANIZATIONS_DDL)
        conn.execute(_ORG_MEMBERS_DDL)
        for ddl in _INDEXES_DDL:
            conn.execute(ddl)
        conn.commit()
    logger.info("[AUTH] organizations + org_members tables ready")

    # Provision personal orgs for pre-tenancy users — a one-time migration, gated
    # so it does not re-scan users on every boot. All user-creation paths
    # (register, Google callback) provision an org inline, so no org-less user
    # appears after this runs. run_migration_once holds an advisory lock so
    # concurrent boots can't both run it. Best-effort: an unset marker retries.
    from src.backend.auth.migration_state import run_migration_once

    def _provision_personal_orgs() -> None:
        from src.backend.auth.org_repository import OrgRepository
        OrgRepository().backfill_personal_orgs()

    if run_migration_once("personal_org_backfill", _provision_personal_orgs):
        logger.info("[AUTH] personal-org backfill applied")
    else:
        logger.info("[AUTH] personal-org backfill already applied; skipping")

    # Collapse any pre-existing multi-org memberships to a single active org — a
    # one-time migration gated by the same advisory lock so concurrent boots can't
    # double-run it and it doesn't re-scan on every boot. Runs after the personal-org
    # backfill so no org-less user is left behind. Note: run_migration_once holds an
    # advisory-lock connection open across this call, and collapse_all_to_single_org
    # borrows its own pooled connections underneath it — so the migration nests pooled
    # borrows. Safe because the depth is small and sequential (lock conn + one
    # membership conn + one bump conn) and stays well under the pool ceiling.
    def _collapse_single_org() -> None:
        from src.backend.auth.org_repository import OrgRepository
        OrgRepository().collapse_all_to_single_org()

    if run_migration_once("collapse_to_single_org", _collapse_single_org):
        logger.info("[AUTH] single-active-org collapse applied")
    else:
        logger.info("[AUTH] single-active-org collapse already applied; skipping")
