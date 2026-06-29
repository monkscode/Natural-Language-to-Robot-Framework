# tests/test_auth/test_invitation_repository_integration.py
import uuid
import pytest
from src.backend.auth.repository import UserRepository
from src.backend.auth.org_repository import OrgRepository
from src.backend.auth.invitation_repository import InvitationRepository, InvitationExists
from src.backend.auth.db import get_pool
from src.backend.auth.invitations_db import init_invitations_db

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


def test_create_find_and_consume_invite():
    init_invitations_db()
    users, orgs, invites = UserRepository(), OrgRepository(), InvitationRepository()
    owner_email = f"oi-{uuid.uuid4().hex[:8]}@x.com"
    invitee_email = f"inv-{uuid.uuid4().hex[:8]}@x.com"
    try:
        owner = users.create_user(owner_email, "password123", "Owner")
        org_id = orgs.create_team_org("Acme", str(owner["id"]))
        inv = invites.create(invitee_email, org_id, str(owner["id"]))
        assert inv["status"] == "open"
        with pytest.raises(InvitationExists):
            invites.create(invitee_email, org_id, str(owner["id"]))
        found = invites.find_open_by_email(invitee_email)
        assert found is not None and str(found["org_id"]) == org_id
        invitee = users.create_user(invitee_email, "password123", "Inv")
        invites.consume(str(found["id"]), str(invitee["id"]))
        assert invites.find_open_by_email(invitee_email) is None
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([owner_email, invitee_email],))
            conn.commit()
