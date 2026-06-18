"""
Run-history API — the History tab's data source.

GET /api/history lists test_runs rows newest-first:
- regular users get ONLY their own runs (scoped by the JWT's user_id);
- admins get every user's runs (the admin claim is re-validated against the
  users table — a stale token of a demoted admin falls back to own-runs);
- with AUTH_ENFORCED off (local API-only debugging) token-less requests see
  everything, mirroring require_user's permissive escape hatch.

GET /api/history/{run_id} is the detail view (drawer): the same row plus the
run's stored Robot code via resolve_robot_code(). Unknown ids and other
users' runs both 404 so run existence cannot be probed by id.

Authorization for the report FILES under /reports/{run_id}/ is enforced
separately by authorize_report_access (auth/jwt_utils.py) using the same
ownership rows, so a user cannot open another user's log.html by URL.

Referenced by: main.py (router registration), api/endpoints.py
(resolve_robot_code for history reruns), frontend HistoryPage.
Depends on: core/run_registry.py, auth/jwt_utils.py, core/artifact_store.py
(get_artifact_store).
"""

import logging
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException

from src.backend.auth.jwt_utils import is_validated_admin, require_user
from src.backend.core.run_registry import get_run_registry
from src.backend.core.artifact_store import get_artifact_store

logger = logging.getLogger(__name__)

router = APIRouter()

# Statuses with Robot artifacts on disk — only these runs have a log.html.
_REPORT_STATUSES = ("passed", "failed")


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
    user: dict | None = Depends(require_user),
):
    """Role-scoped run history (own runs for users, all runs for admins).

    status narrows rows, total and pagination to one run status — the History
    tabs page within their own filter instead of the full list. q is a
    server-side substring search (description / owner email / run id) so it
    spans the whole result set rather than just the loaded page."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    q = q.strip() if q else None

    admin = is_validated_admin(user)
    # user is None only when AUTH_ENFORCED is off — permissive dev scope.
    scope_user_id = None if (admin or user is None) else user["user_id"]

    runs, total = get_run_registry().list_runs(
        user_id=scope_user_id, limit=limit, offset=offset, status=status, q=q
    )
    for r in runs:
        r["has_report"] = r["status"] in _REPORT_STATUSES
        if not admin:
            # A user's own rows don't need identity columns echoed back.
            r.pop("user_id", None)
            r.pop("user_email", None)
    return {
        "runs": runs,
        "total": total,
        "scope": "all" if scope_user_id is None else "own",
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

    run = get_run_registry().get_run(run_id)
    admin = is_validated_admin(user)
    is_owner = (
        run is not None
        and user is not None
        and run.get("user_id") is not None
        and run.get("user_id") == user.get("user_id")
    )
    if run is None or not (admin or user is None or is_owner):
        raise HTTPException(status_code=404, detail="Run not found")

    run["robot_code"] = resolve_robot_code(run)
    run["has_report"] = run["status"] in _REPORT_STATUSES
    if not admin:
        run.pop("user_id", None)
        run.pop("user_email", None)
    return run
