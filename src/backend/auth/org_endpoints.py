"""Org-owner surface: invite members into your own team org; list members.

Gated on being org_admin of a TEAM org (not a personal one). Org-owners can
invite and revoke invites; they CANNOT approve — approval stays with the owner
(can_approve). Invites are matched at signup by email and still require owner
approval.

Referenced by: main.py (org_router).
Depends on: auth/jwt_utils.py (require_user), auth/invitation_repository.py,
auth/org_repository.py, auth/db.py.
"""
import logging

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

from src.backend.auth.db import get_pool
from src.backend.auth.invitation_repository import InvitationExists, InvitationRepository
from src.backend.auth.jwt_utils import require_user
from src.backend.auth.org_repository import OrgRepository

logger = logging.getLogger(__name__)

org_router = APIRouter(prefix="/auth/org", tags=["org"])
_invites = InvitationRepository()
_orgs = OrgRepository()


def _require_team_org_admin(user: dict | None) -> tuple[str, dict]:
    """Return (org_id, user) if the caller is org_admin of a TEAM org; else 403.
    Re-reads membership from the DB so a demoted owner loses invite power at once."""
    if user is None:
        raise HTTPException(401, "Not authenticated")
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT m.org_id FROM org_members m "
            "JOIN organizations o ON o.id = m.org_id "
            "WHERE m.user_id = %s AND m.org_role = 'org_admin' AND o.kind = 'team' "
            "ORDER BY m.created_at LIMIT 1",
            (user["user_id"],),
        ).fetchone()
    if row is None:
        raise HTTPException(403, "Org-owner of a team org required")
    return str(row["org_id"]), user


class _InviteBody(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _norm(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or len(v) > 254:
            raise ValueError("Enter a valid email")
        return v


@org_router.post("/invitations", status_code=201)
def create_invitation(body: _InviteBody, request: Request,
                      user: dict | None = Depends(require_user)):
    org_id, _ = _require_team_org_admin(user)
    try:
        inv = _invites.create(body.email, org_id, user["user_id"])
    except InvitationExists:
        raise HTTPException(409, "An open invitation for that email already exists")
    request.state.audit_detail = {"email": body.email, "org_id": org_id}
    return {"id": str(inv["id"]), "email": inv["email"], "status": inv["status"]}


@org_router.get("/invitations")
def list_invitations(user: dict | None = Depends(require_user)):
    org_id, _ = _require_team_org_admin(user)
    return _invites.list_for_org(org_id)


@org_router.delete("/invitations/{invitation_id}")
def revoke_invitation(invitation_id: str, request: Request,
                      user: dict | None = Depends(require_user)):
    org_id, _ = _require_team_org_admin(user)
    try:
        row = _invites.revoke(invitation_id, org_id)
    except psycopg.errors.InvalidTextRepresentation:
        # A non-UUID invitation id is a client error (400), never a 500 (Finding #5).
        raise HTTPException(400, "Invalid invitation id")
    if row is None:
        raise HTTPException(404, "No open invitation with that id in your org")
    request.state.audit_detail = {"invitation_id": invitation_id, "org_id": org_id}
    return {"status": "ok"}


@org_router.get("/members")
def list_members(user: dict | None = Depends(require_user)):
    org_id, _ = _require_team_org_admin(user)
    return _orgs.get_members(org_id)
