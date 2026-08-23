"""The one shared org-scoped access predicate (spec §6).

Every path (reports, history, rerun, feedback, traces, metrics) routes its
authorization decision through caller_can_access so the access model lives in
one place. Pure function: no DB, no request — the caller passes the decoded
JWT dict, the resource's owner_id/org_id, and a pre-computed is_platform_admin
flag (is_validated_admin does the DB re-validation at the call site).

ONE function, not two. It was briefly split into caller_can_read /
caller_can_act around an "an author keeps reading work they wrote, whatever
org they are in now" rule. That rule is gone (2026-08-23): leaving an org ends
access to the org's data, full stop, which is what every comparable product
does — GitHub, Notion and Figma all revoke on removal and keep the content
with the workspace. With the rule gone the two functions were byte-identical,
and the real axis between the call sites is is_grouped, which is a parameter.

Coexistence: a token minted before Phase 1b has no org_id claim. Such a caller
falls back to the exact pre-tenancy rule (owner_id == caller.user_id), so the
single existing org's behaviour is unchanged until every token has rotated.

Referenced by: auth/jwt_utils.py (reports), api/history_endpoints.py,
api/endpoints.py (rerun, feedback), api/trace_endpoints.py,
api/workflow_metrics_endpoints.py.
Depends on: nothing (pure).
"""


def caller_can_access(
    caller: dict | None,
    owner_id: str | None,
    org_id: str | None,
    *,
    is_platform_admin: bool,
    is_grouped: bool = False,
) -> bool:
    """May `caller` reach a resource owned by `owner_id` within `org_id`?

    Rules (first match wins):
    1. caller is None  -> allow (AUTH_ENFORCED off; permissive dev escape hatch).
    2. is_platform_admin -> allow (cross-org).
    3. PUBLISHED (is_grouped): the resource sits in one of its org's folders
       and the caller is in that org -> allow. Filing a run into a folder is
       how the team hands work to each other, so a peer who wrote none of it
       still reads it. Requires owner_id is not None, which keeps rule 5's
       fail-closed rule intact for a row nobody owns.
    4. caller has no org_id claim (legacy token) -> allow iff owner_id == caller.user_id.
    5. org-aware: org_id must equal the caller's org_id; within that org, allow iff
       caller is org_admin OR owner_id == caller.user_id.

    There is NO "the author keeps it whatever org they are in" rule. Nothing in
    a token distinguishes an internal transfer from an offboarding —
    remove_member drops the user into a fresh personal org exactly as a move
    does — so such a rule does not grant "an author who moved", it grants
    "anyone who ever authored anything here, forever". The org's data stays
    with the org. A user who rejoins gets everything back automatically,
    because visibility is computed from their CURRENT org rather than from a
    retention window.

    is_grouped is passed per call site, and the sites disagree on purpose:

      * history detail, /reports and a re-run PASS it. A published test is
        there for the team to read and repeat, and a re-run is attributed to
        whoever fired it, so accountability survives.
      * FEEDBACK DOES NOT. It mutates the org's learning store — conflict
        detection fires with org_id=record.org_id — and shaping the hints
        every future generation in the org receives is a different power from
        reading a colleague's test.

    Pass it from the RUN's stored folder, never from a client hint.

    Unattributed resources (owner_id None) fail closed for every non-platform
    caller — including a same-org org_admin and rule 3, which requires
    owner_id is not None. /reports exposes typed credentials, so a row no user
    owns (legacy/unknown run) is readable only by a platform admin (rule 2),
    never granted by the org-admin shortcut and never by being filed into a
    folder. run_registry._VISIBLE_RUN_SQL and _OWNED_RUN_SQL carry the same
    `user_id IS NOT NULL` term, so no list can offer a row this refuses.
    """
    if caller is None:
        return True
    if is_platform_admin:
        return True

    caller_org = caller.get("org_id")
    caller_uid = caller.get("user_id")

    if (is_grouped and owner_id is not None
            and org_id is not None and org_id == caller_org):
        return True

    if caller_org is None:  # legacy token, coexistence fallback
        return owner_id is not None and owner_id == caller_uid

    if org_id != caller_org:
        return False

    if caller.get("org_role") == "org_admin":
        return owner_id is not None

    return owner_id == caller_uid


def is_dashboard_viewer(caller: dict | None, *, is_platform_admin: bool) -> bool:
    """May `caller` view an org-level aggregate dashboard (traces/metrics)?

    True for: platform-admin (all orgs), the AUTH_ENFORCED-off escape hatch
    (caller None), or an org-admin (their own org). Plain org-members get no
    aggregate dashboard — they see only their own runs via caller_can_access.

    An org_admin claim without a resolvable org_id is rejected: callers derive
    the dashboard's org scope from caller["org_id"], so a None org_id would
    silently widen the query to every org. Fail closed at the gate instead.
    """
    if caller is None or is_platform_admin:
        return True
    return caller.get("org_role") == "org_admin" and caller.get("org_id") is not None
