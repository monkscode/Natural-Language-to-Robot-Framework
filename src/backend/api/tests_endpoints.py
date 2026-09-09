"""Tests API -- the Tests page's list of tests (a test = a versioned NL
query + its generated Robot code; distinct from its individual RUNS, which
Activity/`/api/history` keeps listing).

GET /api/tests lists the caller's visible tests newest-result-first: their
own tests plus every test their org has published into a folder (the same
"owner, or the org published it" rule /api/history applies to runs, restated
over `tests` by RunRegistry.list_tests / _VISIBLE_TEST_SQL -- see section 5a
of the design spec for why results and tests need separate predicates).

Field scoping, restated here because it is easy to get backwards: version_count,
result_count, pass_count, last_status/last_run_at/last_run_id are WHOLE-TEST
(every version, every result); health, running and spark are scoped to the
CURRENT version only. See RunRegistry.list_tests' own docstring for the full
rules behind each field.

can_move mirrors list_history's shape exactly (own test, or an org_admin's
test in their own org) rather than the stricter check assign_runs enforces
for moving a RUN (which also requires the row's own org to equal the
caller's CURRENT org, even on the owner branch). That stricter check is
reachable only when a caller can both SEE a foreign/NULL-org row they own
and act as an identified caller -- structurally only a platform admin,
since the row-level org_id filter excludes such a row for every ordinary
caller and org_admin. The same gap already exists, unfixed, for RUNS (see
list_history); mirroring here keeps tests and runs consistent with each
other rather than making tests stricter than runs for the identical caller
shape. Whatever move endpoint a later task builds for tests would need the
same authority check assign_runs already enforces for runs, so it would
inherit this narrow disagreement too -- an offered move a caller-scoped
authority check refuses in exactly the case assign_runs already refuses
for the run equivalent, not a new gap this endpoint introduces.

Referenced by: main.py (router registration).
Depends on: core/run_registry.py (RunRegistry.list_tests), api/history_scope.py
(the shared caller scope, shared with /api/history and /api/groups),
auth/jwt_utils.py (require_user).
"""

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException

from src.backend.api.history_scope import history_scope
from src.backend.auth.jwt_utils import require_user
from src.backend.core.run_registry import get_run_registry

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/tests")
def list_tests(
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    group: str | None = None,
    health: Literal["all", "passing", "failing"] = "all",
    sort: str | None = None,
    user: dict | None = Depends(require_user),
):
    """The Tests page's data source: the caller's tests narrowed exactly as
    history_scope narrows /api/history for the same caller -- see that
    module's docstring for precisely which callers get "own" vs "all" (it
    is not simply "admins get all"; a solo signup is org_admin of their own
    personal org and gets "all" too).

    limit/offset/q/group follow the exact conventions list_history applies
    to runs: limit clamped to [1, 200], offset floored at 0, q trimmed and
    only a live filter when non-empty, group trimmed and treated as "no
    filter" when empty (a bare ?group= must not reach the registry as a
    literal group_id = '' match) and validated as a UUID unless it is the
    literal "ungrouped". health is validated by FastAPI's own enum against
    the public all|passing|failing vocabulary (no "flaky" -- owner ruling O2)
    before this body ever runs; RunRegistry.list_tests treats anything other
    than "passing"/"failing" as no filter. sort is accepted for forward
    compatibility -- see list_tests' own docstring for why it does nothing
    yet.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    q = q.strip() if q else None
    group = (group or "").strip() or None

    if group and group != "ungrouped":
        try:
            group = str(uuid.UUID(group))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid group id")

    scope = history_scope(user)
    tests, total = get_run_registry().list_tests(
        user_id=scope.user_id, org_id=scope.org_id,
        limit=limit, offset=offset, q=q, group=group, health=health,
        sort=sort,
        # The FOLDER scope is the caller's own org even when their TEST scope
        # is every org -- see history_scope.folder_org_id.
        folder_org_id=scope.folder_org_id,
        # Only a platform admin (or the token-less dev caller) may READ a
        # test no user owns, so only they may list one -- the same rule
        # list_history applies to runs.
        include_unowned=scope.is_admin or scope.caller_user_id is None,
    )
    for t in tests:
        # Mirrors list_history's can_move exactly -- see this module's own
        # docstring for the one place it does not also mirror assign_runs,
        # and why that is a deliberate "mirror, don't diverge" choice rather
        # than an oversight.
        t["can_move"] = (
            scope.caller_user_id is None                  # dev, no token
            or t.get("user_id") == scope.caller_user_id   # own test
            or (scope.is_org_admin and t.get("org_id") == scope.folder_org_id)
        )
        # org_id was selected only to answer can_move, same as list_history.
        t.pop("org_id", None)
        if not scope.is_admin:
            # The internal user id stays admin-only; the EMAIL does not --
            # a folder is shared, so a colleague's test reaches this caller,
            # and an unattributed row would leave the org unable to say who
            # authored what.
            t.pop("user_id", None)
    return {
        "tests": tests,
        "total": total,
        # Same vocabulary /api/history returns for the same caller -- "all"
        # means scope.user_id is None (a validated platform admin, an
        # org_admin viewing their whole org, or the token-less dev caller),
        # "own" otherwise. Deliberately not a third value: the client tells
        # those cases apart from its own role check against /auth/me, not
        # from anything returned here.
        "scope": "all" if scope.user_id is None else "own",
    }
