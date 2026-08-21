"""The one shared org-scoped ownership predicate (spec §6).

Every path (reports, history, rerun, feedback, traces, metrics) routes its
authorization decision through this module so the access model lives in one
place. Pure function: no DB, no request — the caller passes the decoded JWT dict,
the resource's owner_id/org_id, and a pre-computed is_platform_admin flag
(is_validated_admin does the DB re-validation at the call site).

TWO entry points over ONE rule set (_can): caller_can_read applies the
owner-across-orgs rule, caller_can_act does not. Reads may follow the author
out of the org they wrote the work in; actions may not, because leaving an org
has to end every ongoing power over it. Split 2026-08-22 — before it, a
learning-store write and a container execution were both gating on a predicate
named `read`.

Coexistence: a token minted before Phase 1b has no org_id claim. Such a caller
falls back to the exact pre-tenancy rule (owner_id == caller.user_id), so the
single existing org's behaviour is unchanged until every token has rotated.

Referenced by: auth/jwt_utils.py (reports, caller_can_read),
api/history_endpoints.py (caller_can_read), api/endpoints.py (rerun and
feedback, both caller_can_act), api/trace_endpoints.py,
api/workflow_metrics_endpoints.py.
Depends on: nothing (pure).
"""


def _can(
    caller: dict | None,
    owner_id: str | None,
    org_id: str | None,
    *,
    is_platform_admin: bool,
    owner_rule_survives_the_org: bool,
) -> bool:
    """The rule set. Reached only through caller_can_read / caller_can_act.

    Rules (first match wins):
    1. caller is None  -> allow (AUTH_ENFORCED off; permissive dev escape hatch).
    2. is_platform_admin -> allow (cross-org).
    3. READS ONLY (owner_rule_survives_the_org): owner_id is not None and
       owner_id == caller.user_id -> allow, whatever the org. An internal org
       move must not lock an author out of work they wrote: a user moved from
       org A to org B keeps read access to the runs they own in org A.
       caller_can_act switches this rule OFF — see its docstring for why.
    4. caller has no org_id claim (legacy token) -> allow iff owner_id == caller.user_id.
    5. org-aware: org_id must equal the caller's org_id; within that org, allow iff
       caller is org_admin OR owner_id == caller.user_id.

    Unattributed resources (owner_id None) fail closed for every non-platform
    caller — including a same-org org_admin and rule 3 above (which requires
    owner_id is not None). /reports exposes typed credentials, so a row no
    user owns (legacy/unknown run) is readable only by a platform admin
    (rule 2), never granted by the org-admin shortcut.
    """
    if caller is None:
        return True
    if is_platform_admin:
        return True

    caller_org = caller.get("org_id")
    caller_uid = caller.get("user_id")

    if owner_rule_survives_the_org and owner_id is not None and owner_id == caller_uid:
        return True

    if caller_org is None:  # legacy token, coexistence fallback
        return owner_id is not None and owner_id == caller_uid

    if org_id is None or org_id != caller_org:
        return False
    # Fail closed before the org-admin shortcut: an unattributed row is
    # platform-admin-only, not visible to the tenant's own admin.
    if owner_id is None:
        return False
    if caller.get("org_role") == "org_admin":
        return True
    return owner_id == caller_uid


def caller_can_read(
    caller: dict | None,
    owner_id: str | None,
    org_id: str | None,
    *,
    is_platform_admin: bool,
) -> bool:
    """May `caller` READ a resource owned by `owner_id` within `org_id`?

    Rule 3 applies: an author keeps their own work whatever org they are in
    now. That is deliberate, and it discloses nothing new — the sensitive
    content of a run is the credentials the author themselves typed into the
    script, which a revocation cannot un-know. Revoking the read would cost a
    real capability (their own history and reports after an org move) and buy
    no secrecy.
    """
    return _can(caller, owner_id, org_id, is_platform_admin=is_platform_admin,
                owner_rule_survives_the_org=True)


def caller_can_act(
    caller: dict | None,
    owner_id: str | None,
    org_id: str | None,
    *,
    is_platform_admin: bool,
) -> bool:
    """May `caller` ACT on a resource owned by `owner_id` within `org_id`?

    Same rules as caller_can_read MINUS the owner-across-orgs rule, so leaving
    an org ends every ongoing power over it. Nothing in a token distinguishes
    an internal move from an offboarding — remove_member drops the user into a
    fresh personal org exactly as a move does — so rule 3 did not grant "an
    author who moved", it granted "anyone who ever authored a run in this org,
    forever". Two sites need this and reads do not:

      * POST /api/feedback mutates the org's learning store. Conflict
        detection fires with org_id=record.org_id — the RUN's org, never the
        caller's — so an ex-member kept shaping the hints injected into that
        org's future generations.
      * A re-run executes a container against the org's environment, using the
        credentials embedded in the stored script.

    Both were gating on a predicate named `read`; that was the defect, not the
    width of rule 3. Do NOT route a read through this function — an author
    losing their own history on an org move is the case rule 3 exists for.
    """
    return _can(caller, owner_id, org_id, is_platform_admin=is_platform_admin,
                owner_rule_survives_the_org=False)


def is_dashboard_viewer(caller: dict | None, *, is_platform_admin: bool) -> bool:
    """May `caller` view an org-level aggregate dashboard (traces/metrics)?

    True for: platform-admin (all orgs), the AUTH_ENFORCED-off escape hatch
    (caller None), or an org-admin (their own org). Plain org-members get no
    aggregate dashboard — they see only their own runs via caller_can_read.

    An org_admin claim without a resolvable org_id is rejected: callers derive
    the dashboard's org scope from caller["org_id"], so a None org_id would
    silently widen the query to every org. Fail closed at the gate instead.
    """
    if caller is None or is_platform_admin:
        return True
    return caller.get("org_role") == "org_admin" and caller.get("org_id") is not None
