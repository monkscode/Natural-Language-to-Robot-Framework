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
    -- ON DELETE SET NULL, never CASCADE — see _ORG_OWNER_COLUMN_DDL below.
    owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
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

# owner_user_id lets a user RECLAIM their own personal org (ensure_personal_org
# steps 2-3 in org_repository.py) instead of minting a new empty one every time
# they cycle through a team and back out solo. org_db.py has no ALTER
# mechanism (CREATE TABLE IF NOT EXISTS only), so a new column needs its own
# idempotent statement alongside the one baked into _ORGANIZATIONS_DDL above
# (baseline DDL + idempotent migration, both — a fresh DB gets the column
# directly; an existing one gets it via this ALTER).
#
# ON DELETE SET NULL, and it must NEVER become CASCADE. CASCADE would delete
# the org row when its owner is deleted, reintroducing the exact "vacated
# personal org gets deleted, stranding its learning rows" bug T1 removed.
# SET NULL is also the honest statement: the owner really is gone, but the
# org and the learning rows naming it are not — do not "tidy" this into a
# cascade.
_ORG_OWNER_COLUMN_DDL = """
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS owner_user_id UUID
    REFERENCES users(id) ON DELETE SET NULL
"""

# Partial (kind='personal' only): a team org has no 1:1 owner and is
# deliberately left unconstrained. Two personal orgs both owned by NULL are
# allowed (the common case before a user is ever stamped/backfilled); two
# personal orgs owned by the SAME user are not.
_ORG_OWNER_INDEX_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_org_owner_personal
    ON organizations (owner_user_id) WHERE kind = 'personal'
"""


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
        conn.execute(_ORG_OWNER_COLUMN_DDL)
        conn.execute(_ORG_OWNER_INDEX_DDL)
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

    # Stamp owner_user_id on personal orgs that predate the column — a
    # one-time migration, gated by the same advisory lock. Two passes (sole
    # remaining member, then name==email); see
    # OrgRepository.backfill_personal_org_owners for what each pass claims
    # and the ambiguous cases it deliberately leaves NULL rather than
    # aborting.
    def _backfill_personal_org_owners() -> None:
        from src.backend.auth.org_repository import OrgRepository
        OrgRepository().backfill_personal_org_owners()

    if run_migration_once("personal_org_owner_backfill", _backfill_personal_org_owners):
        logger.info("[AUTH] personal-org owner backfill applied")
    else:
        logger.info("[AUTH] personal-org owner backfill already applied; skipping")
