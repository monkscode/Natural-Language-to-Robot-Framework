"""Integration via TestClient: approve flips status and grants access."""
import uuid
import pytest
from fastapi.testclient import TestClient
from src.backend.main import app
from src.backend.auth.repository import UserRepository
from src.backend.auth.jwt_utils import create_access_token, decode_token
from src.backend.auth.db import get_pool
from src.backend.auth.invitations_db import init_invitations_db
from src.backend.auth.org_repository import OrgRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]
client = TestClient(app)


def _admin_token():
    repo = UserRepository()
    email = f"adm-{uuid.uuid4().hex[:8]}@x.com"
    row = repo.create_user(email, "password123", "Adm")
    repo.set_platform_role(str(row["id"]), "admin")
    repo.set_status(str(row["id"]), "active")
    full = repo.get_by_id(str(row["id"]))
    return email, create_access_token({
        "id": str(full["id"]), "email": full["email"], "role": "admin",
        "display_name": "Adm", "token_version": full["token_version"],
        "status": "active",
    })


def _active_user_token():
    """An ACTIVE non-admin user + token (must be active in the DB to pass
    require_admin's revalidation and reach the 403 role check)."""
    repo = UserRepository()
    email = f"usr-{uuid.uuid4().hex[:8]}@x.com"
    row = repo.create_user(email, "password123", "Usr")
    repo.set_status(str(row["id"]), "active")
    full = repo.get_by_id(str(row["id"]))
    return email, create_access_token({
        "id": str(full["id"]), "email": full["email"], "role": "user",
        "display_name": "Usr", "token_version": full["token_version"],
        "status": "active",
    })


def test_approve_makes_pending_user_active():
    init_invitations_db()
    repo = UserRepository()
    admin_email, token = _admin_token()
    pending_email = f"pend-{uuid.uuid4().hex[:8]}@x.com"
    try:
        pend = repo.create_user(pending_email, "password123", "Pend")
        r = client.post(f"/auth/admin/users/{pend['id']}/approve",
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["status"] == "active"
        assert repo.get_by_id(str(pend["id"]))["is_active"] is True
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([admin_email, pending_email],))
            conn.commit()


def test_create_org_unknown_owner_returns_400():
    """A nonexistent owner_user_id is a bad request, not a 500. The FK violation
    from create_team_org is mapped to a clean 400 (no orphan org left behind —
    create_team_org's connection context rolls back on the error)."""
    init_invitations_db()
    admin_email, token = _admin_token()
    try:
        r = client.post(
            "/auth/admin/orgs",
            json={"name": "Ghost Org", "owner_user_id": str(uuid.uuid4())},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (admin_email,))
            conn.commit()


def test_list_users_returns_seeded_users_with_status_and_role():
    init_invitations_db()
    repo = UserRepository()
    admin_email, token = _admin_token()
    u_email = f"mem-{uuid.uuid4().hex[:8]}@x.com"
    try:
        u = repo.create_user(u_email, "password123", "Mem")
        repo.set_status(str(u["id"]), "suspended")
        r = client.get("/auth/admin/users",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        by_email = {row["email"]: row for row in r.json()}
        assert admin_email in by_email and u_email in by_email
        assert by_email[admin_email]["role"] == "admin"
        assert by_email[admin_email]["status"] == "active"
        assert by_email[u_email]["role"] == "user"
        assert by_email[u_email]["status"] == "suspended"
        assert by_email[u_email]["display_name"] == "Mem"
        assert set(by_email[u_email].keys()) == {"id", "email", "display_name", "role", "status"}
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([admin_email, u_email],))
            conn.commit()


def test_list_users_forbidden_for_non_admin():
    init_invitations_db()
    email, token = _active_user_token()
    try:
        r = client.get("/auth/admin/users",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (email,))
            conn.commit()


def test_list_orgs_returns_member_counts():
    init_invitations_db()
    repo = UserRepository()
    orgs = OrgRepository()
    admin_email, token = _admin_token()
    owner_email = f"own-{uuid.uuid4().hex[:8]}@x.com"
    org_id = None
    try:
        owner = repo.create_user(owner_email, "password123", "Own")
        org_id = orgs.create_team_org("Acme QA", str(owner["id"]))
        r = client.get("/auth/admin/orgs",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        by_id = {row["id"]: row for row in r.json()}
        assert org_id in by_id
        assert by_id[org_id]["name"] == "Acme QA"
        assert by_id[org_id]["kind"] == "team"
        assert by_id[org_id]["member_count"] == 1
        assert set(by_id[org_id].keys()) == {"id", "name", "kind", "member_count"}
    finally:
        with get_pool().connection() as conn:
            if org_id:
                conn.execute("DELETE FROM org_members WHERE org_id = %s", (org_id,))
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([admin_email, owner_email],))
            conn.commit()


def test_list_orgs_forbidden_for_non_admin():
    init_invitations_db()
    email, token = _active_user_token()
    try:
        r = client.get("/auth/admin/orgs",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (email,))
            conn.commit()


def test_admin_cannot_suspend_self():
    """An admin must not be able to suspend their own account — suspend denies
    login and bumps token_version, so self-suspend is an irreversible-from-the-UI
    lockout. Guarded with a 400 (mirrors the self-demote guard on /role)."""
    init_invitations_db()
    repo = UserRepository()
    admin_email, token = _admin_token()
    admin_id = str(repo.get_by_email(admin_email)["id"])
    try:
        r = client.post(f"/auth/admin/users/{admin_id}/suspend",
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400
        # The account must be untouched — still active, no token bump.
        row = repo.get_by_id(admin_id)
        assert row["status"] == "active"
        assert row["token_version"] == 0
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (admin_email,))
            conn.commit()


def test_admin_cannot_reject_self():
    """Same lockout guard for self-reject (also denies login + bumps token)."""
    init_invitations_db()
    repo = UserRepository()
    admin_email, token = _admin_token()
    admin_id = str(repo.get_by_email(admin_email)["id"])
    try:
        r = client.post(f"/auth/admin/users/{admin_id}/reject",
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400
        row = repo.get_by_id(admin_id)
        assert row["status"] == "active"
        assert row["token_version"] == 0
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (admin_email,))
            conn.commit()


def test_admin_can_suspend_another_user():
    """Positive control: the self-guard must not block suspending OTHER users."""
    init_invitations_db()
    repo = UserRepository()
    admin_email, token = _admin_token()
    target_email = f"tgt-{uuid.uuid4().hex[:8]}@x.com"
    try:
        target = repo.create_user(target_email, "password123", "Tgt")
        repo.set_status(str(target["id"]), "active")
        r = client.post(f"/auth/admin/users/{target['id']}/suspend",
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert repo.get_by_id(str(target["id"]))["status"] == "suspended"
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([admin_email, target_email],))
            conn.commit()


def test_assign_member_adds_user_to_team_org():
    """POST /orgs/{id}/members seats a user in a team org (org_member)."""
    init_invitations_db()
    repo, orgs = UserRepository(), OrgRepository()
    admin_email, token = _admin_token()
    owner_email = f"amow-{uuid.uuid4().hex[:8]}@x.com"
    member_email = f"amm-{uuid.uuid4().hex[:8]}@x.com"
    org_id = None
    try:
        owner = repo.create_user(owner_email, "password123", "Ow")
        member = repo.create_user(member_email, "password123", "Mem")
        org_id = orgs.create_team_org("Acme", str(owner["id"]))
        r = client.post(f"/auth/admin/orgs/{org_id}/members",
                        json={"user_id": str(member["id"]), "org_role": "org_member"},
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert str(member["id"]) in {m["user_id"] for m in orgs.get_members(org_id)}
    finally:
        with get_pool().connection() as conn:
            if org_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([admin_email, owner_email, member_email],))
            conn.commit()


def test_assign_member_unknown_user_returns_400():
    """A nonexistent user_id is a bad request, not a 500 (FK violation → 400)."""
    init_invitations_db()
    repo, orgs = UserRepository(), OrgRepository()
    admin_email, token = _admin_token()
    owner_email = f"auow-{uuid.uuid4().hex[:8]}@x.com"
    org_id = None
    try:
        owner = repo.create_user(owner_email, "password123", "Ow")
        org_id = orgs.create_team_org("Acme", str(owner["id"]))
        r = client.post(f"/auth/admin/orgs/{org_id}/members",
                        json={"user_id": str(uuid.uuid4()), "org_role": "org_member"},
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400
    finally:
        with get_pool().connection() as conn:
            if org_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            conn.execute("DELETE FROM users WHERE email = %s", (owner_email,))
            conn.commit()


def test_remove_org_member_provisions_personal_when_orgless():
    """DELETE removes the membership; if it was the user's LAST org, a personal
    org is provisioned so they stay usable — exactly one membership afterward."""
    init_invitations_db()
    repo, orgs = UserRepository(), OrgRepository()
    admin_email, token = _admin_token()
    owner_email = f"rmow-{uuid.uuid4().hex[:8]}@x.com"
    member_email = f"rmm-{uuid.uuid4().hex[:8]}@x.com"
    org_id = None
    try:
        owner = repo.create_user(owner_email, "password123", "Ow")
        member = repo.create_user(member_email, "password123", "Mem")
        org_id = orgs.create_team_org("Acme", str(owner["id"]))
        orgs.add_member(org_id, str(member["id"]), "org_member")  # member's ONLY org
        r = client.delete(f"/auth/admin/orgs/{org_id}/members/{member['id']}",
                          headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        remaining = orgs.get_orgs_for_user(str(member["id"]))
        assert len(remaining) == 1
        assert remaining[0]["kind"] == "personal"
    finally:
        with get_pool().connection() as conn:
            if org_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([admin_email, owner_email, member_email],))
            conn.commit()


def test_remove_org_member_unknown_returns_404():
    """No such membership → 404 (not 500)."""
    init_invitations_db()
    repo, orgs = UserRepository(), OrgRepository()
    admin_email, token = _admin_token()
    owner_email = f"r4ow-{uuid.uuid4().hex[:8]}@x.com"
    org_id = None
    try:
        owner = repo.create_user(owner_email, "password123", "Ow")
        org_id = orgs.create_team_org("Acme", str(owner["id"]))
        r = client.delete(f"/auth/admin/orgs/{org_id}/members/{uuid.uuid4()}",
                          headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 404
    finally:
        with get_pool().connection() as conn:
            if org_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            conn.execute("DELETE FROM users WHERE email = %s", (owner_email,))
            conn.commit()


def test_reassign_user_moves_and_bumps_token():
    """POST reassign moves the user between team orgs and bumps token_version so
    their stale (wrong-org) token dies on the next request."""
    init_invitations_db()
    repo, orgs = UserRepository(), OrgRepository()
    admin_email, token = _admin_token()
    owner_email = f"rsow-{uuid.uuid4().hex[:8]}@x.com"
    member_email = f"rsm-{uuid.uuid4().hex[:8]}@x.com"
    from_id = to_id = None
    try:
        owner = repo.create_user(owner_email, "password123", "Ow")
        member = repo.create_user(member_email, "password123", "Mem")
        from_id = orgs.create_team_org("From Co", str(owner["id"]))
        to_id = orgs.create_team_org("To Co", str(owner["id"]))
        orgs.add_member(from_id, str(member["id"]), "org_member")
        tv_before = repo.get_by_id(str(member["id"]))["token_version"]
        r = client.post(f"/auth/admin/users/{member['id']}/reassign",
                        json={"from_org_id": from_id, "to_org_id": to_id},
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert repo.get_by_id(str(member["id"]))["token_version"] == tv_before + 1
        assert str(member["id"]) in {m["user_id"] for m in orgs.get_members(to_id)}
        assert str(member["id"]) not in {m["user_id"] for m in orgs.get_members(from_id)}
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM organizations WHERE id = ANY(%s)",
                         ([o for o in (from_id, to_id) if o],))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([admin_email, owner_email, member_email],))
            conn.commit()


def test_approve_bumps_token_version_so_stale_pending_token_dies():
    """After approve, the invitee's pending token (org_id=None) must be rejected on
    the next request so the SPA re-logs-in into a fresh org-bearing token (Finding
    #4). A fresh login then carries the provisioned org_id."""
    init_invitations_db()
    repo = UserRepository()
    admin_email, admin_token = _admin_token()
    pending_email = f"pt-{uuid.uuid4().hex[:8]}@x.com"
    try:
        pend = repo.create_user(pending_email, "password123", "Pend")  # pending, tv=0
        full = repo.get_by_id(str(pend["id"]))
        stale = create_access_token({
            "id": str(full["id"]), "email": full["email"], "role": "user",
            "display_name": "Pend", "org_id": None,
            "token_version": full["token_version"], "status": "pending",
        })
        # Sanity: while pending, the gate token is accepted by /auth/me.
        assert client.get("/auth/me",
                          headers={"Authorization": f"Bearer {stale}"}).status_code == 200
        r = client.post(f"/auth/admin/users/{pend['id']}/approve",
                        headers={"Authorization": f"Bearer {admin_token}"})
        assert r.status_code == 200
        # Approve bumped token_version → the stale pending token is now revoked.
        assert client.get("/auth/me",
                          headers={"Authorization": f"Bearer {stale}"}).status_code == 401
        # A fresh login carries the provisioned (non-null) org_id.
        login = client.post("/auth/login",
                            json={"email": pending_email, "password": "password123"})
        assert login.status_code == 200
        assert decode_token(login.json()["access_token"])["org_id"] is not None
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([admin_email, pending_email],))
            conn.commit()


def test_reactivate_provisions_org_for_never_provisioned_user():
    """A pending user who is rejected (never provisioned) then reactivated must end
    up usable with exactly one org membership — reactivate provisions on approval
    (Finding #3), not just flip status to active."""
    init_invitations_db()
    repo = UserRepository()
    orgs = OrgRepository()
    admin_email, token = _admin_token()
    target_email = f"react-{uuid.uuid4().hex[:8]}@x.com"
    try:
        target = repo.create_user(target_email, "password123", "React")  # pending, no org
        repo.set_status(str(target["id"]), "rejected")
        assert orgs.get_orgs_for_user(str(target["id"])) == []  # never provisioned
        r = client.post(f"/auth/admin/users/{target['id']}/reactivate",
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["status"] == "active"
        memberships = orgs.get_orgs_for_user(str(target["id"]))
        assert len(memberships) == 1
        assert memberships[0]["kind"] == "personal"
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([admin_email, target_email],))
            conn.commit()


@pytest.mark.parametrize("verb", ["approve", "reject", "suspend", "reactivate"])
def test_transition_malformed_id_returns_400(verb):
    """A non-UUID user id on a lifecycle route is a clean 400, never a 500 — the
    raw path id must not reach SQL as an invalid uuid literal (Finding #5)."""
    init_invitations_db()
    admin_email, token = _admin_token()
    try:
        r = client.post(f"/auth/admin/users/not-a-uuid/{verb}",
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (admin_email,))
            conn.commit()


def test_org_members_malformed_id_returns_400():
    """A non-UUID org id on GET members is a clean 400, never a 500 (Finding #5)."""
    init_invitations_db()
    admin_email, token = _admin_token()
    try:
        r = client.get("/auth/admin/orgs/not-a-uuid/members",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (admin_email,))
            conn.commit()


def test_reassign_forbidden_for_non_admin():
    """A non-admin cannot reassign — 403 before any mutation."""
    init_invitations_db()
    email, token = _active_user_token()
    try:
        r = client.post(f"/auth/admin/users/{uuid.uuid4()}/reassign",
                        json={"from_org_id": str(uuid.uuid4()), "to_org_id": str(uuid.uuid4())},
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (email,))
            conn.commit()


def test_reassign_unknown_target_org_returns_400():
    """An unknown (or non-team) target org is a bad request, not a 500."""
    init_invitations_db()
    repo = UserRepository()
    admin_email, token = _admin_token()
    member_email = f"ru-{uuid.uuid4().hex[:8]}@x.com"
    try:
        member = repo.create_user(member_email, "password123", "Mem")
        r = client.post(f"/auth/admin/users/{member['id']}/reassign",
                        json={"from_org_id": str(uuid.uuid4()), "to_org_id": str(uuid.uuid4())},
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([admin_email, member_email],))
            conn.commit()


def test_assign_member_bumps_moved_user_token():
    """Seating a user in a team org bumps their token_version so their stale
    (wrong-org) token dies and their next request re-logs-in into the new org."""
    init_invitations_db()
    repo, orgs = UserRepository(), OrgRepository()
    admin_email, token = _admin_token()
    owner_email = f"abm-o-{uuid.uuid4().hex[:8]}@x.com"
    member_email = f"abm-m-{uuid.uuid4().hex[:8]}@x.com"
    org_id = None
    try:
        owner = repo.create_user(owner_email, "password123", "Ow")
        member = repo.create_user(member_email, "password123", "Mem")
        org_id = orgs.create_team_org("Acme", str(owner["id"]))
        tv_before = repo.get_by_id(str(member["id"]))["token_version"]
        r = client.post(f"/auth/admin/orgs/{org_id}/members",
                        json={"user_id": str(member["id"]), "org_role": "org_member"},
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert repo.get_by_id(str(member["id"]))["token_version"] == tv_before + 1
    finally:
        with get_pool().connection() as conn:
            if org_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([admin_email, owner_email, member_email],))
            conn.commit()


def test_create_org_bumps_owner_token():
    """Creating a team org moves the owner into it, so their token_version is
    bumped and their stale token is refreshed on the next request."""
    init_invitations_db()
    repo, orgs = UserRepository(), OrgRepository()
    admin_email, token = _admin_token()
    owner_email = f"cob-{uuid.uuid4().hex[:8]}@x.com"
    org_id = None
    try:
        owner = repo.create_user(owner_email, "password123", "Ow")
        repo.set_status(str(owner["id"]), "active")
        tv_before = repo.get_by_id(str(owner["id"]))["token_version"]
        r = client.post("/auth/admin/orgs",
                        json={"name": "Newco", "owner_user_id": str(owner["id"])},
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 201
        org_id = r.json()["org_id"]
        assert repo.get_by_id(str(owner["id"]))["token_version"] == tv_before + 1
    finally:
        with get_pool().connection() as conn:
            if org_id:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            conn.execute("DELETE FROM users WHERE email = ANY(%s)",
                         ([admin_email, owner_email],))
            conn.commit()
