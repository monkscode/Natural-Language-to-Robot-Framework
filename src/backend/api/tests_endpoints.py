"""Tests API -- the Tests page and its drawer (a test = a versioned NL
query + its generated Robot code; distinct from its individual RUNS, which
Activity/`/api/history` keeps listing).

Two routes, and both narrow the caller the same way: GET /api/tests lists
the visible tests, GET /api/tests/{test_id} returns one of them with all of
its versions and one page of its results. The second resolves visibility
through the SAME _VISIBLE_TEST_SQL predicate as the first, so a test the
list will not show is not readable one URL deeper; it answers 404 rather
than 403 so a rejection cannot confirm that an id is real.

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
reachable two ways, not one: a validated platform admin (history_scope
gives them org_id=None, so list_tests appends no te.org_id row filter at
all) AND an identified caller whose own token simply carries no org
(history_scope gives that shape the identical org_id=None, so the row
filter is equally absent for them -- see history_scope's own docstring).
Ordinary callers and org_admins are the ones excluded, by the concrete
te.org_id = <their current org> filter list_tests binds from a real
folder_org_id/org_id -- that part of the reasoning holds. For the org-less
caller specifically the disagreement is not narrow: assign_runs' very
first statement is `if org_id is None: return False`, an unconditional
refusal, so mirrored can_move reads True on every row that caller owns
while assign_runs would refuse every one of those same moves. The same
gap already exists, unfixed, for RUNS (see list_history); mirroring here
keeps tests and runs consistent with each other rather than making tests
stricter than runs for the identical caller shape. Whatever move endpoint
a later task builds for tests would need the same authority check
assign_runs already enforces for runs, so it would inherit this
disagreement too -- an offered move a caller-scoped authority check
refuses in exactly the case assign_runs already refuses for the run
equivalent, not a new gap this endpoint introduces.

Referenced by: main.py (router registration).
Depends on: core/run_registry.py (RunRegistry.list_tests,
RunRegistry.get_test_detail), api/history_scope.py (the shared caller scope,
shared with /api/history and /api/groups), api/history_endpoints.py
(_REPORT_STATUSES -- imported rather than restated so both detail routes
answer "is there a log.html" identically), auth/jwt_utils.py (require_user).
"""

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException

from src.backend.api.history_endpoints import _REPORT_STATUSES
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
    sort: Literal["last_run"] = "last_run",
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
    than "passing"/"failing" as no filter. sort is likewise a validated
    Literal now, the same shape as health -- FastAPI answers 422 on any
    value other than "last_run" (spec case 27's only defined order) before
    this body runs, closing the "offered control the server silently
    ignores" gap a free-form sort would leave next to a validated health.
    The registry itself still does not branch on sort's value -- see
    list_tests' own docstring for why.
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


@router.get("/tests/{test_id}")
def test_detail(
    test_id: str,
    limit: int = 50,
    offset: int = 0,
    user: dict | None = Depends(require_user),
):
    """The test drawer's data source: one test, all its versions, and one
    page of its results (spec section 6.2).

    Scoped by history_scope exactly as the list above is, and resolved
    through the same _VISIBLE_TEST_SQL predicate inside
    RunRegistry.get_test_detail -- a test the Tests page will not list is
    not readable one URL deeper.

    A test this caller may not see answers **404, never 403**, with the same
    body as an id that exists nowhere: the convention groups_endpoints
    states in its own module docstring and again on the
    PUT /groups/assignments handler ("rejection reads as 404, never 403"),
    because a distinguishable rejection tells the caller the id is real. A
    storage failure inside the registry lands here too -- see
    get_test_detail's docstring, which says so and why.

    A malformed test_id answers 400 rather than 404, the same shape
    /api/history/{run_id} uses for a run id. Every test_id this codebase
    mints is a uuid4 (both writers -- _attach_test's mint and the migration
    collapse -- generate one), so a non-uuid names no row that could exist.

    limit/offset page the RESULTS only, clamped to [1, 200] and floored at 0
    exactly as list_history clamps its own. Versions are not paged: spec
    case 28 pages the drawer over results.

    Two fields are removed from the payload before it goes out, matching the
    list's discipline for the same data. org_id is internal and is dropped
    for every caller. The internal user id is admin-only -- and `created_by`
    on each version goes with it, because it holds a user id rather than an
    email and today always holds the TEST'S author: _attach_test's mint
    writes it from the same value it writes into tests.user_id, and the
    migration collapse writes it from a run whose user_id is the very
    grouping key that becomes tests.user_id. Returning it would hand back
    the id the pop above just removed. user_email stays for everyone, so a
    shared folder can still say who authored what.

    Naming a version's author will need more than this once Task 7 lets
    someone other than the author append one; the fix is an email beside
    the id, as `tests.user_email` already does for the test, not relaxing
    this pop.
    """
    try:
        test_id = str(uuid.UUID(test_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid test id")

    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    scope = history_scope(user)
    detail = get_run_registry().get_test_detail(
        test_id,
        user_id=scope.user_id, org_id=scope.org_id,
        limit=limit, offset=offset,
        # The FOLDER scope is the caller's own org even when their TEST
        # scope is every org -- see history_scope.folder_org_id.
        folder_org_id=scope.folder_org_id,
        # Only a platform admin (or the token-less dev caller) may read a
        # test no user owns, the same rule the list applies.
        include_unowned=scope.is_admin or scope.caller_user_id is None,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Test not found")

    for r in detail["results"]:
        # history_endpoints' own constant, imported rather than restated:
        # both detail routes must answer "is there a log.html" identically,
        # and a second copy of the tuple is a second rule that can drift.
        r["has_report"] = r["status"] in _REPORT_STATUSES
    detail["test"].pop("org_id", None)
    if not scope.is_admin:
        detail["test"].pop("user_id", None)
        for v in detail["versions"]:
            v.pop("created_by", None)
    return detail
