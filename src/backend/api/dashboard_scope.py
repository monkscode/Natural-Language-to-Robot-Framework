"""Shared authorization for org-level aggregate dashboards (traces, metrics).

These dashboards span every user in an org, so unlike the rest of the API they
do NOT honour the AUTH_ENFORCED-off escape hatch: a token-less caller is 401
even in dev, where require_user resolves anonymous to None instead of raising.
An authenticated caller must be a platform-admin (all orgs) or an org-admin
(their own org); a plain member is 403. Returning None means platform scope
(all orgs); a string means filter to that org_id.

Referenced by: api/workflow_metrics_endpoints.py, api/trace_endpoints.py.
Depends on: auth/jwt_utils.py (is_validated_admin), auth/ownership.py
            (is_dashboard_viewer).
"""

from typing import Optional

from fastapi import HTTPException

from ..auth.jwt_utils import is_validated_admin
from ..auth.ownership import is_dashboard_viewer


def authorize_dashboard_read(user: dict | None) -> Optional[str]:
    """Authorize an aggregate-dashboard read and return the org filter to apply.

    Raises 401 for a token-less caller (no escape hatch — these expose every
    user's data in an org) and 403 for an authenticated non-viewer. Returns
    None for platform scope (all orgs) or the caller's org_id.
    """
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    admin = is_validated_admin(user)
    if not is_dashboard_viewer(user, is_platform_admin=admin):
        raise HTTPException(status_code=403, detail="Org-admin access required")
    return None if admin else user.get("org_id")
