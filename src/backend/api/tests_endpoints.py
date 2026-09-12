"""Tests API -- the Tests page and its drawer (a test = a versioned NL
query + its generated Robot code; distinct from its individual RUNS, which
Activity/`/api/history` keeps listing).

Four routes. The two reads narrow the caller the same way: GET /api/tests
lists the visible tests, GET /api/tests/{test_id} returns one of them with
all of its versions and one page of its results. The second resolves
visibility through the SAME _VISIBLE_TEST_SQL predicate as the first, so a
test the list will not show is not readable one URL deeper; it answers 404
rather than 403 so a rejection cannot confirm that an id is real. The two
writes: PUT /api/tests/assignments files tests into a folder -- the move
spec section 7.2 puts on a Tests row and section 10 case 6 defines as
moving the TEST, its results following by join -- and
POST /api/tests/{test_id}/versions regenerates a test, appending version
n+1 on success (section 6.3). Both reads and both writes resolve the test
through the SAME predicate stack, and the two writes share the same three
authority terms through may_move_test.

GET /api/tests lists the caller's visible tests newest-result-first: their
own tests plus every test their org has published into a folder (the same
"owner, or the org published it" rule /api/history applies to runs, restated
over `tests` by RunRegistry.list_tests / _VISIBLE_TEST_SQL -- see section 5a
of the design spec for why results and tests need separate predicates).

Field scoping, restated here because it is easy to get backwards: version_count,
result_count, pass_count, last_status/last_run_at/last_run_id are WHOLE-TEST
(every version); health, running and spark are scoped to the CURRENT version
only. Every run-derived field is computed over the results THIS caller may
open, never over every result the test holds -- version_count is the one
above that is not run-derived. See RunRegistry.list_tests' own docstring for
the full rules behind each field.

can_move on a list row reports the SAME three terms PUT
/api/tests/assignments then enforces -- the caller has a concrete org, the
TEST's org equals it, and the caller is an org_admin or the test's own
author -- and since Task 7 they are one function, may_move_test, because
POST /api/tests/{test_id}/versions asks the same question. On a list row
"may move" and "may update" are therefore one answer, which is what lets
the Tests page offer both controls exactly where they will be honoured.

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

list_history's flag is no longer simply left alone: it now asks may_move_test
too, for a run that HAS a test, because filing a run files its test. It asks
it ALONGSIDE the run's own authority rather than instead of it -- assign_runs
refuses on either half independently -- so the two flags are deliberately not
the same expression. See history_endpoints._can_move_run. What remains
pre-existing and recorded there is the token-less dev caller, who still reads
True on a run with no test while the route answers 403.

Referenced by: main.py (router registration).
Depends on: core/run_registry.py (RunRegistry.list_tests,
RunRegistry.get_test_detail, RunRegistry.get_test_head,
RunRegistry.assign_tests),
services/workflow_service.py (stream_generate_only -- the same generation
POST /generate-test runs, told which test to append to),
api/endpoints.py (SSE_MEDIA_TYPE -- imported rather than restated so every
streaming route in this app declares one media type), api/history_scope.py
(the shared caller scope, shared with /api/history and /api/groups, and
may_move_test -- which lives there rather than here because
history_endpoints needs it and imports FROM this module would be a cycle),
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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.backend.api.endpoints import SSE_MEDIA_TYPE
from src.backend.api.groups_endpoints import (
    _require_identity, _require_org_scope, _valid_uuid,
)
from src.backend.api.history_endpoints import _REPORT_STATUSES
from src.backend.api.history_scope import history_scope, may_move_test
from src.backend.auth.jwt_utils import require_user
from src.backend.core.config import settings
from src.backend.core.run_registry import get_run_registry
from src.backend.services.workflow_service import stream_generate_only

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


class TestVersionIn(BaseModel):
    user_query: str
    # FastAPI answers 422 for anything else before the handler runs, the same
    # shape the list's health/sort params already use.
    mode: Literal["update", "new_test"]


def _hide_platform_admin_author(row: dict, flag: str, scope) -> None:
    """D7 on the Tests surface, and the same rule Activity applies to a run.

    `_hide_admin_author` withholds the author's ADDRESS on a RESULT made with
    platform-admin authority. A test and its versions are the same person's
    same act one click away, so without this the address Activity withheld
    arrived here instead -- measured on all three fields, reachable with no
    cross-org action at all: a platform admin who is an ordinary member of an
    org presses Generate, and the generation path computes the same flag.

    The disjunction is `_hide_admin_author`'s, character for character and
    for its reasons: a caller who holds the authority themselves keeps the
    address, and so does the token-less dev caller, who is the one caller
    ownership.py rule 1 exempts from every other check.

    The flag STAYS on the row, exactly as `ran_as_platform_admin` does on a
    run, and for the same reason _hide_admin_author gives: so the client can
    say "Platform admin" rather than render an unattributed blank -- the org
    is told who did it in the only sense that concerns them, without being
    handed a named individual. It reveals nothing the label does not; what
    R11-2 rejected was doing the SUPPRESSION on the client, not shipping the
    fact. The suppression is here, on the server, where it cannot be skipped.
    """
    if not row.get(flag):
        return
    if scope.is_admin or scope.caller_user_id is None:
        return
    # Only fields the row already carries: a test row has user_email, a
    # version row has created_by_email, and inventing the other one on
    # either would change the response shape the SPA's types describe.
    for field in ("user_email", "created_by_email"):
        if field in row:
            row[field] = None


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
    # Everything that says WHICH tests, built once and handed to both reads
    # below, so the tab counts can never be asked about a different list
    # than the rows are.
    which = {
        "user_id": scope.user_id, "org_id": scope.org_id,
        "q": q, "group": group,
        # The FOLDER scope is the caller's own org even when their TEST scope
        # is every org -- see history_scope.folder_org_id.
        "folder_org_id": scope.folder_org_id,
        # Only a platform admin (or the token-less dev caller) may READ a
        # test no user owns, so only they may list one -- the same rule
        # list_history applies to runs.
        "include_unowned": scope.is_admin or scope.caller_user_id is None,
    }
    reg = get_run_registry()
    tests, total = reg.list_tests(
        limit=limit, offset=offset, health=health, sort=sort, **which)
    # Every tab's number, not only the open one's (spec section 7.2): `total`
    # answers for the active health filter alone. Per-viewer, as the rows
    # are -- see count_tests_by_health for why each count equals the total
    # that tab would list.
    counts = reg.count_tests_by_health(**which)
    for t in tests:
        # The three terms PUT /api/tests/assignments enforces, and nothing
        # else -- see this module's own docstring for why this no longer
        # mirrors list_history's flag for runs, and may_move_test for why
        # each term is there. Shared with the update gate below rather than
        # restated, so a Tests row can never offer a control one of the two
        # writes then refuses.
        t["can_move"] = may_move_test(scope, t.get("org_id"), t.get("user_id"))
        # org_id was selected only to answer can_move, same as list_history.
        t.pop("org_id", None)
        _hide_platform_admin_author(t, "author_is_platform_admin", scope)
        if not scope.is_admin:
            # The internal user id stays admin-only; the EMAIL does not --
            # a folder is shared, so a colleague's test reaches this caller,
            # and an unattributed row would leave the org unable to say who
            # authored what. D7 is the ONE exception, applied just above.
            t.pop("user_id", None)
    return {
        "tests": tests,
        "total": total,
        "counts": counts,
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
    email: per-version IDENTITY is admin-only on this surface. It is NOT
    true that created_by always holds the test's author. That was true when
    this pop was written and Task 7 falsified it: create_test_version's
    update mode admits an org_admin who is not the author, and _attach_test's
    append writes the APPENDER's user_id. Only the mint and the migration
    collapse write the author.

    user_email and created_by_email stay, so a shared folder can still say
    who authored what -- with one exception, D7: an address recorded under
    platform-admin authority is withheld from everyone who does not hold it,
    by _hide_platform_admin_author, exactly as Activity does for a run. That
    exception is why the redaction here is no longer inverted: it used to
    drop the opaque uuid and keep the real address.
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
    # D7, per row: the test carries its AUTHOR's address, each version its
    # own CREATOR's, and since Task 7 those can be different people -- so the
    # two are asked separately rather than one standing in for the other.
    _hide_platform_admin_author(
        detail["test"], "author_is_platform_admin", scope)
    for v in detail["versions"]:
        _hide_platform_admin_author(v, "creator_is_platform_admin", scope)
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


@router.post("/tests/{test_id}/versions")
def create_test_version(
    request: Request,
    test_id: str,
    body: TestVersionIn,
    user: dict | None = Depends(require_user),
):
    """Regenerate a test: run the pipeline, and on success append version
    n+1 to this test (spec section 6.3 / 7.4). This is the Update dialog's
    write.

    `mode` picks between two different powers, and they have two different
    gates.

    **update** rewrites the code every LATER run of this test executes, for
    everyone who can see it -- a larger power than moving the test, which
    decision D4 already withholds from a peer. So it takes the same three
    terms `can_move` reports on each row above (may_move_test), AND
    visibility of the test. Both, not either: an org_admin passes the move
    terms for an AUTHOR-LESS test in their org, because assign_tests'
    org_admin branch never looks at the author -- while include_unowned
    hides that test from them in the list. Requiring both means an update
    can never target a test the caller cannot list, and on every Tests row
    "may move" and "may update" are one answer.

    The org-equality term is not bookkeeping either. Generation reads its
    hints from the CALLER's token org (run_crew passes it to
    SmartKeywordProvider), so without it a platform admin regenerates
    another org's test with THIS org's learned hints and writes the result
    into the other org's code.

    **new_test** takes only visibility, and no identity at all -- the same
    rule POST /generate-test applies, so the token-less dev caller may use
    it with AUTH_ENFORCED off. It is what a refused peer WOULD do instead:
    generation runs unchanged, so the new test is minted exactly as Generate
    mints one (their org, them as author, no folder, version 1 with no
    reason) and nothing links it to the source. Inheriting the source's
    folder was rejected: filing a test PUBLISHES it, which would hand a
    peer's private copy to the whole org.

    NO UI REACHES IT YET, and the sentence above must not be read as
    describing a path a user has. The SPA gates BOTH entry points to the
    Update dialog -- the row control and the drawer's -- on can_move, and a
    refused peer is exactly the caller whose can_move is false, so the peer
    this mode was built for cannot open the dialog that would send it. Their
    only path today is retyping the description into Generate. The gate is
    deliberate and pinned by a live test, not an oversight; giving the peer
    a fork control is new product surface and was deferred to P3 rather than
    added at the end of this branch (docs/TODO.md).

    403, not 404, for a caller with no identity or no org on `update`: those
    refusals are about the CALLER and not about a test whose existence a 404
    protects. Not 401 either -- the SPA reads every 401 as "session expired",
    clears the token and redirects to /login. Every other refusal is 404
    with one body, so a rejection cannot confirm that an id is real.

    The `reason` is computed HERE, from the pre-flight read, and never at
    append time: comparing at append would label an untouched description
    'edited' whenever somebody else's update landed mid-generation.
    'regenerated' means the description came back byte-identical once
    trimmed -- case-sensitive, inner whitespace kept, because a capital
    letter or a doubled space can change what a test asserts. It is a drift
    signal (spec section 9): a false 'regenerated' pollutes it, a false
    'edited' only loses a candidate. A test whose user_query is NULL
    (paste-and-execute, case 9) is always 'edited' -- nothing was ever asked
    for, so nothing came back unchanged. The TRIMMED text is what generation
    receives and therefore what becomes tests.user_query, so the stored
    description can never differ from the text this comparison was made on.

    The audit floor records this request whatever the outcome; `mode` is the
    one thing in it that changes what the write MEANS, and the path already
    names the test. Its 200 records that the stream started -- the floor runs
    when the response object is made, which for a StreamingResponse is before
    a single event has been produced, let alone a version written.
    """
    test_id = _valid_uuid(test_id, "test id")
    user_query = body.user_query.strip()
    if not user_query:
        raise HTTPException(400, "Query not provided")
    if body.mode == "update":
        _require_identity(user)
        _require_org_scope(user)

    scope = history_scope(user)
    head = get_run_registry().get_test_head(
        test_id, user_id=scope.user_id, org_id=scope.org_id,
        # The FOLDER scope is the caller's own org even when their TEST
        # scope is every org -- see history_scope.folder_org_id.
        folder_org_id=scope.folder_org_id,
        # Only a platform admin (or the token-less dev caller) may read a
        # test no user owns, the same rule the two reads above apply.
        include_unowned=scope.is_admin or scope.caller_user_id is None,
    )
    if head is None:
        raise HTTPException(404, "Test not found")

    target = reason = None
    if body.mode == "update":
        if not may_move_test(scope, head["org_id"], head["user_id"]):
            raise HTTPException(404, "Test not found")
        target = head["test_id"]
        current = head["user_query"]
        reason = ("regenerated"
                  if current is not None and user_query == current.strip()
                  else "edited")

    # The same provider/key checks POST /generate-test makes, and for the
    # same reason: a missing key must fail here rather than inside the
    # stream, where the client has already been told generation began.
    model_provider = settings.MODEL_PROVIDER
    model_name = (settings.LOCAL_MODEL if model_provider == "local"
                  else settings.ONLINE_MODEL)
    if model_provider == "gemini" and not settings.GEMINI_API_KEY:
        raise HTTPException(
            500, "GEMINI_API_KEY environment variable is not set.")

    request.state.audit_detail = {"mode": body.mode}
    return StreamingResponse(
        stream_generate_only(user_query, model_provider, model_name,
                             user=user, regenerate_test_id=target,
                             version_reason=reason, report_version=True),
        media_type=SSE_MEDIA_TYPE,
    )
