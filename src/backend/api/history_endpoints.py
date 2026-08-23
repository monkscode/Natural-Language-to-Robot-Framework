"""
Run-history API — the History tab's data source.

GET /api/history lists test_runs rows newest-first:
- regular users get their own runs PLUS every run their org has published
  into a folder (moving a run into a folder is what shares it);
- admins get every user's runs (the admin claim is re-validated against the
  users table — a stale token of a demoted admin falls back to own-runs);
- with AUTH_ENFORCED off (local API-only debugging) token-less requests see
  everything, mirroring require_user's permissive escape hatch.

GET /api/history/{run_id} is the detail view (drawer): the same row plus the
run's stored Robot code via resolve_robot_code(). Unknown ids and other
users' runs both 404 so run existence cannot be probed by id.

Both responses carry rerun_of_accessible beside rerun_of: whether THIS caller
could open the original this row was cloned from, which is a live question —
un-filing the original un-publishes it. Both are computed by _can_open, the
same function that gates the detail endpoint, so the flag is that endpoint's
answer rather than a guess about it.

Authorization for the report FILES under /reports/{run_id}/ is enforced
separately by authorize_report_access (auth/jwt_utils.py) using the same
ownership rows, so a user cannot open another user's log.html by URL.

Referenced by: main.py (router registration), api/endpoints.py
(resolve_robot_code for history reruns), frontend HistoryPage.
Depends on: core/run_registry.py, api/history_scope.py (the shared run scope,
shared with /api/groups), auth/jwt_utils.py, core/artifact_store.py
(get_artifact_store).
"""

import logging
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException

from src.backend.api.history_scope import HistoryScope, history_scope
from src.backend.auth.jwt_utils import require_user
from src.backend.auth.ownership import caller_can_access
from src.backend.core.run_registry import get_run_registry
from src.backend.core.artifact_store import get_artifact_store

logger = logging.getLogger(__name__)

router = APIRouter()

# Statuses with Robot artifacts on disk — only these runs have a log.html.
_REPORT_STATUSES = ("passed", "failed")


def _can_open(scope: HistoryScope, user: dict | None, run: dict) -> bool:
    """Would GET /api/history/{run['run_id']} answer 200 for this caller?

    ONE expression, two call sites: run_detail's own gate, and the batched
    reachability of the ORIGINAL a re-run row points at. They have to give the
    same answer for the same run or the "Re-run of …" control lies in one
    direction or the other, and the only way to guarantee that is for them to
    be the same line of code.

    `run` must carry group_id from the CALLER-scoped folder join (get_run /
    get_run_owners_for_caller), never the raw test_runs.group_id column: "is
    this published to me" is "did a folder of MY org resolve"."""
    return caller_can_access(
        user, run.get("user_id"), run.get("org_id"),
        is_platform_admin=scope.is_admin,
        is_grouped=run.get("group_id") is not None,
    )


def _reachable_originals(
    run_ids: set[str], scope: HistoryScope, user: dict | None,
) -> set[str]:
    """Which of these original runs this caller could actually open.

    ONE query for the whole page, and none at all when the set is empty —
    most pages carry no re-run row, and a per-row lookup was never on the
    table (owner ruling R4). Ids that are not well-formed UUIDs are dropped
    rather than looked up: run_detail answers 400 for those, which is not a
    200, so they are unreachable by the same definition.
    """
    ids = set()
    for rid in run_ids:
        try:
            ids.add(str(uuid.UUID(rid)))
        except (ValueError, AttributeError, TypeError):
            continue
    owners = get_run_registry().get_run_owners_for_caller(
        sorted(ids), org_id=scope.folder_org_id,
        identified=scope.caller_user_id is not None,
    )
    return {
        rid for rid, own in owners.items()
        if _can_open(scope, user,
                     {"user_id": own.user_id, "org_id": own.org_id,
                      "group_id": own.group_id})
    }


def resolve_robot_code(run: dict) -> str | None:
    """Stored code for a run: the test_runs.robot_code column (written at
    generation/execution time), falling back to the run's stored test.robot
    via the artifact store for executed runs that predate the column. Legacy
    generate-only runs have no recoverable code -> None."""
    if run.get("robot_code"):
        return run["robot_code"]
    return get_artifact_store().read_text(run["run_id"], "test.robot")


@router.get("/history")
def list_history(
    limit: int = 50,
    offset: int = 0,
    status: Optional[Literal["generated", "running", "passed", "failed", "error"]] = None,
    q: Optional[str] = None,
    group: Optional[str] = None,
    user: dict | None = Depends(require_user),
):
    """Role-scoped run history (own runs for users, all runs for admins).

    status narrows rows, total and pagination to one run status — the History
    tabs page within their own filter instead of the full list. q is a
    server-side substring search (description / owner email / run id) so it
    spans the whole result set rather than just the loaded page. group narrows
    to one run folder (or "ungrouped"), combining with both."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    q = q.strip() if q else None
    # An empty (or whitespace-only) ?group= is the SPA clearing the chip.
    # Without the trailing `or None` it stayed "", skipped the uuid check
    # below (falsy) and reached the registry as a literal group_id = ''
    # filter, matching 0 rows instead of all of them. The q line above is
    # safe with a bare strip() only because q is consumed under `if q:`;
    # group is tested with `is not None`, so "" is a live filter value.
    group = (group or "").strip() or None

    # group: a run_groups id (uuid) or the literal "ungrouped". The filter
    # needs no extra authorization of its own: rows are already scoped below
    # and joined through the folder-visibility predicate, so filtering by a
    # folder the caller cannot see simply yields nothing.
    if group and group != "ungrouped":
        try:
            group = str(uuid.UUID(group))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid group id")

    scope = history_scope(user)
    runs, total = get_run_registry().list_runs(
        user_id=scope.user_id, org_id=scope.org_id,
        limit=limit, offset=offset, status=status, q=q, group=group,
        # The FOLDER scope is the caller's own org even when their RUN scope
        # is every org — see history_scope.folder_org_id.
        folder_org_id=scope.folder_org_id,
        # Only a platform admin (or the token-less dev caller) may READ a run
        # no user owns, so only they may list one. Without this an org_admin's
        # table carried rows whose drawer and report both answered 404.
        include_unowned=scope.is_admin or scope.caller_user_id is None,
    )
    # One lookup for the whole page, before the row loop — never one per row.
    reachable = _reachable_originals(
        {r["rerun_of"] for r in runs if r.get("rerun_of")}, scope, user)
    for r in runs:
        r["has_report"] = r["status"] in _REPORT_STATUSES
        # `rerun_of` stays on the wire because it is TRUE — the row is a
        # re-run — but whether the original is still reachable changes
        # underneath it: a peer re-runs a published run, its owner unfiles it,
        # and the link dies. The SPA draws a link only when this says so;
        # without it the pill was a control that could only 404, and the
        # drawer then read as "the original's code is missing".
        r["rerun_of_accessible"] = r.get("rerun_of") in reachable
        # Whether THIS caller may file THIS run, computed here rather than
        # re-derived in the SPA. Seeing a run and being able to move it are
        # different questions now that a folder publishes a run: a peer reads
        # a colleague's published test but only its owner or an org_admin
        # moves it, and a platform admin's table spans orgs whose folders
        # they do not have. Without the flag the UI drew a Move control on
        # every row, and on those two kinds it could only ever answer 404.
        r["can_move"] = (
            scope.caller_user_id is None                       # dev, no token
            or r.get("user_id") == scope.caller_user_id        # own run
            or (scope.is_org_admin and r.get("org_id") == scope.folder_org_id)
        )
        # org_id was selected only to answer can_move.
        r.pop("org_id", None)
        if not scope.is_admin:
            # The internal user id stays admin-only. The EMAIL does not: a
            # folder is shared, so a row a colleague wrote reaches this
            # caller, and an unattributed row would leave the org unable to
            # say who wrote what — the accountability the whole model rests
            # on (decision D8).
            r.pop("user_id", None)
    return {
        "runs": runs,
        "total": total,
        "scope": "all" if scope.user_id is None else "own",
    }


@router.get("/history/{run_id}")
def run_detail(run_id: str, user: dict | None = Depends(require_user)):
    """One run plus its stored Robot code — the History drawer's data source.

    Same scoping as the list: owner or validated admin (or any caller when
    AUTH_ENFORCED is off). Unattributed legacy rows are admin-only, matching
    the /reports gate's fail-closed rule.
    """
    try:
        run_id = str(uuid.UUID(run_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid run id")

    # Same folder scope as the list, from the same helper: the token-less dev
    # caller is unscoped, everyone else sees their own org's folders.
    scope = history_scope(user)
    run = get_run_registry().get_run(
        run_id, org_id=scope.folder_org_id,
        identified=scope.caller_user_id is not None)
    # group_id comes from the org-scoped join, so it is non-NULL only when
    # the folder belongs to THIS caller's org — which is exactly the fact the
    # published-run rule needs, already established by the read itself. That
    # holds for a caller with NO org only because identified says so: without
    # it the join fell back to its unfiltered form and named a folder from
    # any org at all.
    allowed = run is not None and _can_open(scope, user, run)
    if run is None or not allowed:
        raise HTTPException(status_code=404, detail="Run not found")

    run["robot_code"] = resolve_robot_code(run)
    run["has_report"] = run["status"] in _REPORT_STATUSES
    # At most ONE extra lookup, and only for a row that IS a re-run — the
    # drawer header offers the same link the row pill does, so it needs the
    # same answer. Same helper, so the two cannot disagree.
    run["rerun_of_accessible"] = run.get("rerun_of") in _reachable_originals(
        {run["rerun_of"]} if run.get("rerun_of") else set(), scope, user)
    run["can_move"] = (
        scope.caller_user_id is None
        or run.get("user_id") == scope.caller_user_id
        or (scope.is_org_admin and run.get("org_id") == scope.folder_org_id)
    )
    if not scope.is_admin:
        # The email stays — see the list endpoint for why.
        run.pop("user_id", None)
    return run
