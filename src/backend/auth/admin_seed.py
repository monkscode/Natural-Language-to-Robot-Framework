"""Promote-only platform-admin bootstrap seed (Phase 1b).

ADMIN_EMAILS is a *seed*, not an authority: at startup any existing user whose
email is listed is promoted to platform-admin if not already. The DB (users.role)
is the source of truth thereafter — grant/revoke via UserRepository.set_platform_role.
This never demotes, so removing an email from the env list does not strip admin.

Referenced by: main.py (startup_event).
Depends on: auth/db.py (pool), core/config.py (admin_emails_list).
"""

import logging

from src.backend.auth.db import get_pool
from src.backend.core.config import settings

logger = logging.getLogger(__name__)


def seed_platform_admins() -> int:
    """Promote allow-listed users to platform-admin, and force a PENDING one active
    so the owner can never be stuck pending after a fresh deploy (parent spec
    §Bootstrap). A suspended/rejected allow-listed admin is left disabled — seeding
    must never resurrect an account an owner deliberately turned off; only the role
    is promoted, the status is preserved. Idempotent, promote-only on role. Returns
    the number of rows changed."""
    emails = [e.strip().lower() for e in settings.admin_emails_list if e.strip()]
    if not emails:
        return 0
    with get_pool().connection() as conn:
        # Activation is gated to pending rows only (bootstrap the owner); role is
        # promoted regardless of status. The WHERE fires when either half has work
        # to do, so the row count still reflects real changes and stays idempotent.
        cur = conn.execute(
            "UPDATE users SET role = 'admin', "
            "status = CASE WHEN status = 'pending' THEN 'active' ELSE status END, "
            "is_active = CASE WHEN status = 'pending' THEN TRUE ELSE is_active END "
            "WHERE lower(email) = ANY(%s) AND (role <> 'admin' OR status = 'pending')",
            (emails,),
        )
        n = cur.rowcount
        conn.commit()
    if n:
        logger.info("[AUTH] seeded %d platform-admin(s) from ADMIN_EMAILS", n)
    return n
