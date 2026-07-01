import uuid
import pytest
from src.backend.auth.repository import UserRepository
from src.backend.auth.org_repository import OrgRepository
from src.backend.auth.db import get_pool

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


def test_create_team_org_and_add_member():
    users, orgs = UserRepository(), OrgRepository()
    owner_email = f"owner-{uuid.uuid4().hex[:8]}@x.com"
    member_email = f"mem-{uuid.uuid4().hex[:8]}@x.com"
    try:
        owner = users.create_user(owner_email, "password123", "Owner")
        member = users.create_user(member_email, "password123", "Mem")
        org_id = orgs.create_team_org("Acme", str(owner["id"]))
        orgs.add_member(org_id, str(member["id"]), "org_member")
        members = orgs.get_members(org_id)
        roles = {m["email"]: m["org_role"] for m in members}
        assert roles[owner_email] == "org_admin"
        assert roles[member_email] == "org_member"
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([owner_email, member_email],))
            conn.commit()


def test_is_team_admin_true_only_for_team_org_admin():
    users, orgs = UserRepository(), OrgRepository()
    owner_email = f"ta-{uuid.uuid4().hex[:8]}@x.com"
    member_email = f"tm-{uuid.uuid4().hex[:8]}@x.com"
    solo_email = f"ts-{uuid.uuid4().hex[:8]}@x.com"
    try:
        owner = users.create_user(owner_email, "password123", "Own")
        member = users.create_user(member_email, "password123", "Mem")
        solo = users.create_user(solo_email, "password123", "Solo")
        orgs.ensure_personal_org(str(solo["id"]), solo_email)  # personal org_admin
        org_id = orgs.create_team_org("Acme", str(owner["id"]))
        orgs.add_member(org_id, str(member["id"]), "org_member")
        assert orgs.is_team_admin(str(owner["id"])) is True     # team org_admin
        assert orgs.is_team_admin(str(member["id"])) is False    # team org_member
        assert orgs.is_team_admin(str(solo["id"])) is False      # personal org_admin only
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([owner_email, member_email, solo_email],))
            conn.commit()
