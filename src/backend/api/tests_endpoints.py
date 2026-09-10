"""Tests API -- the Tests page and its drawer (a test = a versioned NL
query + its generated Robot code; distinct from its individual RUNS, which
Activity/`/api/history` keeps listing).

Three routes. The two reads narrow the caller the same way: GET /api/tests
lists the visible tests, GET /api/tests/{test_id} returns one of them with
all of its versions and one page of its results. The second resolves
visibility through the SAME _VISIBLE_TEST_SQL predicate as the first, so a
test the list will not show is not readable one URL deeper; it answers 404
rather than 403 so a rejection cannot confirm that an id is real. The
write, PUT /api/tests/assignments, files tests into a folder -- the move
spec section 7.2 puts on a Tests row and section 10 case 6 defines as
moving the TEST, its results following by join.

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

can_move on a list row reports the SAME three terms PUT
/api/tests/assignments then enforces -- the caller has a concrete org, the
TEST's org equals it, and the caller is an org_admin or the test's own
author -- and the two live in this one module so they can be read against
each other rather than kept in step by hand.

It deliberately no longer mirrors list_history's can_move for RUNS, which
omits the org term on its owner branch. history_scope gives BOTH a
validated platform admin and an identified caller whose own token carries
no org the same org_id=None, list_tests then appends no te.org_id row
filter for either, and the mirrored flag therefore read True on every row
such a caller owned while the server refuses every one of those moves:
for the org-less caller PUT /api/tests/assignments answers 403 from
_require_org_scope before the registry is reached, and RunRegistry.
assign_tests refuses org_id=None on its own first statement in any case;
for the platform admin the te.org_id term refuses each foreign-org test.
list_history's own flag is left ALONE -- that gap is pre-existing and
recorded, and widening this task to runs would break the surgical-diff
constraint.

Referenced by: main.py (router registration).
Depends on: core/run_registry.py (RunRegistry.list_tests,
RunRegistry.get_test_detail, RunRegistry.assign_tests), api/history_scope.py
(the shared caller scope, shared with /api/history and /api/groups),
api/history_endpoints.py (_REPORT_STATUSES -- imported rather than restated
so both detail routes answer "is there a log.html" identically),
api/groups_endpoints.py (_require_identity / _require_org_scope /
_valid_uuid -- imported for the same reason: they carry the 403-not-401
contract for a token-less mutation, the rule that an org_role claim counts
only alongside a concrete org_id, and the 400 shape for a malformed id),
auth/jwt_utils.py (require_user).
"""

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.backend.api.groups_endpoints import (
    _require_identity, _require_org_scope, _valid_uuid,
)
from src.backend.api.history_endpoints import _REPORT_STATUSES
from src.backend.api.history_scope import history_scope
from src.backend.auth.jwt_utils import require_user
from src.backend.core.run_registry import get_run_registry

logger = logging.getLogger(__name__)

router = APIRouter()

# One assignment is one UPDATE with a test_id = ANY(%s) array, so the batch is
# the one request whose cost a client sets. 500 is far above any real
# selection: the Tests list clamps itself to 200 rows, the same bound
# groups_endpoints' _RUN_IDS_MAX reasons from over test_runs. Its own 500 is
# not imported -- these are two batch budgets over two tables, not one shared
# rule -- and this one also keeps the audit_log.detail JSON written below
# bounded.
_TEST_IDS_MAX = 500


class TestAssignmentsIn(BaseModel):
    test_ids: list[str]
    group_id: str | None = None


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
        # The three terms PUT /api/tests/assignments enforces, and nothing
        # else -- see this module's own docstring for why this no longer
        # mirrors list_history's flag for runs.
        #
        # The folder_org_id term is load-bearing, not defensive. Without it
        # a caller with no org compares None to an ORG-LESS test's None and
        # reads True on a move that route answers 403 for, which is the
        # same "two NULLs must not compare equal" trap the authorship gate
        # in _attach_test needs its own `user_id is not None` for. It also
        # excludes the token-less dev caller, whose mutations are 403 by
        # design (groups_endpoints' module docstring).
        t["can_move"] = (
            scope.folder_org_id is not None
            and t.get("org_id") == scope.folder_org_id
            and (scope.is_org_admin
                 or t.get("user_id") == scope.caller_user_id)
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


@router.put("/tests/assignments")
def assign_tests(
    request: Request,
    body: TestAssignmentsIn,
    user: dict | None = Depends(require_user),
):
    """Move tests into a folder (group_id null = remove from folder).

    Filing a test into a folder PUBLISHES it to the org; passing null takes
    it back to its author alone. Atomic: one test the caller may not file,
    or a folder outside their org, rejects the whole request -- and the
    rejection reads as 404, never 403, the convention
    GET /api/tests/{test_id} above follows for the same reason.

    Only the test's author, or an org_admin, may file it, and only inside
    their own org. Being able to SEE a colleague's published test is not
    authority over where it lives (owner decision D4). Those are the same
    three terms can_move reports on each row of the list above, which is
    what lets the Tests page offer the control only where it will be
    honoured.

    Rows move that the caller did not name: every RESULT of a filed test
    follows it, a peer's included, and both folder columns are written.
    That is the model rather than a cascade -- a result belongs to its test
    -- and RunRegistry.assign_tests' own docstring is the authority on
    which columns it writes and on why writing only the test's would leave
    an unfiled test's results still published. The audit detail below
    records the ids the CALLER gave, so those results leave or join the
    folder without appearing in the record: the same snapshot bound
    PUT /groups/assignments already documents for its own cascade.

    403 rather than 404 for a caller with no identity or no org, because
    those two refusals are about the CALLER and not about a folder whose
    existence a 404 is there to protect. Not 401 either -- the SPA reads
    every 401 as "session expired", clears the token and redirects to
    /login, so a 401 here logs the whole app out on a click.
    """
    user_id = _require_identity(user)
    org_id, is_org_admin = _require_org_scope(user)
    if not body.test_ids:
        raise HTTPException(400, "test_ids must not be empty")
    if len(body.test_ids) > _TEST_IDS_MAX:
        raise HTTPException(400, f"test_ids must not exceed {_TEST_IDS_MAX} ids")
    test_ids = [_valid_uuid(t, "test id") for t in body.test_ids]
    group_id = _valid_uuid(body.group_id, "group id") if body.group_id else None
    if not get_run_registry().assign_tests(
            org_id, user_id, is_org_admin, test_ids, group_id):
        raise HTTPException(404, "Group or test not found")
    # Audited only on success: the floor writes one row per request whatever
    # the status, and detail on a refused move would read as a move that
    # happened. test_ids is capped at _TEST_IDS_MAX, so the JSON stays
    # bounded.
    request.state.audit_detail = {"group_id": group_id, "test_ids": test_ids}
    return {"assigned": len(test_ids), "group_id": group_id}
