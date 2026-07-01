"""Platform-owner access console: approve/reject/suspend users, manage orgs.

All routes require_admin. Mutations flow through the audit floor automatically;
handlers set request.state.audit_detail for from/to context. The approver
authority is can_approve (owner-only today; widen the predicate to delegate).

Referenced by: main.py (admin_access_router).
Depends on: auth/repository.py, auth/org_repository.py, auth/provisioning.py,
auth/approval_policy.py, auth/jwt_utils.py (require_admin).
"""
import logging
from typing import Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.backend.auth.approval_policy import can_approve
from src.backend.auth.db import get_pool
from src.backend.auth.jwt_utils import require_admin
from src.backend.auth.org_repository import OrgRepository
from src.backend.auth.provisioning import provision_on_approval
from src.backend.auth.repository import UserRepository

logger = logging.getLogger(__name__)

admin_access_router = APIRouter(prefix="/auth/admin", tags=["admin-access"])
_repo = UserRepository()
_orgs = OrgRepository()


def _public(row: dict) -> dict:
    return {"id": str(row["id"]), "email": row["email"],
            "role": row.get("role", "user"), "status": row["status"]}


def _transition(user_id: str, status: str, *, bump: bool, admin: dict, request: Request) -> dict:
    if not can_approve(admin):
        raise HTTPException(403, "Not permitted to manage this user")
    # Self-lockout guard: an admin may not suspend or reject their OWN account.
    # Both statuses deny login and bump token_version, so a self-transition locks
    # the admin out of the very console needed to undo it. Mirrors the self-demote
    # guard on POST /auth/admin/users/{id}/role. Approve/reactivate to self are
    # harmless (they keep the account usable) and stay allowed.
    if user_id == admin["user_id"] and status in ("suspended", "rejected"):
        raise HTTPException(400, "You cannot suspend or reject your own account")
    row = _repo.set_status(user_id, status, bump_token=bump)
    if row is None:
        raise HTTPException(404, "User not found")
    request.state.audit_detail = {"from": row["old_status"], "to": status}
    return _public(row)


@admin_access_router.get("/pending")
def list_pending(admin: dict = Depends(require_admin)):
    """Pending users, annotated with the org that invited them (if any)."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT u.id, u.email, u.display_name, o.name AS invited_org "
            "FROM users u "
            "LEFT JOIN invitations i ON i.consumed_by = u.id AND i.status = 'consumed' "
            "LEFT JOIN organizations o ON o.id = i.org_id "
            "WHERE u.status = 'pending' ORDER BY u.created_at",
        ).fetchall()
    return [
        {"id": str(r["id"]), "email": r["email"],
         "display_name": r["display_name"], "invited_org": r["invited_org"]}
        for r in rows
    ]


@admin_access_router.get("/users")
def list_users(admin: dict = Depends(require_admin)):
    """Every user (all statuses) for the Members tab. Read-only: no audit_detail."""
    return [
        {"id": str(r["id"]), "email": r["email"], "display_name": r["display_name"],
         "role": r["role"], "status": r["status"]}
        for r in _repo.list_users()
    ]


@admin_access_router.post("/users/{user_id}/approve")
def approve(user_id: str, request: Request, admin: dict = Depends(require_admin)):
    result = _transition(user_id, "active", bump=False, admin=admin, request=request)
    # Intentional and safe: _transition commits the user to active, then provision is called.
    # If provision raises, the user is active but the response is 500; because provision_on_approval
    # is idempotent (ensure_personal_org + add_member ON CONFLICT), an admin retry self-heals.
    provision_on_approval(user_id)
    return result


@admin_access_router.post("/users/{user_id}/reject")
def reject(user_id: str, request: Request, admin: dict = Depends(require_admin)):
    return _transition(user_id, "rejected", bump=True, admin=admin, request=request)


@admin_access_router.post("/users/{user_id}/suspend")
def suspend(user_id: str, request: Request, admin: dict = Depends(require_admin)):
    return _transition(user_id, "suspended", bump=True, admin=admin, request=request)


@admin_access_router.post("/users/{user_id}/reactivate")
def reactivate(user_id: str, request: Request, admin: dict = Depends(require_admin)):
    result = _transition(user_id, "active", bump=False, admin=admin, request=request)
    # Provision org membership, same as approve: a user rejected while pending was
    # never provisioned, so reactivating them without this leaves them active with
    # no org (Finding #3). Idempotent (ensure_personal_org + add_member ON CONFLICT),
    # so a suspended user who already has an org is unaffected.
    provision_on_approval(user_id)
    return result


class _Reassign(BaseModel):
    from_org_id: str
    to_org_id: str
    org_role: Literal["org_admin", "org_member"] = "org_member"


@admin_access_router.post("/users/{user_id}/reassign")
def reassign_user(user_id: str, body: _Reassign, request: Request,
                  admin: dict = Depends(require_admin)):
    """Move a user from one team org to another. Bumps token_version so the user's
    stale (wrong-org) token dies and their next request forces a fresh login into
    a correct-org token."""
    try:
        _orgs.reassign_user_org(user_id, body.from_org_id, body.to_org_id, body.org_role)
    except (psycopg.errors.ForeignKeyViolation, psycopg.errors.InvalidTextRepresentation, ValueError):
        raise HTTPException(400, "Invalid ids or non-team target org")
    _repo.bump_token_version(user_id)  # stale org token dies -> forces fresh login
    request.state.audit_detail = {"user_id": user_id, "from": body.from_org_id, "to": body.to_org_id}
    return {"status": "ok"}


class _CreateOrg(BaseModel):
    name: str
    owner_user_id: str


@admin_access_router.post("/orgs", status_code=201)
def create_org(body: _CreateOrg, request: Request, admin: dict = Depends(require_admin)):
    try:
        org_id = _orgs.create_team_org(body.name, body.owner_user_id)
    except (psycopg.errors.ForeignKeyViolation, psycopg.errors.InvalidTextRepresentation):
        raise HTTPException(400, "Unknown owner_user_id")
    request.state.audit_detail = {"org_id": org_id, "owner": body.owner_user_id}
    return {"org_id": org_id}


class _SetOwner(BaseModel):
    user_id: str
    org_role: Literal["org_admin", "org_member"]


@admin_access_router.post("/orgs/{org_id}/owner")
def set_owner(org_id: str, body: _SetOwner, request: Request,
              admin: dict = Depends(require_admin)):
    _orgs.set_org_owner(org_id, body.user_id, body.org_role)
    request.state.audit_detail = {"org_id": org_id, "user_id": body.user_id,
                                  "org_role": body.org_role}
    return {"status": "ok"}


@admin_access_router.get("/orgs/{org_id}/members")
def org_members(org_id: str, admin: dict = Depends(require_admin)):
    return _orgs.get_members(org_id)


class _AssignMember(BaseModel):
    user_id: str
    org_role: Literal["org_admin", "org_member"] = "org_member"


@admin_access_router.post("/orgs/{org_id}/members")
def assign_member(org_id: str, body: _AssignMember, request: Request,
                  admin: dict = Depends(require_admin)):
    """Seat a user in a team org. A bad UUID / unknown org-or-user / non-team org
    is a clean 400, never a 500."""
    try:
        _orgs.add_member(org_id, body.user_id, body.org_role)
    except (psycopg.errors.ForeignKeyViolation, psycopg.errors.InvalidTextRepresentation, ValueError):
        raise HTTPException(400, "Unknown org/user or non-team org")
    request.state.audit_detail = {"org_id": org_id, "user_id": body.user_id, "org_role": body.org_role}
    return {"status": "ok"}


@admin_access_router.delete("/orgs/{org_id}/members/{user_id}")
def remove_org_member(org_id: str, user_id: str, request: Request,
                      admin: dict = Depends(require_admin)):
    """Remove a membership. A non-UUID id is a 400; no such membership is a 404.
    Removing the user's last org provisions a personal org so they stay usable."""
    try:
        removed = _orgs.remove_member(org_id, user_id)
    except psycopg.errors.InvalidTextRepresentation:
        raise HTTPException(400, "Invalid org/user id")
    if not removed:
        raise HTTPException(404, "No such membership")
    request.state.audit_detail = {"org_id": org_id, "user_id": user_id}
    return {"status": "ok"}


@admin_access_router.get("/orgs")
def list_orgs(admin: dict = Depends(require_admin)):
    """Every org with member count for the Orgs tab. Read-only: no audit_detail."""
    return _orgs.list_orgs()
