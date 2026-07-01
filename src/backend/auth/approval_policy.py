"""The single approver-authority predicate (spec §Extensibility seam).

Pure function mirroring auth/ownership.py: callers pass decoded claims, never a
DB handle. Today only the platform owner (role='admin') may approve. To delegate
later — e.g. let a team's org_admin approve their own members when a per-org flag
is set — widen THIS function; no schema change, no call-site change.

Referenced by: auth/admin_access_endpoints.py.
Depends on: nothing (pure).
"""


def can_approve(
    approver: dict,
    target_user: dict | None = None,
    target_org: dict | None = None,
) -> bool:
    """True if `approver` may approve/reject/suspend `target_user`. Today: the
    platform owner only. target_user/target_org are accepted now so the signature
    is stable when delegation rules start reading them."""
    return approver.get("role") == "admin"
