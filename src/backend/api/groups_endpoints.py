"""Run-groups API — the History page's folder feature.

Folders are ORG-keyed (run_groups.org_id) with a per-folder visibility flag:
an 'org' folder is visible to everyone in the org, a 'private' one only to
its creator. Anyone in the org may create; the creator — or an org_admin, on
an 'org' folder only — renames, flips visibility and deletes; a run may be
filed only into a folder the caller can see, and never into a private folder
whose owner does not own the run. Refusals are 404, never 403, so a private
folder's existence cannot be probed by id.

The read scope comes from api/history_scope.py, the same helper /api/history
uses, so the chip counts and the table always describe one set of runs.

Token-less callers (AUTH_ENFORCED off, the bench and local-debug path): GET
returns an empty folder list with the real unscoped Ungrouped count — that
caller's /api/history shows every run — and mutations return 403. Not 401:
the SPA reads any 401 as "session expired", clears the token and redirects to
/login, so a 401 here logged the whole app out on a click.

Referenced by: main.py (router registration), frontend HistoryPage
(GroupChipsRow / MoveToGroupMenu / useGroups).
Depends on: core/run_registry.py, api/history_scope.py, auth/jwt_utils.py.
"""

import logging
import uuid
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.backend.api.history_scope import history_scope
from src.backend.auth.jwt_utils import require_user
from src.backend.core.run_registry import (
    DuplicateGroupName,
    GroupVisibilityConflict,
    GroupVisibilityForbidden,
    _VISIBILITIES,
    get_run_registry,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_NAME_MAX = 60
# One assignment is one UPDATE with a run_id = ANY(%s) array, so the batch is
# the one request whose cost a client sets. 500 is far above any real
# selection (the History page loads 200 rows at most).
_RUN_IDS_MAX = 500


class GroupIn(BaseModel):
    name: str
    # Deliberately untyped: _clean_visibility validates, and a `str`
    # annotation would answer a null or a number with pydantic's 422, whose
    # detail is a list of objects where the SPA renders a string.
    visibility: Any = "org"


class GroupPatchIn(BaseModel):
    name: str | None = None
    visibility: Any = None


class AssignmentsIn(BaseModel):
    run_ids: List[str]
    group_id: Optional[str] = None


def _require_identity(user: dict | None) -> str:
    if user is None:
        raise HTTPException(403, "Sign-in required for groups")
    return user["user_id"]


def _require_org_scope(user: dict) -> tuple[str, bool]:
    """(org_id, is_org_admin) for a caller who is about to MUTATE folders.

    Folders are keyed to an org, so a caller whose token carries none has
    nowhere to put one: 403, rather than the NOT NULL violation the INSERT
    would surface as a 500. The guard also means the registry NEVER receives
    org_id=None on a write, where None means "unscoped, any org" and would
    let such a caller file their run into another org's folder.

    is_org_admin is history_scope's test: an org_role claim counts only
    alongside a concrete org_id, which the guard above assures. The caller's
    own identity stays user["user_id"] — an org_admin's FILTER user_id is
    None over in history_scope, but their identity never is, and folder
    ownership is decided on identity.
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


def _clean_visibility(raw: Any) -> str:
    """Validated by hand, not by a pydantic Literal: the SPA renders `detail`
    as a string, and FastAPI's 422 detail is a list of objects. Takes ANY
    JSON value for the same reason — a wrong TYPE has to reach this 400 too,
    not just a wrong string."""
    if raw not in _VISIBILITIES:
        raise HTTPException(
            400, f"Visibility must be one of: {', '.join(_VISIBILITIES)}")
    return raw


def _valid_uuid(value: str, what: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(400, f"Invalid {what}")


def _duplicate(exc: DuplicateGroupName) -> HTTPException:
    """409 for a name collision. The name is whichever scope the collision
    landed in — the org's for an 'org' folder, the creator's for a private
    one — so the copy cannot say "you already have", which is false for an
    org folder another member created."""
    return HTTPException(409, f'A group named "{exc}" already exists')


@router.get("/groups")
def list_groups(user: dict | None = Depends(require_user)):
    """The folders the caller can see, with run counts (plus the Ungrouped
    count) — drives the History chip row.

    Two answers, on ONE rule: folders are scoped to the caller's own org,
    whoever they are. A caller WITH an org sees its 'org' folders plus their
    own private ones. A caller with NO org — including the token-less dev
    caller — gets an empty list, because folders are org-keyed so they own
    none, and passing their None straight through would hand them the
    registry's "no visibility filter" view of every org's folders, other
    users' private ones included.

    A validated platform admin used to take a third, unfiltered branch here.
    That was the leak: every org's folder names came back, other users'
    private ones among them, while every mutation binds their real token org
    (_require_org_scope) and answered 404 for exactly those folders — so the
    SPA drew Edit/Delete/Move controls that could never work. Their RUN scope
    still spans every org; only their FOLDER scope narrowed.

    The run scope behind the counts MUST be the one /api/history computes for
    this same caller, or the chip counts a different set of runs than the
    table lists (an org_admin's History spans the org; a per-user count
    showed 1 beside a table of 2). history_scope is that one computation.
    """
    scope = history_scope(user)
    reg = get_run_registry()
    # ONE folder scope for everyone: the caller's own org. A platform admin
    # used to take an unfiltered branch here, which handed them every org's
    # folders including other users' private ones — while every mutation binds
    # this same org and answered 404 for exactly those folders. A caller with
    # no org (including the token-less dev caller) owns no folders, so [].
    if scope.folder_org_id:
        groups = reg.list_groups(
            scope.folder_org_id, scope.caller_user_id,
            scope_user_id=scope.user_id)
    else:
        groups = []
    return {
        "groups": groups,
        "ungrouped_count": reg.count_ungrouped(
            scope.user_id, scope.org_id, scope.caller_user_id,
            folder_org_id=scope.folder_org_id),
    }


@router.post("/groups", status_code=201)
def create_group(body: GroupIn, user: dict | None = Depends(require_user)):
    user_id = _require_identity(user)
    org_id, _ = _require_org_scope(user)
    name = _clean_name(body.name)
    visibility = _clean_visibility(body.visibility)
    try:
        return get_run_registry().create_group(org_id, user_id, name, visibility)
    except DuplicateGroupName as exc:
        raise _duplicate(exc)


@router.patch("/groups/{group_id}")
def update_group(
    group_id: str, body: GroupPatchIn, user: dict | None = Depends(require_user)
):
    """Rename a folder and/or change its visibility — at least one of them.

    Returns only what it changed: the SPA discards this body and refetches
    the list, so echoing the untouched fields would cost an extra read.
    'org' -> 'private' is refused with 409 while the folder still holds runs
    owned by other members, who would otherwise lose sight of their own runs;
    that message carries a COUNT and never who owns them.
    """
    user_id = _require_identity(user)
    org_id, is_org_admin = _require_org_scope(user)
    group_id = _valid_uuid(group_id, "group id")
    if body.name is None and body.visibility is None:
        raise HTTPException(400, "Provide a name or a visibility to change")
    name = None if body.name is None else _clean_name(body.name)
    visibility = (
        None if body.visibility is None else _clean_visibility(body.visibility)
    )
    try:
        changed = get_run_registry().rename_group(
            org_id, user_id, is_org_admin, group_id,
            name=name, visibility=visibility)
    except DuplicateGroupName as exc:
        raise _duplicate(exc)
    except GroupVisibilityConflict as exc:
        raise HTTPException(409, str(exc))
    except GroupVisibilityForbidden as exc:
        raise HTTPException(403, str(exc))
    if not changed:
        raise HTTPException(404, "Group not found")
    out = {"group_id": group_id}
    if name is not None:
        out["name"] = name
    if visibility is not None:
        out["visibility"] = visibility
    return out


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
    """Move runs into a folder (group_id null = remove from folder). Atomic:
    one run the caller may not file, or a folder they cannot see, rejects the
    whole request — and rejection reads as 404, never 403."""
    user_id = _require_identity(user)
    org_id, is_org_admin = _require_org_scope(user)
    if not body.run_ids:
        raise HTTPException(400, "run_ids must not be empty")
    if len(body.run_ids) > _RUN_IDS_MAX:
        raise HTTPException(400, f"run_ids must not exceed {_RUN_IDS_MAX} ids")
    run_ids = [_valid_uuid(r, "run id") for r in body.run_ids]
    group_id = _valid_uuid(body.group_id, "group id") if body.group_id else None
    if not get_run_registry().assign_runs(
            org_id, user_id, is_org_admin, run_ids, group_id):
        raise HTTPException(404, "Group or run not found")
    return {"assigned": len(run_ids), "group_id": group_id}
