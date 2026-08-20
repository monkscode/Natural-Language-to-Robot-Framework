"""Personal run-groups API — the History page's folder feature.

Groups are per-user folders over test_runs rows (one group per run,
test_runs.group_id). Everything here is scoped to the caller's user_id:
cross-user reads and writes 404 so group existence cannot be probed,
mirroring history_endpoints.py. Org-admins can SEE members' runs via
/api/history but can never file another user's run into a group — the
assignment UPDATE's user_id predicate enforces that at the SQL level.

Token-less callers (AUTH_ENFORCED off): GET returns an empty list and all
mutations 401 — personal groups have no meaning without an identity.

Referenced by: main.py (router registration), frontend HistoryPage
(GroupChipsRow / MoveToGroupMenu / useGroups).
Depends on: core/run_registry.py, auth/jwt_utils.py.
"""

import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.backend.auth.jwt_utils import require_user
from src.backend.core.run_registry import DuplicateGroupName, get_run_registry

logger = logging.getLogger(__name__)

router = APIRouter()

_NAME_MAX = 60


class GroupIn(BaseModel):
    name: str


class AssignmentsIn(BaseModel):
    run_ids: List[str]
    group_id: Optional[str] = None


def _require_identity(user: dict | None) -> str:
    if user is None:
        raise HTTPException(401, "Sign-in required for groups")
    return user["user_id"]


def _scope(user: dict) -> tuple[str | None, bool]:
    """(org_id, is_org_admin) for the registry's authority checks — the same
    test history_endpoints.list_history applies. The caller's own identity
    stays user["user_id"]: an org_admin's FILTER user_id is None there, but
    their identity never is, and folder ownership is decided on identity."""
    org_id = user.get("org_id")
    return org_id, bool(org_id) and user.get("org_role") == "org_admin"


def _clean_name(raw: str) -> str:
    name = raw.strip()
    if not name or len(name) > _NAME_MAX:
        raise HTTPException(400, f"Group name must be 1-{_NAME_MAX} characters")
    return name


def _valid_uuid(value: str, what: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(400, f"Invalid {what}")


@router.get("/groups")
def list_groups(user: dict | None = Depends(require_user)):
    """The caller's groups with run counts (plus the Ungrouped count) —
    drives the History chip row."""
    if user is None:
        return {"groups": [], "ungrouped_count": 0}
    reg = get_run_registry()
    return {
        "groups": reg.list_groups(user.get("org_id"), user["user_id"]),
        "ungrouped_count": reg.count_ungrouped(user["user_id"]),
    }


@router.post("/groups", status_code=201)
def create_group(body: GroupIn, user: dict | None = Depends(require_user)):
    user_id = _require_identity(user)
    name = _clean_name(body.name)
    try:
        return get_run_registry().create_group(user.get("org_id"), user_id, name)
    except DuplicateGroupName:
        raise HTTPException(409, f'You already have a group named "{name}"')


@router.patch("/groups/{group_id}")
def rename_group(
    group_id: str, body: GroupIn, user: dict | None = Depends(require_user)
):
    user_id = _require_identity(user)
    org_id, is_org_admin = _scope(user)
    group_id = _valid_uuid(group_id, "group id")
    name = _clean_name(body.name)
    try:
        renamed = get_run_registry().rename_group(
            org_id, user_id, is_org_admin, group_id, name=name)
    except DuplicateGroupName:
        raise HTTPException(409, f'You already have a group named "{name}"')
    if not renamed:
        raise HTTPException(404, "Group not found")
    return {"group_id": group_id, "name": name}


@router.delete("/groups/{group_id}", status_code=204)
def delete_group(group_id: str, user: dict | None = Depends(require_user)):
    """Delete a group; its runs return to Ungrouped (runs are never deleted)."""
    user_id = _require_identity(user)
    org_id, is_org_admin = _scope(user)
    group_id = _valid_uuid(group_id, "group id")
    if not get_run_registry().delete_group(org_id, user_id, is_org_admin, group_id):
        raise HTTPException(404, "Group not found")


@router.put("/groups/assignments")
def assign_runs(body: AssignmentsIn, user: dict | None = Depends(require_user)):
    """Move runs into a group (group_id null = remove from group). Atomic:
    any run or group that isn't the caller's rejects the whole request."""
    user_id = _require_identity(user)
    org_id, is_org_admin = _scope(user)
    if not body.run_ids:
        raise HTTPException(400, "run_ids must not be empty")
    run_ids = [_valid_uuid(r, "run id") for r in body.run_ids]
    group_id = _valid_uuid(body.group_id, "group id") if body.group_id else None
    if not get_run_registry().assign_runs(
            org_id, user_id, is_org_admin, run_ids, group_id):
        raise HTTPException(404, "Group or run not found")
    return {"assigned": len(run_ids), "group_id": group_id}
