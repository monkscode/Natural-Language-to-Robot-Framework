# src/backend/auth/invitations_db.py
"""Invitations schema bootstrap: org-owner invites matched at signup by email.

An invitation is a pre-authorization record. When the invited email self-
registers, the signup flow matches the open invite and tags the new user to the
inviter's org; the user still requires owner approval. Importing this module
never touches the DB; init_invitations_db() runs at startup AFTER init_org_db()
(FKs reference organizations + users).

Referenced by: main.py (init_invitations_db), auth/invitation_repository.py.
Depends on: auth/db.py (pool).
"""
import logging
from src.backend.auth.db import get_pool

logger = logging.getLogger(__name__)

_INVITATIONS_DDL = """
CREATE TABLE IF NOT EXISTS invitations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email       TEXT NOT NULL,
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    invited_by  UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'consumed', 'revoked', 'expired')),
    token       TEXT UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ,
    consumed_by UUID REFERENCES users(id) ON DELETE SET NULL,
    consumed_at TIMESTAMPTZ
)
"""

_INDEXES_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_open_invite "
    "ON invitations (lower(email), org_id) WHERE status = 'open'",
    "CREATE INDEX IF NOT EXISTS idx_invitations_email_open "
    "ON invitations (lower(email)) WHERE status = 'open'",
)


def init_invitations_db() -> None:
    """Create the invitations table + indexes if absent. Idempotent; run at
    startup AFTER init_org_db()."""
    with get_pool().connection() as conn:
        conn.execute(_INVITATIONS_DDL)
        for ddl in _INDEXES_DDL:
            conn.execute(ddl)
        conn.commit()
    logger.info("[AUTH] invitations table ready")
