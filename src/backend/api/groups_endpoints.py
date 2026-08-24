"""Run-groups API — the Test Runs page's folder feature.

Folders are ORG-keyed (run_groups.org_id) and nothing finer: every member of
an org sees every folder in it, and every run filed into one. Moving a run
into a folder is what PUBLISHES it to the team — an ungrouped run is work in
progress and stays with whoever is doing it. Anyone in the org creates a
folder; the creator, or an org_admin, renames it, and an ORG_ADMIN deletes
it — removing a folder un-publishes every run inside it, so that authority is
the org's whoever created the folder. A folder name
belongs to the org, case-insensitively, so a taken name answers 409 and a
testcase can only ever carry one folder name.

Seeing a shared run is NOT authority over it: a peer reads a colleague's
published test and re-runs it, but only its owner or an org_admin files it
somewhere else. A folder the caller cannot act on is always a 404, never a
403, so its existence cannot be probed by id.

Every mutation here sets request.state.audit_detail, so the framework audit
floor records WHAT changed alongside who changed it — the actor and the
timestamp were already free, the folder and the run ids were not.

The read scope comes from api/history_scope.py, the same helper /api/history
uses, so the chip counts and the table always describe one set of runs.

Token-less callers (AUTH_ENFORCED off, the bench and local-debug path): GET
returns an empty folder list with the real unscoped Ungrouped count — that
caller's /api/history shows every run — and mutations return 403. Not 401:
the SPA reads any 401 as "session expired", clears the token and redirects to
/login, so a 401 here logged the whole app out on a click.

Referenced by: main.py (router registration), frontend HistoryPage
(GroupChipsRow / MoveToGroupMenu / useGroups).
Depends on: core/run_registry.py, api/history_scope.py, auth/jwt_utils.py,
core/audit_log.py (the floor that reads request.state.audit_detail).
"""

import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.backend.api.history_scope import history_scope
from src.backend.auth.jwt_utils import require_user
from src.backend.core.run_registry import DuplicateGroupName, get_run_registry

logger = logging.getLogger(__name__)

router = APIRouter()

_NAME_MAX = 60
# One assignment is one UPDATE with a run_id = ANY(%s) array, so the batch is
# the one request whose cost a client sets. 500 is far above any real
# selection (the History page loads 200 rows at most).
_RUN_IDS_MAX = 500
# A folder's audit record needs its OWN, larger cap: it accumulates runs
# across its whole lifetime — many assignment batches, not the one a client
# sizes — so reusing _RUN_IDS_MAX would cap the common case, not just the
# extreme one. 2000 is 4x _RUN_IDS_MAX: comfortably above anything real (the
# live table holds 60 runs across 3 orgs and 0 folders today — R5) while
# keeping audit_log.detail (a TEXT column written on every delete) bounded
# to well under 100KB. A folder that ever exceeds it still deletes cleanly —
# the record just says plainly that its run_ids list was capped, instead of
# silently looking complete.
_DELETE_AUDIT_RUN_IDS_MAX = 2000


class GroupIn(BaseModel):
    name: str


class GroupPatchIn(BaseModel):
    name: str | None = None


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


def _valid_uuid(value: str, what: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(400, f"Invalid {what}")


def _duplicate(exc: DuplicateGroupName) -> HTTPException:
    """409 for a name collision. The name belongs to the ORG, so the copy
    cannot say "you already have" — the folder holding it may well be one a
    colleague created, and telling the caller it exists is the point: one
    name, one folder, so every testcase carries a single folder name.

    The exception carries the EXISTING folder's name, not the caller's
    spelling of it, so the sentence names something they can actually find in
    the list."""
    return HTTPException(409, f'A group named "{exc}" already exists')


@router.get("/groups")
def list_groups(user: dict | None = Depends(require_user)):
    """The org's folders, with run counts (plus the Ungrouped count) — drives
    the Test Runs chip row and the sidebar's quick-access list.

    Folders are scoped to the caller's own org, whoever they are. A caller
    with NO org — including the token-less dev caller — gets an empty list,
    because folders are org-keyed so they own none, and passing their None
    straight through would hand them the registry's "no filter" view of every
    org's folders.

    A validated platform admin is no exception: their RUN scope still spans
    every org, but their FOLDER scope is their own org's, the same value
    every mutation binds. Before that, /api/groups answered with every org's
    folder names while every write answered 404 for exactly those folders.

    run_count is the folder's WHOLE contents, not the caller's slice of it —
    a folder belongs to the org, so its count does too. Scoping it per user
    was what put a member's chip at 1 beside a folder holding 2.
    """
    scope = history_scope(user)
    reg = get_run_registry()
    groups = (
        reg.list_groups(scope.folder_org_id, run_org_id=scope.org_id)
        if scope.folder_org_id else []
    )
    return {
        "groups": groups,
        "ungrouped_count": reg.count_ungrouped(
            scope.user_id, scope.org_id,
            folder_org_id=scope.folder_org_id,
            include_unowned=scope.is_admin or scope.caller_user_id is None),
    }


@router.post("/groups", status_code=201)
def create_group(
    request: Request, body: GroupIn, user: dict | None = Depends(require_user)
):
    user_id = _require_identity(user)
    org_id, _ = _require_org_scope(user)
    name = _clean_name(body.name)
    try:
        created = get_run_registry().create_group(org_id, user_id, name)
    except DuplicateGroupName as exc:
        raise _duplicate(exc)
    request.state.audit_detail = {
        "group_id": created["group_id"], "name": name}
    return created


@router.patch("/groups/{group_id}")
def update_group(
    request: Request,
    group_id: str,
    body: GroupPatchIn,
    user: dict | None = Depends(require_user),
):
    """Rename a folder — its creator, or an org_admin.

    Returns only what it changed: the SPA discards this body and refetches
    the list, so echoing the untouched fields would cost an extra read.
    """
    user_id = _require_identity(user)
    org_id, is_org_admin = _require_org_scope(user)
    group_id = _valid_uuid(group_id, "group id")
    if body.name is None:
        raise HTTPException(400, "Provide a name to change")
    name = _clean_name(body.name)
    old_name: list[str] = []
    try:
        changed = get_run_registry().rename_group(
            org_id, user_id, is_org_admin, group_id, name,
            audit_old_name=old_name)
    except DuplicateGroupName as exc:
        raise _duplicate(exc)
    if not changed:
        raise HTTPException(404, "Group not found")
    # Audited only on success: detail on a refused rename would read as one
    # that happened. old_name names what the folder used to be called —
    # rename_group only ever fills it in on this same success path.
    request.state.audit_detail = {
        "group_id": group_id, "name": name, "old_name": old_name[0]}
    return {"group_id": group_id, "name": name}


@router.delete("/groups/{group_id}", status_code=204)
def delete_group(
    request: Request, group_id: str, user: dict | None = Depends(require_user)
):
    """Delete a folder; its runs return to Ungrouped (runs are never deleted).

    ORG_ADMIN ONLY. Ungrouped means "mine alone", so this UN-PUBLISHES every
    run the folder held — nothing is lost and any owner can re-file, but the
    org loses sight of that work until someone does. That consequence is the
    org's, so the authority is too, whoever created the folder.

    A member who is refused gets the same 404 as a folder that does not
    exist, so no caller can probe a folder with a delete.
    """
    user_id = _require_identity(user)
    org_id, is_org_admin = _require_org_scope(user)
    group_id = _valid_uuid(group_id, "group id")
    run_ids: list[str] = []
    if not get_run_registry().delete_group(
            org_id, user_id, is_org_admin, group_id, audit_run_ids=run_ids):
        raise HTTPException(404, "Group not found")
    # Audited only on success, matching every other mutation here. run_ids
    # is what D8 needs answered — "what stopped being shared" — and gets
    # its own cap (a folder outlives any one assignment's _RUN_IDS_MAX): a
    # truncated list says so plainly rather than quietly looking complete.
    request.state.audit_detail = {
        "group_id": group_id,
        "run_ids": run_ids[:_DELETE_AUDIT_RUN_IDS_MAX],
        "run_ids_truncated": len(run_ids) > _DELETE_AUDIT_RUN_IDS_MAX,
    }


@router.put("/groups/assignments")
def assign_runs(
    request: Request, body: AssignmentsIn, user: dict | None = Depends(require_user)
):
    """Move runs into a folder (group_id null = remove from folder).

    Filing a run into a folder PUBLISHES it to the org; passing null takes it
    back to the author alone. Atomic: one run the caller may not file, or a
    folder outside their org, rejects the whole request — and rejection reads
    as 404, never 403.

    Only the run's owner, or an org_admin, may file it. Being able to SEE a
    colleague's published test is not authority over where it lives.

    One row may move that the caller did not name: a re-run sitting in the
    SAME folder as a run being moved travels with it, because a re-run
    belongs to the test it was cloned from (see assign_runs). It can only
    ever REDUCE that re-run's exposure. The audit detail below records the
    ids the CALLER gave, so a cascaded re-run leaves the folder without
    appearing in the record — the same snapshot bound delete_group's
    docstring states, and a known gap rather than an oversight.
    """
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
    # Audited only on success: the floor writes one row per request whatever
    # the status, and detail on a refused move would read as a move that
    # happened. run_ids is capped at _RUN_IDS_MAX, so the JSON stays bounded.
    request.state.audit_detail = {"group_id": group_id, "run_ids": run_ids}
    return {"assigned": len(run_ids), "group_id": group_id}
