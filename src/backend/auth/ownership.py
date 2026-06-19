"""The one shared org-scoped ownership predicate (spec §6).

Every read path (reports, history, rerun, feedback, traces, metrics) routes its
authorization decision through caller_can_read so the access model lives in one
place. Pure function: no DB, no request — the caller passes the decoded JWT dict,
the resource's owner_id/org_id, and a pre-computed is_platform_admin flag
(is_validated_admin does the DB re-validation at the call site).

Coexistence: a token minted before Phase 1b has no org_id claim. Such a caller
falls back to the exact pre-tenancy rule (owner_id == caller.user_id), so the
single existing org's behaviour is unchanged until every token has rotated.

Referenced by: auth/jwt_utils.py (reports), api/history_endpoints.py,
api/endpoints.py (rerun, feedback), api/trace_endpoints.py,
api/workflow_metrics_endpoints.py.
Depends on: nothing (pure).
"""


def caller_can_read(
    caller: dict | None,
    owner_id: str | None,
    org_id: str | None,
    *,
    is_platform_admin: bool,
) -> bool:
    """True if `caller` may read a resource owned by `owner_id` within `org_id`.

    Rules (first match wins):
    1. caller is None  -> allow (AUTH_ENFORCED off; permissive dev escape hatch).
    2. is_platform_admin -> allow (cross-org).
    3. caller has no org_id claim (legacy token) -> allow iff owner_id == caller.user_id.
    4. org-aware: org_id must equal the caller's org_id; within that org, allow iff
       caller is org_admin OR owner_id == caller.user_id.
    Unattributed resources (owner_id None) are never allowed to non-admins.
    """
    if caller is None:
        return True
    if is_platform_admin:
        return True

    caller_org = caller.get("org_id")
    caller_uid = caller.get("user_id")

    if caller_org is None:  # legacy token, coexistence fallback
        return owner_id is not None and owner_id == caller_uid

    if org_id is None or org_id != caller_org:
        return False
    if caller.get("org_role") == "org_admin":
        return True
    return owner_id is not None and owner_id == caller_uid


def is_dashboard_viewer(caller: dict | None, *, is_platform_admin: bool) -> bool:
    """May `caller` view an org-level aggregate dashboard (traces/metrics)?

    True for: platform-admin (all orgs), the AUTH_ENFORCED-off escape hatch
    (caller None), or an org-admin (their own org). Plain org-members get no
    aggregate dashboard — they see only their own runs via caller_can_read.
    """
    if caller is None or is_platform_admin:
        return True
    return caller.get("org_role") == "org_admin"
