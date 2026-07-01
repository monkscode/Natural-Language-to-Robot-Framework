"""Signup-time invite matching and approval-time org provisioning.

Two hooks keep org membership honest: a pending user gets NO membership, so a
rejected user leaves no litter. At signup we only RECORD which org invited them
(consume the invite). At approval we materialise membership: the invited org for
an invitee, or a fresh personal org for a cold signup.

Referenced by: auth/endpoints.py (signup), auth/admin_access_endpoints.py (approve).
Depends on: auth/invitation_repository.py, auth/org_repository.py.
"""
import logging

from src.backend.auth.invitation_repository import InvitationRepository
from src.backend.auth.org_repository import OrgRepository

logger = logging.getLogger(__name__)

_invites = InvitationRepository()
_orgs = OrgRepository()


def match_invite_on_signup(user_id: str, email: str) -> None:
    """If an open invite matches this email, consume it (records consumed_by).
    No membership is created yet — that happens at approval."""
    inv = _invites.find_open_by_email(email)
    if inv is not None:
        _invites.consume(str(inv["id"]), user_id)
        logger.info("[AUTH] signup matched invite to org %s", inv["org_id"])


def provision_on_approval(user_id: str) -> None:
    """Materialise org membership when the owner approves. Invitee -> member of
    the team org that invited them; cold signup -> their own personal org."""
    inv = _invites.find_consumed_by_user(user_id)
    if inv is not None:
        _orgs.add_member(str(inv["org_id"]), user_id, "org_member")
        logger.info("[AUTH] approved user %s into org %s", user_id, inv["org_id"])
    else:
        # ensure_personal_org needs a name; the user's email is the personal-org
        # name elsewhere in the codebase (register path). Read it here.
        from src.backend.auth.repository import UserRepository
        row = UserRepository().get_by_id(user_id)
        _orgs.ensure_personal_org(user_id, row["email"] if row else user_id)
