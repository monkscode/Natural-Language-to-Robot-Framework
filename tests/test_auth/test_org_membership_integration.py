import uuid
import pytest
from src.backend.auth.repository import UserRepository
from src.backend.auth.org_repository import OrgRepository
from src.backend.auth.db import get_pool
from src.backend.core.run_registry import get_run_registry
from tests.test_auth.conftest import ensure_stub_learning_tables

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


def test_remove_member_rejects_non_team_org():
    """Finding #4: remove_member applies to TEAM orgs only. Targeting a personal
    org raises ValueError rather than deleting its sole membership and churning a
    replacement personal org — the personal membership is left untouched."""
    users, orgs = UserRepository(), OrgRepository()
    solo_email = f"rmt-{uuid.uuid4().hex[:8]}@x.com"
    personal_id = None
    try:
        solo = users.create_user(solo_email, "password123", "Solo")
        personal_id = orgs.ensure_personal_org(str(solo["id"]), solo_email)
        with pytest.raises(ValueError):
            orgs.remove_member(personal_id, str(solo["id"]))
        memberships = orgs.get_orgs_for_user(str(solo["id"]))
        assert len(memberships) == 1
        assert memberships[0]["org_id"] == personal_id  # unchanged, not churned
    finally:
        with get_pool().connection() as conn:
            if personal_id:
                conn.execute("DELETE FROM org_members WHERE org_id = %s", (personal_id,))
                conn.execute("DELETE FROM organizations WHERE id = %s", (personal_id,))
            conn.execute("DELETE FROM users WHERE email = %s", (solo_email,))
            conn.commit()


def test_add_member_moves_user_out_of_personal_org():
    """Single-active-org: assigning a user (who has a personal org) to a team org
    MOVES them; they end up solely in the team, and the emptied personal org
    row survives (org rows are permanent, never deleted)."""
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
        assert gone is not None  # emptied personal org survives, not deleted
    finally:
        with get_pool().connection() as conn:
            if org_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([owner_email, member_email],))
            conn.commit()


def test_add_member_keeps_personal_org_and_its_learning_rows():
    """A solo user's personal org holds learning rows before they join a
    team. add_member must not delete the vacated personal org: deleting it
    would strand the rows (their org_id would name an org that no longer
    exists, unclaimable by any future token) even though the row's own
    org_id value is left untouched."""
    users, orgs = UserRepository(), OrgRepository()
    owner_email = f"lrn-o-{uuid.uuid4().hex[:8]}@x.com"
    solo_email = f"lrn-s-{uuid.uuid4().hex[:8]}@x.com"
    org_id = None
    personal_id = None
    row_ids: dict[str, int] = {}
    try:
        owner = users.create_user(owner_email, "password123", "Own")
        solo = users.create_user(solo_email, "password123", "Solo")
        personal_id = orgs.ensure_personal_org(str(solo["id"]), solo_email)
        with get_pool().connection() as conn:
            ensure_stub_learning_tables(conn)
            for table in ("nl_feedback_corrections", "learning_anchors", "execution_records"):
                row = conn.execute(
                    f"INSERT INTO {table} (org_id) VALUES (%s) RETURNING id",
                    (personal_id,),
                ).fetchone()
                row_ids[table] = row["id"]
            conn.commit()

        org_id = orgs.create_team_org("Acme", str(owner["id"]))
        orgs.add_member(org_id, str(solo["id"]), "org_member")

        with get_pool().connection() as conn:
            for table, rid in row_ids.items():
                row = conn.execute(
                    f"SELECT org_id FROM {table} WHERE id = %s", (rid,)
                ).fetchone()
                assert row is not None, f"{table} row must not be deleted"
                assert row["org_id"] == personal_id, f"{table}.org_id must not be rewritten"
            gone = conn.execute("SELECT 1 FROM organizations WHERE id = %s",
                                (personal_id,)).fetchone()
        assert gone is not None, "the personal org row must survive"
    finally:
        with get_pool().connection() as conn:
            for table, rid in row_ids.items():
                conn.execute(f"DELETE FROM {table} WHERE id = %s", (rid,))
            if org_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            if personal_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (personal_id,))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([owner_email, solo_email],))
            conn.commit()


def test_create_team_org_moves_owner_out_of_personal_org():
    """Seating an owner in a new team org moves them out of their personal org
    (single-active-org): the owner ends up solely in the team; the personal
    org row survives (org rows are permanent, never deleted)."""
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
        assert gone is not None  # personal org survives, not deleted
    finally:
        with get_pool().connection() as conn:
            if org_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            conn.execute("DELETE FROM users WHERE email = %s", (owner_email,))
            conn.commit()


def test_create_team_org_keeps_personal_org_and_its_learning_rows():
    """Same shape as add_member: a solo user with learning rows in their
    personal org creates/owns a new team org. The new org must start with
    zero hints of its own, and the personal org must survive with its rows
    intact; create_team_org funnels through the same _collapse_to_single
    chokepoint as add_member."""
    users, orgs = UserRepository(), OrgRepository()
    solo_email = f"lrn-c-{uuid.uuid4().hex[:8]}@x.com"
    org_id = None
    personal_id = None
    row_ids: dict[str, int] = {}
    try:
        solo = users.create_user(solo_email, "password123", "Solo")
        personal_id = orgs.ensure_personal_org(str(solo["id"]), solo_email)
        with get_pool().connection() as conn:
            ensure_stub_learning_tables(conn)
            for table in ("nl_feedback_corrections", "learning_anchors", "execution_records"):
                row = conn.execute(
                    f"INSERT INTO {table} (org_id) VALUES (%s) RETURNING id",
                    (personal_id,),
                ).fetchone()
                row_ids[table] = row["id"]
            conn.commit()

        org_id = orgs.create_team_org("New Co", str(solo["id"]))

        with get_pool().connection() as conn:
            hint_count = conn.execute(
                "SELECT COUNT(*) AS n FROM nl_feedback_corrections WHERE org_id = %s",
                (org_id,),
            ).fetchone()
            assert hint_count["n"] == 0, "the new team org must start with zero hints"
            for table, rid in row_ids.items():
                row = conn.execute(
                    f"SELECT org_id FROM {table} WHERE id = %s", (rid,)
                ).fetchone()
                assert row is not None, f"{table} row must not be deleted"
                assert row["org_id"] == personal_id, f"{table}.org_id must not be rewritten"
            gone = conn.execute("SELECT 1 FROM organizations WHERE id = %s",
                                (personal_id,)).fetchone()
        assert gone is not None, "the personal org row must survive"
    finally:
        with get_pool().connection() as conn:
            for table, rid in row_ids.items():
                conn.execute(f"DELETE FROM {table} WHERE id = %s", (rid,))
            if org_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            if personal_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (personal_id,))
            conn.execute("DELETE FROM users WHERE email = %s", (solo_email,))
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
        assert gone is not None  # stray personal org survives, not pruned
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM organizations WHERE id = ANY(%s)",
                         ([o for o in (from_id, to_id) if o],))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([owner_email, member_email],))
            conn.commit()


def test_collapse_all_to_single_org_fixes_double_membership():
    """The one-time cleanup reduces a user in (team + personal) down to the team,
    keeps the personal org row (not pruned), bumps their token, and is
    idempotent on a second run."""
    users, orgs = UserRepository(), OrgRepository()
    owner_email = f"cas-o-{uuid.uuid4().hex[:8]}@x.com"
    member_email = f"cas-m-{uuid.uuid4().hex[:8]}@x.com"
    org_id = personal_id = None
    try:
        owner = users.create_user(owner_email, "password123", "Own")
        member = users.create_user(member_email, "password123", "Mem")
        org_id = orgs.create_team_org("Acme", str(owner["id"]))
        # Build the legacy double-membership directly (add_member would collapse it):
        personal_id = orgs.ensure_personal_org(str(member["id"]), member_email)
        with get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO org_members (org_id, user_id, org_role) VALUES (%s, %s, 'org_member')",
                (org_id, str(member["id"])),
            )
            conn.commit()
        assert len(orgs.get_orgs_for_user(str(member["id"]))) == 2
        tv_before = users.get_by_id(str(member["id"]))["token_version"]

        collapsed = orgs.collapse_all_to_single_org()
        assert collapsed >= 1
        memberships = orgs.get_orgs_for_user(str(member["id"]))
        assert len(memberships) == 1
        assert memberships[0]["org_id"] == org_id          # team kept over personal
        assert users.get_by_id(str(member["id"]))["token_version"] == tv_before + 1
        with get_pool().connection() as conn:
            gone = conn.execute("SELECT 1 FROM organizations WHERE id = %s",
                                (personal_id,)).fetchone()
        assert gone is not None  # personal org survives, not deleted

        assert orgs.collapse_all_to_single_org() == 0      # idempotent
    finally:
        with get_pool().connection() as conn:
            if org_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([owner_email, member_email],))
            conn.commit()


def test_reassign_user_org_keeps_vacated_personal_org():
    """Chokepoint proof (1 of 2): reassign_user_org is the second caller of
    _collapse_to_single. The fix must live in the shared helper, not be
    special-cased inside add_member alone, so a personal org vacated by a
    reassign must also survive."""
    users, orgs = UserRepository(), OrgRepository()
    owner_email = f"cpr-o-{uuid.uuid4().hex[:8]}@x.com"
    member_email = f"cpr-m-{uuid.uuid4().hex[:8]}@x.com"
    to_id = None
    personal_id = None
    try:
        owner = users.create_user(owner_email, "password123", "Own")
        member = users.create_user(member_email, "password123", "Mem")
        personal_id = orgs.ensure_personal_org(str(member["id"]), member_email)
        to_id = orgs.create_team_org("Reassign Co", str(owner["id"]))
        orgs.reassign_user_org(str(member["id"]), personal_id, to_id)
        memberships = orgs.get_orgs_for_user(str(member["id"]))
        assert len(memberships) == 1
        assert memberships[0]["org_id"] == to_id
        with get_pool().connection() as conn:
            gone = conn.execute("SELECT 1 FROM organizations WHERE id = %s",
                                (personal_id,)).fetchone()
        assert gone is not None  # vacated personal org survives reassign_user_org
    finally:
        with get_pool().connection() as conn:
            if to_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (to_id,))
            if personal_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (personal_id,))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([owner_email, member_email],))
            conn.commit()


def test_collapse_all_to_single_org_keeps_vacated_personal_org():
    """Chokepoint proof (2 of 2): collapse_all_to_single_org is the third
    caller of _collapse_to_single. Same proof as add_member and
    reassign_user_org: the one-time cleanup must not delete the personal
    org it vacates either."""
    users, orgs = UserRepository(), OrgRepository()
    owner_email = f"cpc-o-{uuid.uuid4().hex[:8]}@x.com"
    member_email = f"cpc-m-{uuid.uuid4().hex[:8]}@x.com"
    org_id = None
    personal_id = None
    try:
        owner = users.create_user(owner_email, "password123", "Own")
        member = users.create_user(member_email, "password123", "Mem")
        org_id = orgs.create_team_org("Collapse Co", str(owner["id"]))
        # Build the legacy double-membership directly (add_member would collapse it):
        personal_id = orgs.ensure_personal_org(str(member["id"]), member_email)
        with get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO org_members (org_id, user_id, org_role) VALUES (%s, %s, 'org_member')",
                (org_id, str(member["id"])),
            )
            conn.commit()
        assert len(orgs.get_orgs_for_user(str(member["id"]))) == 2
        collapsed = orgs.collapse_all_to_single_org()
        assert collapsed >= 1
        memberships = orgs.get_orgs_for_user(str(member["id"]))
        assert len(memberships) == 1
        assert memberships[0]["org_id"] == org_id
        with get_pool().connection() as conn:
            gone = conn.execute("SELECT 1 FROM organizations WHERE id = %s",
                                (personal_id,)).fetchone()
        assert gone is not None  # vacated personal org survives collapse_all_to_single_org
    finally:
        with get_pool().connection() as conn:
            if org_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            if personal_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (personal_id,))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([owner_email, member_email],))
            conn.commit()


def test_set_org_owner_role_change_keeps_single_membership():
    """Entry-point matrix's role-change row (single-active-org holds through a role
    change, not only a move): changing a member's role via set_org_owner updates the
    role in place and leaves them in EXACTLY that one org."""
    users, orgs = UserRepository(), OrgRepository()
    owner_email = f"sooc-o-{uuid.uuid4().hex[:8]}@x.com"
    member_email = f"sooc-m-{uuid.uuid4().hex[:8]}@x.com"
    org_id = None
    try:
        owner = users.create_user(owner_email, "password123", "Own")
        member = users.create_user(member_email, "password123", "Mem")
        org_id = orgs.create_team_org("Acme", str(owner["id"]))
        orgs.add_member(org_id, str(member["id"]), "org_member")  # member solely in team
        orgs.set_org_owner(org_id, str(member["id"]), "org_admin")  # role change, same org
        memberships = orgs.get_orgs_for_user(str(member["id"]))
        assert len(memberships) == 1
        assert memberships[0]["org_id"] == org_id
        assert memberships[0]["org_role"] == "org_admin"
    finally:
        with get_pool().connection() as conn:
            if org_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([owner_email, member_email],))
            conn.commit()

def _group_row(reg, group_id):
    """The raw folder row — the point is to prove what the row still says."""
    with reg._pool.connection() as conn:
        return conn.execute(
            "SELECT group_id, org_id, created_by, name "
            "FROM run_groups WHERE group_id = %s", (group_id,)).fetchone()


# ---------------------------------------------------------------------------
# A folder STAYS WITH ITS ORG when its creator leaves.
#
# A folder belongs to the org, not to whoever made it, so a departure changes
# nothing about it: the org keeps the folder, the runs inside it stay
# published to the org, and the remaining members can rename or delete it
# through the ordinary org_admin route. There is no folder ownership to
# transfer and nothing to clean up.
#
# This replaces the private-folder handling that came before it — a routine
# that flipped a departing member's private folders to 'org' visibility,
# publishing names they chose in private and silently renaming them on
# collision, plus the later decision to leave them stranded instead. Neither
# question exists once folders have no visibility of their own.
# ---------------------------------------------------------------------------

def test_a_folder_survives_its_creator_leaving_and_the_org_still_manages_it():
    """The contract: the folder is the org's, so a leaver takes nothing with
    them and strands nothing behind them. The org_admin who remains can still
    see it, rename it and delete it — no folder is ever unreachable."""
    users, orgs = UserRepository(), OrgRepository()
    reg = get_run_registry()
    admin_email = f"pmv-a-{uuid.uuid4().hex[:8]}@x.com"
    mover_email = f"pmv-m-{uuid.uuid4().hex[:8]}@x.com"
    old_org = new_org = None
    try:
        admin = users.create_user(admin_email, "password123", "Admin")
        mover = users.create_user(mover_email, "password123", "Mover")
        old_org = orgs.create_team_org("Old Co", str(admin["id"]))
        orgs.add_member(old_org, str(mover["id"]), "org_member")
        group = reg.create_group(old_org, str(mover["id"]), "checkout")

        new_org = orgs.create_team_org("New Co", str(mover["id"]))  # vacates old_org

        row = _group_row(reg, group["group_id"])
        assert row is not None, "the folder row must still exist"
        assert str(row["org_id"]) == str(old_org), "the folder stays with the org"
        assert row["name"] == "checkout", "the user's chosen name was rewritten"
        assert str(row["created_by"]) == str(mover["id"]), "authorship is history"

        # The org that kept the work can still see it AND still manage it —
        # this is what makes the folder reachable rather than stranded.
        seen = reg.list_groups(old_org)
        assert group["group_id"] in [g["group_id"] for g in seen]
        assert reg.rename_group(old_org, str(admin["id"]), True,
                                group["group_id"], "checkout v2") is True
        assert reg.delete_group(old_org, str(admin["id"]), True,
                                group["group_id"]) is True

        # And the name is free for the mover in the org they joined, because
        # names are scoped to the org.
        assert reg.create_group(new_org, str(mover["id"]), "checkout")
    finally:
        with get_pool().connection() as conn:
            # Folders name their org, and the BEFORE DELETE guard on
            # organizations refuses a delete while any row still does.
            # Clear them first — the guard is working, not in the way.
            for oid in (old_org, new_org):
                if oid:
                    conn.execute("DELETE FROM run_groups WHERE org_id = %s", (oid,))
                    conn.execute("DELETE FROM organizations WHERE id = %s", (oid,))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([admin_email, mover_email],))
            conn.commit()


def test_remove_member_leaves_the_folder_with_the_org():
    """remove_member deletes the membership directly — it never goes through
    _collapse_to_single, so it was the one genuinely separate call site the
    old folder handling needed. It now has no folder behaviour at all, and
    this is what says so."""
    users, orgs = UserRepository(), OrgRepository()
    reg = get_run_registry()
    admin_email = f"rmv-a-{uuid.uuid4().hex[:8]}@x.com"
    leaver_email = f"rmv-l-{uuid.uuid4().hex[:8]}@x.com"
    org_id = None
    try:
        admin = users.create_user(admin_email, "password123", "Admin")
        leaver = users.create_user(leaver_email, "password123", "Leaver")
        org_id = orgs.create_team_org("Kept Co", str(admin["id"]))
        orgs.add_member(org_id, str(leaver["id"]), "org_member")
        group = reg.create_group(org_id, str(leaver["id"]), "payments")

        assert orgs.remove_member(org_id, str(leaver["id"])) is True

        row = _group_row(reg, group["group_id"])
        assert row["name"] == "payments", "the folder is untouched by a removal"
        assert group["group_id"] in [
            g["group_id"] for g in reg.list_groups(org_id)]
    finally:
        with get_pool().connection() as conn:
            # Folders name their org, and the BEFORE DELETE guard on
            # organizations refuses a delete while any row still does.
            # Clear them first — the guard is working, not in the way.
            if org_id:
                conn.execute("DELETE FROM run_groups WHERE org_id = %s", (org_id,))
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([admin_email, leaver_email],))
            conn.commit()


# ---------------------------------------------------------------------------
# Task T2 — a user RECLAIMS their own personal org (organizations.owner_user_id)
#
# T1 made a vacated personal org row survive. That alone is not enough: the
# org is findable only through org_members, and the team join that vacated it
# deleted that exact row. Without owner_user_id, a user who goes team -> solo
# gets a BRAND NEW personal org every time, and every earlier one's learning
# history is orphaned forever. These tests pin the reclaim (ensure_personal_org
# steps 2-3), the token consequence, and the FK's SET NULL (never CASCADE).
# ---------------------------------------------------------------------------

def test_ensure_personal_org_reclaims_same_org_after_team_round_trip():
    """Test 5 — the round trip. A user goes team -> solo via remove_member.
    ensure_personal_org must return the SAME org id they had before joining
    the team (not a fresh one), AND the user must end up with exactly ONE
    org_members row. The membership half is the one that regresses silently
    if step 3's INSERT is ever dropped — remove_member calls
    ensure_personal_org precisely to keep a last-membership user usable, and
    a reclaim that returns the org id without seating the membership leaves
    the user with ZERO memberships."""
    users, orgs = UserRepository(), OrgRepository()
    owner_email = f"rt-o-{uuid.uuid4().hex[:8]}@x.com"
    solo_email = f"rt-s-{uuid.uuid4().hex[:8]}@x.com"
    org_id = None
    personal_id = None
    try:
        owner = users.create_user(owner_email, "password123", "Own")
        solo = users.create_user(solo_email, "password123", "Solo")
        personal_id = orgs.ensure_personal_org(str(solo["id"]), solo_email)

        org_id = orgs.create_team_org("Acme", str(owner["id"]))
        orgs.add_member(org_id, str(solo["id"]), "org_member")  # vacates personal_id
        assert orgs.remove_member(org_id, str(solo["id"])) is True  # solo again

        reclaimed = orgs.ensure_personal_org(str(solo["id"]), solo_email)

        assert reclaimed == personal_id, "must reclaim the SAME org, not mint a new one"
        memberships = orgs.get_orgs_for_user(str(solo["id"]))
        assert len(memberships) == 1, "must end up with exactly one membership"
        assert memberships[0]["org_id"] == personal_id
    finally:
        with get_pool().connection() as conn:
            if org_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            if personal_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (personal_id,))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([owner_email, solo_email],))
            conn.commit()


def test_token_after_team_round_trip_has_non_null_org_id():
    """Test 6 — the token. After the same team -> solo round trip,
    _token_payload must mint a non-NULL org_id claim. This is the direct
    guard against the unscoped-learning failure mode (org_id=None means
    every org's hints get injected into the user's runs) and is kept
    separate from test 5 so a membership-count regression and a token
    regression fail independently."""
    from src.backend.auth.endpoints import _token_payload
    from src.backend.auth.jwt_utils import decode_token

    users, orgs = UserRepository(), OrgRepository()
    owner_email = f"tok-o-{uuid.uuid4().hex[:8]}@x.com"
    solo_email = f"tok-s-{uuid.uuid4().hex[:8]}@x.com"
    org_id = None
    personal_id = None
    try:
        owner = users.create_user(owner_email, "password123", "Own")
        solo = users.create_user(solo_email, "password123", "Solo")
        personal_id = orgs.ensure_personal_org(str(solo["id"]), solo_email)

        org_id = orgs.create_team_org("Acme", str(owner["id"]))
        orgs.add_member(org_id, str(solo["id"]), "org_member")
        # remove_member's own last-membership self-heal reclaims personal_id.
        assert orgs.remove_member(org_id, str(solo["id"])) is True

        row = users.get_by_id(str(solo["id"]))
        claims = decode_token(_token_payload(row)["access_token"])

        assert claims["org_id"] is not None
        assert claims["org_id"] == personal_id
    finally:
        with get_pool().connection() as conn:
            if org_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            if personal_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (personal_id,))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([owner_email, solo_email],))
            conn.commit()


def test_deleting_user_nulls_owner_but_keeps_org_and_learning_rows():
    """Test 8 — the FK is not CASCADE. owner_user_id is ON DELETE SET NULL:
    deleting the owning user must leave the org row AND its learning rows
    intact, only clearing the owner link. CASCADE would delete the org row
    on owner deletion, reintroducing the exact bug T1 removed (a personal
    org disappearing out from under its still-live learning rows)."""
    users, orgs = UserRepository(), OrgRepository()
    email = f"fk-{uuid.uuid4().hex[:8]}@x.com"
    personal_id = None
    row_ids: dict[str, int] = {}
    try:
        user = users.create_user(email, "password123", "FK")
        personal_id = orgs.ensure_personal_org(str(user["id"]), email)
        with get_pool().connection() as conn:
            ensure_stub_learning_tables(conn)
            for table in ("nl_feedback_corrections", "learning_anchors", "execution_records"):
                row = conn.execute(
                    f"INSERT INTO {table} (org_id) VALUES (%s) RETURNING id",
                    (personal_id,),
                ).fetchone()
                row_ids[table] = row["id"]
            conn.commit()

        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE id = %s", (str(user["id"]),))
            conn.commit()

        with get_pool().connection() as conn:
            org_row = conn.execute(
                "SELECT owner_user_id FROM organizations WHERE id = %s", (personal_id,)
            ).fetchone()
            assert org_row is not None, "the org row must survive its owner's deletion"
            assert org_row["owner_user_id"] is None, "SET NULL, never CASCADE"
            for table, rid in row_ids.items():
                learning_row = conn.execute(
                    f"SELECT org_id FROM {table} WHERE id = %s", (rid,)
                ).fetchone()
                assert learning_row is not None, f"{table} row must survive"
                assert learning_row["org_id"] == personal_id
    finally:
        with get_pool().connection() as conn:
            for table, rid in row_ids.items():
                conn.execute(f"DELETE FROM {table} WHERE id = %s", (rid,))
            if personal_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (personal_id,))
            conn.execute("DELETE FROM users WHERE email = %s", (email,))  # no-op: already gone
            conn.commit()
