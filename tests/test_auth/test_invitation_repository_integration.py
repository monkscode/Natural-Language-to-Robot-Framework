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


def test_revoke_list_and_find_consumed():
    init_invitations_db()
    users, orgs, invites = UserRepository(), OrgRepository(), InvitationRepository()
    owner_email = f"ro-{uuid.uuid4().hex[:8]}@x.com"
    invitee_email = f"rv-{uuid.uuid4().hex[:8]}@x.com"
    try:
        owner = users.create_user(owner_email, "password123", "Owner")
        org_id = orgs.create_team_org("Acme", str(owner["id"]))

        inv = invites.create(invitee_email, org_id, str(owner["id"]))
        inv_id = str(inv["id"])

        # list_for_org returns the created invite with the exact dict shape
        # (no consumed_by key, matching the implementation).
        listed = invites.list_for_org(org_id)
        assert any(i["email"] == invitee_email for i in listed)
        entry = next(i for i in listed if i["email"] == invitee_email)
        assert entry["status"] == "open"
        assert set(entry.keys()) == {"id", "email", "status", "created_at"}

        # cross-org guard: revoking from a different org must fail (return None)
        # and must not touch the invite's status.
        assert invites.revoke(inv_id, str(uuid.uuid4())) is None
        still_open = next(i for i in invites.list_for_org(org_id) if i["id"] == inv_id)
        assert still_open["status"] == "open"

        # revoke succeeds for the owning org.
        revoked = invites.revoke(inv_id, org_id)
        assert revoked is not None and revoked["status"] == "revoked"

        # revoking again (already revoked, not open) returns None.
        assert invites.revoke(inv_id, org_id) is None

        # list_for_org reflects the revoked status.
        after_revoke = next(i for i in invites.list_for_org(org_id) if i["id"] == inv_id)
        assert after_revoke["status"] == "revoked"

        # find_consumed_by_user: a fresh invite, consumed by a new invitee.
        inv2 = invites.create(invitee_email, org_id, str(owner["id"]))
        invitee = users.create_user(invitee_email, "password123", "Inv")
        invitee_id = str(invitee["id"])
        invites.consume(str(inv2["id"]), invitee_id)
        consumed = invites.find_consumed_by_user(invitee_id)
        assert consumed is not None and str(consumed["org_id"]) == org_id
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([owner_email, invitee_email],))
            conn.commit()
