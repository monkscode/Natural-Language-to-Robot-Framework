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
    """Promote allow-listed users to platform-admin. Idempotent, promote-only.
    Returns the number of users newly promoted."""
    emails = [e.strip().lower() for e in settings.admin_emails_list if e.strip()]
    if not emails:
        return 0
    with get_pool().connection() as conn:
        cur = conn.execute(
            "UPDATE users SET role = 'admin' "
            "WHERE lower(email) = ANY(%s) AND role <> 'admin'",
            (emails,),
        )
        n = cur.rowcount
        conn.commit()
    if n:
        logger.info("[AUTH] seeded %d platform-admin(s) from ADMIN_EMAILS", n)
    return n
