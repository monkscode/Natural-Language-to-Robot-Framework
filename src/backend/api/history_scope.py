"""Shared run-history scope for the History page's two data sources.

/api/history (the table) and /api/groups (the folder chips) must compute the
SAME run filter for a given caller, or the chips count a different set of runs
than the table lists — an org_admin once saw "1" beside a table of 2. This
module owns that computation once so both agree by construction.

What it returns is a FILTER, not an identity. The filter user_id is None for a
validated platform admin, for an org_admin inside a concrete org (their view
spans the org, with no narrowing inside it), and for the token-less dev caller
that require_user yields with AUTH_ENFORCED off. None of those mean "no user".
Folder authorship, created_by, and the "is this private folder mine" test bound
into the registry's visibility join all read caller_user_id instead, which is
None only when there really is no token. Wiring them from the filter field is
how an org_admin ends up creating folders owned by NULL.

An org_admin claim counts only ALONGSIDE a concrete org_id. Widening on the
claim alone would hand a token carrying org_id null the (user_id=None,
org_id=None) scope, which the registry reads as every user in every org.

is_validated_admin re-reads the users table on every call, so the flag is
computed once here and carried on the result: all three call sites need the
scope and the flag together, and this keeps that at one DB round-trip.

Referenced by: api/history_endpoints.py, api/groups_endpoints.py.
Depends on: auth/jwt_utils.py (is_validated_admin).
"""

from typing import NamedTuple

from ..auth.jwt_utils import is_validated_admin


class HistoryScope(NamedTuple):
    """The run filter a caller's History reads apply, plus their identity.

    user_id / org_id are the FILTER (None = no narrowing on that dimension).
    caller_user_id is the caller's IDENTITY — never use the filter user_id
    where an identity is meant. is_admin is the re-validated platform-admin
    flag the scope was derived from.
    """

    user_id: str | None
    org_id: str | None
    caller_user_id: str | None
    is_admin: bool


def history_scope(user: dict | None) -> HistoryScope:
    """The run scope for this caller.

    Platform admin (re-validated) and the token-less dev caller are unscoped:
    every org, every user. An org_admin with a concrete org gets their whole
    org with no per-user narrowing. Everyone else gets their own rows within
    their own org.
    """
    admin = is_validated_admin(user)
    caller_user_id = None if user is None else user["user_id"]
    if admin or user is None:
        return HistoryScope(None, None, caller_user_id, admin)
    org_id = user.get("org_id")
    is_org_admin = bool(org_id) and user.get("org_role") == "org_admin"
    return HistoryScope(
        None if is_org_admin else user["user_id"], org_id, caller_user_id, admin
    )
