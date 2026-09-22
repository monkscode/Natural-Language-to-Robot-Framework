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
run's stored Robot code via resolve_robot_code(). It admits what the
list admits, published runs included (D5), so a peer DOES open a
colleague's filed run here. Everything this caller may not reach 404s,
unknown ids included, so run existence cannot be probed by id.

Both responses also name the TEST a result belongs to and the version it
ran (test_id / test_name / test_query / test_version_n, spec 7.5), and
carry ran_as_platform_admin so the author column can honour D7 — a run
made with platform-admin authority names the role rather than a person to
every caller who does not hold that authority (_hide_admin_author).

Both responses carry rerun_of_accessible beside rerun_of: whether THIS caller
could open the original this row was cloned from, which is a live question —
un-filing the original un-publishes it. Both are computed by _can_open, the
same function that gates the detail endpoint, so the flag is that endpoint's
answer rather than a guess about it.

The detail response also carries can_read_feedback: whether
GET /api/feedback/{run_id} would answer this caller at all. That route
withholds is_grouped where this one passes it, so a peer reading a
published run is refused there and admitted here — and only the server
can tell the drawer which it is (_can_read_feedback).

Authorization for the report FILES under /reports/{run_id}/ is enforced
separately by authorize_report_access (auth/jwt_utils.py) using the same
ownership rows, so a user cannot open another user's log.html by URL.

Referenced by: main.py (router registration), api/endpoints.py
(resolve_robot_code for history reruns), frontend HistoryPage.
Depends on: core/run_registry.py, api/history_scope.py (the shared run scope,
shared with /api/groups, and may_move_test -- the TEST authority half of
can_move, shared with /api/tests), auth/jwt_utils.py, auth/ownership.py
(caller_can_access), core/artifact_store.py (get_artifact_store).
"""

import logging
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException

from src.backend.api.history_scope import (
    HistoryScope, history_scope, may_move_test,
)
from src.backend.auth.jwt_utils import require_user
from src.backend.auth.ownership import caller_can_access
from src.backend.core.run_registry import RunOwnership, get_run_registry
from src.backend.core.artifact_store import get_artifact_store
from src.backend.core.failure_sentences import failure_sentence

logger = logging.getLogger(__name__)

router = APIRouter()

# Statuses with Robot artifacts on disk — only these runs have a log.html.
_REPORT_STATUSES = ("passed", "failed")

# Statuses for which the detail payload carries a failure reason (Ruling R7).
# A reused run id (Task 1) can hold a stale message only while "running" —
# set_status writes error_message EXACTLY on every terminal write, so a pass
# clears it; "running" is the sole status where record_start's COALESCE lets
# the previous run's message survive. Restricting this tuple to failed/error
# is defence in depth for any other status, not the mechanism that clears a
# stale reason on a pass.
_FAILURE_STATUSES = ("failed", "error")


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


def _hide_admin_author(run: dict, scope: HistoryScope) -> None:
    """D7: withhold the AUTHOR'S ADDRESS on a run made with platform-admin
    authority, from every caller who does not hold that authority themselves.

    The flag itself stays on the row, so the client can say "Platform admin"
    rather than render an unattributed dash: the org is told who ran it in the
    only sense that concerns them, without being handed a named individual.

    Done here rather than as a rendering rule in the SPA, for two reasons that
    are about this codebase rather than about taste. The row's author cell is
    a "filter the list by this user" BUTTON, so an address that reaches the
    client reaches the search box with it; and `user_id` is already dropped
    from these same two payloads on the same principle. The stored COLUMN is
    untouched — D7's "the email stays stored, so audit and platform-admin
    views lose nothing" is about `test_runs`, not about this response.

    The token-less dev caller keeps the address, because
    is_validated_admin(None) is False and without the caller_user_id term the
    one caller ownership.py rule 1 exempts from every other check would be the
    only one reading History blind. It is the same disjunction list_history
    already applies to include_unowned, for the same reason.
    """
    if not run.get("ran_as_platform_admin"):
        return
    if scope.is_admin or scope.caller_user_id is None:
        return
    run["user_email"] = None


def _can_move_run(run: dict, scope: HistoryScope) -> bool:
    """May this caller file THIS run — the answer assign_runs will give.

    The list and the drawer both call this so they cannot disagree, and it
    mirrors assign_runs rather than restating a rule: that method refuses on
    TWO independent conditions, so this is their conjunction.

      * The TEST's authority (may_move_test). Filing a run files its test,
        so owning a RESULT of a colleague's test is not authority over it —
        a peer who re-ran a published test could otherwise re-publish, unfile
        or relocate the author's test, and re-admit the org to /reports for
        the author's own runs after the author took them private.
      * The RUN's own authority, unchanged below. assign_runs' parent UPDATE
        still matches on the run's owner and rolls the whole call back when
        its rowcount comes up short, so the test's own AUTHOR is refused a
        peer's result of it. Necessary and not sufficient, each way round.

    A run with NO test (test_id NULL — decision D8, a run with no
    robot_code) has no test authority to consult, and its owner files it on
    the run rule exactly as before. The INNER JOIN in assign_runs' refusal
    leaves that population alone for the same reason, so the two agree here
    too.

    Measured against assign_runs itself over 11 caller/run shapes: this
    agrees on 10. The one divergence is pre-existing and not this rule's —
    the token-less dev caller reads True on a run with no test while
    groups_endpoints 403s them before the registry is reached. Recorded in
    docs/TODO.md rather than closed here, because it is a behaviour change
    to the AUTH_ENFORCED=false path and D1 is about test authority.
    """
    return (
        scope.caller_user_id is None                       # dev, no token
        or run.get("user_id") == scope.caller_user_id      # own run
        or (scope.is_org_admin and run.get("org_id") == scope.folder_org_id)
    ) and (
        run.get("test_id") is None
        or may_move_test(scope, run.get("test_org_id"),
                         run.get("test_user_id"))
    )


def _original_owners(
    run_ids: set[str], scope: HistoryScope,
) -> dict[str, RunOwnership]:
    """Ownership rows for the ORIGINAL runs some re-run rows point at.

    ONE query for the whole page, and none at all when the set is empty —
    most pages carry no re-run row, and a per-row lookup was never on the
    table (owner ruling R4). Ids that are not well-formed UUIDs are dropped
    rather than looked up: run_detail answers 400 for those, which is not a
    200, so they are unreachable by the same definition.

    Split out from _reachable_originals so run_detail can answer TWO
    questions about the same original — "could you open it" and "may you read
    its feedback" — off one read. The two apply different predicates to these
    same rows, which is the whole point: they are different questions.
    """
    ids = set()
    for rid in run_ids:
        try:
            ids.add(str(uuid.UUID(rid)))
        except (ValueError, AttributeError, TypeError):
            continue
    return get_run_registry().get_run_owners_for_caller(
        sorted(ids), org_id=scope.folder_org_id,
        identified=scope.caller_user_id is not None,
    )


def _openable(
    owners: dict[str, RunOwnership], scope: HistoryScope, user: dict | None,
) -> set[str]:
    """Which of these runs GET /api/history/{id} would answer 200 for."""
    return {
        rid for rid, own in owners.items()
        if _can_open(scope, user,
                     {"user_id": own.user_id, "org_id": own.org_id,
                      "group_id": own.group_id})
    }


def _reachable_originals(
    run_ids: set[str], scope: HistoryScope, user: dict | None,
) -> set[str]:
    """Which of these original runs this caller could actually open."""
    return _openable(_original_owners(run_ids, scope), scope, user)


def _can_read_feedback(
    run: dict, scope: HistoryScope, user: dict | None,
    originals: dict[str, RunOwnership],
) -> bool:
    """Would GET /api/feedback/{run['run_id']} answer anything but 403?

    The same two gates get_run_corrections applies, in the same order:
    caller_can_access on the SUBMITTED row, then — because a re-run owns no
    learning record of its own and the read redirects to `rerun_of` — the
    same predicate again on the ORIGINAL (_gated_feedback_target). Both are
    computed off rows this request has already read.

    is_grouped is NOT passed, on either, and that omission is the entire
    reason this flag exists. _can_open above DOES pass it, so a peer opens a
    colleague's published run in the drawer; feedback deliberately does not,
    because filing a test into a folder publishes the test and never the
    corrections written against it. The drawer had no way to tell those two
    answers apart — a platform admin, a same-org org_admin and the
    token-less dev caller may all read a peer's feedback, and the client
    knows it is none of them — so it fired the request on every peer
    row and collected a 403 each time. The org_admin is the shape most
    easily missed: with is_grouped withheld, caller_can_access' last org
    rule still admits them on any OWNED row in their own org.

    An original that does not resolve is absent from `originals` and arrives
    here as two Nones, which caller_can_access refuses for everyone but a
    platform admin. That matches _gated_feedback_target, which substitutes an
    empty dict for an unreadable original and gates exactly that.

    This suppresses an OFFERED request; it does not replace a refusal. The
    feedback route keeps its own authorization unchanged, and the drawer
    keeps degrading silently if the request is ever made anyway.
    """
    if not caller_can_access(
            user, run.get("user_id"), run.get("org_id"),
            is_platform_admin=scope.is_admin):
        return False
    rerun_of = run.get("rerun_of")
    if not rerun_of:
        return True
    original = originals.get(rerun_of)
    return caller_can_access(
        user,
        original.user_id if original else None,
        original.org_id if original else None,
        is_platform_admin=scope.is_admin,
    )


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
        r["can_move"] = _can_move_run(r, scope)
        _hide_admin_author(r, scope)
        # org_id and the two test authority columns were selected only to
        # answer can_move.
        r.pop("org_id", None)
        r.pop("test_user_id", None)
        r.pop("test_org_id", None)
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
        # "all" means scope.user_id is None — no per-user narrowing — which
        # an org_admin gets for their whole org, not just a platform admin.
        # Only a platform admin, or the token-less dev caller when
        # AUTH_ENFORCED is off (see include_unowned above), sees every run
        # on the platform — and neither is_admin nor caller_user_id is part
        # of this response body, so the client tells the three cases apart
        # from its own role === 'admin' check against /auth/me, not from
        # anything returned here.
        "scope": "all" if scope.user_id is None else "own",
    }


@router.get("/history/{run_id}")
def run_detail(run_id: str, user: dict | None = Depends(require_user)):
    """One run plus its stored Robot code — the History drawer's data source.

    Same scoping as the list: the owner, while their token names the run's
    org (or names none, the legacy-token fallback); an org_admin of the
    run's org; any same-org caller on a run PUBLISHED into one of that org's
    folders (D5, via _can_open's is_grouped); or a validated admin — or,
    with AUTH_ENFORCED off, a caller with no token at all. For every caller
    with a token, unattributed legacy rows are admin-only, matching the
    /reports gate's fail-closed rule. Everything else the gate refuses 404s,
    unknown ids included.
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
    # R7: only a failed/errored run carries a reason. Every other status pops
    # the column the SELECT put on the dict and adds no sentence — a reused
    # run id (Task 1) can still hold a stale message while running (set_status
    # has not yet overwritten record_start's COALESCEd value), and it must
    # not render against that outcome.
    if run["status"] in _FAILURE_STATUSES:
        run["failure_sentence"] = failure_sentence(run["error_message"])
    else:
        run.pop("error_message", None)
    # At most ONE extra lookup, and only for a row that IS a re-run — the
    # drawer header offers the same link the row pill does, so it needs the
    # same answer. Same helper, so the two cannot disagree. The feedback flag
    # below asks a DIFFERENT question of the same rows, so it shares the read
    # rather than taking a second one.
    originals = _original_owners(
        {run["rerun_of"]} if run.get("rerun_of") else set(), scope)
    run["rerun_of_accessible"] = run.get("rerun_of") in _openable(
        originals, scope, user)
    # Whether GET /api/feedback/{run_id} would answer this caller at all. The
    # drawer fires that read on every open and it 403s by design for a peer's
    # published run, so without this the SPA could only learn the answer by
    # being refused. scope.is_admin is the same is_validated_admin result the
    # feedback route computes for itself — reused, not looked up again.
    run["can_read_feedback"] = _can_read_feedback(run, scope, user, originals)
    run["can_move"] = _can_move_run(run, scope)
    _hide_admin_author(run, scope)
    # org_id is internal — the _can_open access gate above and can_move are
    # its only readers; it is not part of the response. The two test
    # authority columns are can_move's alone, and go the same way.
    run.pop("org_id", None)
    run.pop("test_user_id", None)
    run.pop("test_org_id", None)
    if not scope.is_admin:
        # The email stays — see the list endpoint for why.
        run.pop("user_id", None)
    return run
