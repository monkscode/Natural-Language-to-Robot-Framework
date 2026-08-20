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

from src.backend.auth.jwt_utils import is_validated_admin, require_user
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


def _require_org_scope(user: dict) -> tuple[str, bool]:
    """(org_id, is_org_admin) for a caller who is about to MUTATE folders.

    Folders are keyed to an org, so a caller whose token carries none has
    nowhere to put one: 403, rather than the NOT NULL violation the INSERT
    would surface as a 500. The guard also means the registry NEVER receives
    org_id=None on a write, where None means "unscoped, any org" and would
    let such a caller file their run into another org's folder.

    is_org_admin is history_endpoints.list_history's test: an org_role claim
    counts only alongside a concrete org_id, which the guard above assures.
    The caller's own identity stays user["user_id"] — an org_admin's FILTER
    user_id is None over in list_history, but their identity never is, and
    folder ownership is decided on identity.
    """
    org_id = user.get("org_id")
    if not org_id:
        raise HTTPException(403, "Your account is not in an organization yet")
    return org_id, user.get("org_role") == "org_admin"


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
    """The folders the caller can see, with run counts (plus the Ungrouped
    count) — drives the History chip row.

    Three callers, three answers. A validated PLATFORM admin passes org_id
    None, which the registry reads as "no visibility filter": they already
    see every org's runs, so hiding every folder name would just break the
    page. An org member passes their own org. A caller whose token carries
    NO org (a login that failed to provision one) gets an empty list —
    folders are org-keyed so they own none, and passing their None straight
    through would hand them that platform-admin view of every org's folders,
    other users' private ones included.
    """
    if user is None:
        return {"groups": [], "ungrouped_count": 0}
    reg = get_run_registry()
    me = user["user_id"]
    # The run scope MUST be the one /api/history computes for this same
    # caller, or the chip counts a different set of runs than the table lists
    # (an org_admin's History spans the org; a per-user count showed 1 beside
    # a table of 2). Branch order is load-bearing: admin, then org, then
    # empty — reversing the first two hands an org-less token the unscoped
    # every-org folder view.
    if is_validated_admin(user):
        scope_org_id = scope_user_id = None
        groups = reg.list_groups(None, me, scope_user_id=None)
    elif user.get("org_id"):
        scope_org_id = user["org_id"]
        scope_user_id = None if user.get("org_role") == "org_admin" else me
        groups = reg.list_groups(scope_org_id, me, scope_user_id=scope_user_id)
    else:
        # No org, so no folder can be theirs; the count still matches what
        # /api/history shows them, which is their own runs.
        scope_org_id, scope_user_id = None, me
        groups = []
    return {
        "groups": groups,
        "ungrouped_count": reg.count_ungrouped(scope_user_id, scope_org_id, me),
    }


@router.post("/groups", status_code=201)
def create_group(body: GroupIn, user: dict | None = Depends(require_user)):
    user_id = _require_identity(user)
    org_id, _ = _require_org_scope(user)
    name = _clean_name(body.name)
    try:
        return get_run_registry().create_group(org_id, user_id, name)
    except DuplicateGroupName:
        raise HTTPException(409, f'You already have a group named "{name}"')


@router.patch("/groups/{group_id}")
def rename_group(
    group_id: str, body: GroupIn, user: dict | None = Depends(require_user)
):
    user_id = _require_identity(user)
    org_id, is_org_admin = _require_org_scope(user)
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
    org_id, is_org_admin = _require_org_scope(user)
    group_id = _valid_uuid(group_id, "group id")
    if not get_run_registry().delete_group(org_id, user_id, is_org_admin, group_id):
        raise HTTPException(404, "Group not found")


@router.put("/groups/assignments")
def assign_runs(body: AssignmentsIn, user: dict | None = Depends(require_user)):
    """Move runs into a group (group_id null = remove from group). Atomic:
    any run or group that isn't the caller's rejects the whole request."""
    user_id = _require_identity(user)
    org_id, is_org_admin = _require_org_scope(user)
    if not body.run_ids:
        raise HTTPException(400, "run_ids must not be empty")
    run_ids = [_valid_uuid(r, "run id") for r in body.run_ids]
    group_id = _valid_uuid(body.group_id, "group id") if body.group_id else None
    if not get_run_registry().assign_runs(
            org_id, user_id, is_org_admin, run_ids, group_id):
        raise HTTPException(404, "Group or run not found")
    return {"assigned": len(run_ids), "group_id": group_id}
