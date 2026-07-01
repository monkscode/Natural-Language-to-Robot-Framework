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


def test_remove_member_deletes_and_reports():
    """remove_member deletes the row and returns True; a second removal of the
    same (now absent) membership returns False."""
    users, orgs = UserRepository(), OrgRepository()
    owner_email = f"ro-{uuid.uuid4().hex[:8]}@x.com"
    member_email = f"rm-{uuid.uuid4().hex[:8]}@x.com"
    try:
        owner = users.create_user(owner_email, "password123", "Own")
        member = users.create_user(member_email, "password123", "Mem")
        orgs.ensure_personal_org(str(member["id"]), member_email)  # keep them usable after removal
        org_id = orgs.create_team_org("Acme", str(owner["id"]))
        orgs.add_member(org_id, str(member["id"]), "org_member")
        assert orgs.remove_member(org_id, str(member["id"])) is True
        assert str(member["id"]) not in {m["user_id"] for m in orgs.get_members(org_id)}
        assert orgs.remove_member(org_id, str(member["id"])) is False  # nothing to delete
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([owner_email, member_email],))
            conn.commit()


def test_reassign_user_org_moves_atomically():
    """reassign_user_org removes the from-membership and adds the to-membership;
    the user ends up org_member of to_org and no longer in from_org."""
    users, orgs = UserRepository(), OrgRepository()
    owner_email = f"rao-{uuid.uuid4().hex[:8]}@x.com"
    member_email = f"ram-{uuid.uuid4().hex[:8]}@x.com"
    from_id = to_id = None
    try:
        owner = users.create_user(owner_email, "password123", "Own")
        member = users.create_user(member_email, "password123", "Mem")
        from_id = orgs.create_team_org("From Co", str(owner["id"]))
        to_id = orgs.create_team_org("To Co", str(owner["id"]))
        orgs.add_member(from_id, str(member["id"]), "org_member")
        orgs.reassign_user_org(str(member["id"]), from_id, to_id)
        to_roles = {m["user_id"]: m["org_role"] for m in orgs.get_members(to_id)}
        assert to_roles.get(str(member["id"])) == "org_member"
        assert str(member["id"]) not in {m["user_id"] for m in orgs.get_members(from_id)}
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM organizations WHERE id = ANY(%s)",
                         ([o for o in (from_id, to_id) if o],))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([owner_email, member_email],))
            conn.commit()


def test_reassign_rejects_personal_target_org():
    """A personal org is 1:1 with its user and never a move target — reassign
    into one raises ValueError."""
    users, orgs = UserRepository(), OrgRepository()
    owner_email = f"rpo-{uuid.uuid4().hex[:8]}@x.com"
    member_email = f"rpm-{uuid.uuid4().hex[:8]}@x.com"
    solo_email = f"rps-{uuid.uuid4().hex[:8]}@x.com"
    from_id = None
    try:
        owner = users.create_user(owner_email, "password123", "Own")
        member = users.create_user(member_email, "password123", "Mem")
        solo = users.create_user(solo_email, "password123", "Solo")
        from_id = orgs.create_team_org("From Co", str(owner["id"]))
        orgs.add_member(from_id, str(member["id"]), "org_member")
        personal_id = orgs.ensure_personal_org(str(solo["id"]), solo_email)
        with pytest.raises(ValueError):
            orgs.reassign_user_org(str(member["id"]), from_id, personal_id)
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([owner_email, member_email, solo_email],))
            conn.commit()


def test_add_member_rejects_non_team_org():
    """Finding #6: membership changes (add_member / set_org_owner) apply to TEAM
    orgs only — targeting a personal org raises ValueError (never silently seats
    a non-member/orphan owner on someone's personal org)."""
    users, orgs = UserRepository(), OrgRepository()
    solo_email = f"ntg-{uuid.uuid4().hex[:8]}@x.com"
    other_email = f"nto-{uuid.uuid4().hex[:8]}@x.com"
    try:
        solo = users.create_user(solo_email, "password123", "Solo")
        other = users.create_user(other_email, "password123", "Other")
        personal_id = orgs.ensure_personal_org(str(solo["id"]), solo_email)
        with pytest.raises(ValueError):
            orgs.add_member(personal_id, str(other["id"]), "org_member")
        with pytest.raises(ValueError):
            orgs.set_org_owner(personal_id, str(other["id"]), "org_admin")
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([solo_email, other_email],))
            conn.commit()


def test_add_member_moves_user_out_of_personal_org():
    """Single-active-org: assigning a user (who has a personal org) to a team org
    MOVES them — they end up solely in the team and the emptied personal org is
    deleted."""
    users, orgs = UserRepository(), OrgRepository()
    owner_email = f"amv-o-{uuid.uuid4().hex[:8]}@x.com"
    member_email = f"amv-m-{uuid.uuid4().hex[:8]}@x.com"
    org_id = None
    try:
        owner = users.create_user(owner_email, "password123", "Own")
        member = users.create_user(member_email, "password123", "Mem")
        personal_id = orgs.ensure_personal_org(str(member["id"]), member_email)
        org_id = orgs.create_team_org("Acme", str(owner["id"]))
        orgs.add_member(org_id, str(member["id"]), "org_member")
        memberships = orgs.get_orgs_for_user(str(member["id"]))
        assert len(memberships) == 1
        assert memberships[0]["org_id"] == org_id
        assert memberships[0]["kind"] == "team"
        with get_pool().connection() as conn:
            gone = conn.execute("SELECT 1 FROM organizations WHERE id = %s",
                                (personal_id,)).fetchone()
        assert gone is None  # emptied personal org deleted
    finally:
        with get_pool().connection() as conn:
            if org_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([owner_email, member_email],))
            conn.commit()


def test_create_team_org_moves_owner_out_of_personal_org():
    """Seating an owner in a new team org moves them out of their personal org
    (single-active-org): the owner ends up solely in the team, personal deleted."""
    users, orgs = UserRepository(), OrgRepository()
    owner_email = f"cto-{uuid.uuid4().hex[:8]}@x.com"
    org_id = None
    try:
        owner = users.create_user(owner_email, "password123", "Own")
        personal_id = orgs.ensure_personal_org(str(owner["id"]), owner_email)
        org_id = orgs.create_team_org("Globex", str(owner["id"]))
        memberships = orgs.get_orgs_for_user(str(owner["id"]))
        assert len(memberships) == 1
        assert memberships[0]["org_id"] == org_id
        assert memberships[0]["org_role"] == "org_admin"
        with get_pool().connection() as conn:
            gone = conn.execute("SELECT 1 FROM organizations WHERE id = %s",
                                (personal_id,)).fetchone()
        assert gone is None
    finally:
        with get_pool().connection() as conn:
            if org_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            conn.execute("DELETE FROM users WHERE email = %s", (owner_email,))
            conn.commit()


def test_reassign_collapses_to_target_even_with_stray_membership():
    """reassign must leave the user in EXACTLY the target org — even if they had a
    membership other than from_org (e.g. a stray personal org). Hardens the move so
    a wrong/stale from_org_id can't leave a second membership behind."""
    users, orgs = UserRepository(), OrgRepository()
    owner_email = f"rcs-o-{uuid.uuid4().hex[:8]}@x.com"
    member_email = f"rcs-m-{uuid.uuid4().hex[:8]}@x.com"
    from_id = to_id = personal_id = None
    try:
        owner = users.create_user(owner_email, "password123", "Own")
        member = users.create_user(member_email, "password123", "Mem")
        from_id = orgs.create_team_org("From Co", str(owner["id"]))
        to_id = orgs.create_team_org("To Co", str(owner["id"]))
        # Seat member in from_id, then hand-insert a stray personal membership so the
        # user has TWO memberships that are not to_id (add_member would collapse, so
        # build the pre-state directly).
        personal_id = orgs.ensure_personal_org(str(member["id"]), member_email)
        with get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO org_members (org_id, user_id, org_role) VALUES (%s, %s, 'org_member')",
                (from_id, str(member["id"])),
            )
            conn.commit()
        assert len(orgs.get_orgs_for_user(str(member["id"]))) == 2  # from + personal
        orgs.reassign_user_org(str(member["id"]), from_id, to_id)
        memberships = orgs.get_orgs_for_user(str(member["id"]))
        assert len(memberships) == 1
        assert memberships[0]["org_id"] == to_id
        with get_pool().connection() as conn:
            gone = conn.execute("SELECT 1 FROM organizations WHERE id = %s",
                                (personal_id,)).fetchone()
        assert gone is None  # stray personal org pruned
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM organizations WHERE id = ANY(%s)",
                         ([o for o in (from_id, to_id) if o],))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([owner_email, member_email],))
            conn.commit()
