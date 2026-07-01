# tests/test_auth/test_signup_invite_match_integration.py
import uuid
import pytest
from src.backend.auth.repository import UserRepository
from src.backend.auth.org_repository import OrgRepository
from src.backend.auth.invitation_repository import InvitationRepository
from src.backend.auth.provisioning import match_invite_on_signup, provision_on_approval
from src.backend.auth.db import get_pool
from src.backend.auth.invitations_db import init_invitations_db

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


def test_invited_signup_is_tagged_to_org_on_approval():
    init_invitations_db()
    users, orgs, invites = UserRepository(), OrgRepository(), InvitationRepository()
    owner_email = f"o-{uuid.uuid4().hex[:8]}@x.com"
    invitee_email = f"i-{uuid.uuid4().hex[:8]}@x.com"
    try:
        owner = users.create_user(owner_email, "password123", "Owner")
        org_id = orgs.create_team_org("Acme", str(owner["id"]))
        invites.create(invitee_email, org_id, str(owner["id"]))
        invitee = users.create_user(invitee_email, "password123", "Inv")
        match_invite_on_signup(str(invitee["id"]), invitee_email)   # signup hook
        provision_on_approval(str(invitee["id"]))                   # approval hook
        member_orgs = {o["org_id"] for o in orgs.get_orgs_for_user(str(invitee["id"]))}
        assert member_orgs == {org_id}
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([owner_email, invitee_email],))
            conn.commit()


def test_cold_signup_gets_personal_org_on_approval():
    init_invitations_db()
    users, orgs = UserRepository(), OrgRepository()
    email = f"c-{uuid.uuid4().hex[:8]}@x.com"
    try:
        u = users.create_user(email, "password123", "Cold")
        match_invite_on_signup(str(u["id"]), email)  # no invite -> no-op
        provision_on_approval(str(u["id"]))
        orgs_for = orgs.get_orgs_for_user(str(u["id"]))
        assert len(orgs_for) == 1 and orgs_for[0]["kind"] == "personal"
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (email,))
            conn.commit()
