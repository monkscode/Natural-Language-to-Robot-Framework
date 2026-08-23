"""Shared run-history scope for the History page's two data sources.

/api/history (the table) and /api/groups (the folder chips) must compute the
SAME run filter for a given caller, or the chips count a different set of runs
than the table lists — an org_admin once saw "1" beside a table of 2. This
module owns that computation once so both agree by construction.

What it returns is a FILTER, not an identity. The filter user_id is None for a
validated platform admin, for an org_admin inside a concrete org (their view
spans the org, with no narrowing inside it), and for the token-less dev caller
that require_user yields with AUTH_ENFORCED off. None of those mean "no user".
Folder authorship (created_by) and the per-row can_move flag read
caller_user_id instead, which is None only when there really is no token.
Wiring them from the filter field is how an org_admin ends up creating folders
owned by NULL, and how their own runs stop being movable.

An org_admin claim counts only ALONGSIDE a concrete org_id. Widening on the
claim alone would hand a token carrying org_id null the (user_id=None,
org_id=None) scope, which the registry reads as every user in every org.

is_validated_admin re-reads the users table on every call, so the flag is
    computed once here and carried on the result: all four call sites need the
    scope and the flag together, and this keeps that at one DB round-trip.

is_org_admin rides along for the same reason. It is NOT derivable from the
filter fields — a platform admin and an org_admin both carry user_id None —
and /api/history needs it per row to answer "may this caller file this run",
a narrower question than "may they see it" ever since a folder became what
publishes a run.

Referenced by: api/history_endpoints.py, api/groups_endpoints.py,
api/endpoints.py.
Depends on: auth/jwt_utils.py (is_validated_admin).
"""

from typing import NamedTuple

from ..auth.jwt_utils import is_validated_admin


class HistoryScope(NamedTuple):
    """The run filter a caller's History reads apply, plus their identity.

    user_id / org_id are the FILTER (None = no narrowing on that dimension).
    user_id doubles as the caller's identity inside the registry's shared run
    predicate — mine, or published to my org — so it is None only for a
    caller who really does see everything. caller_user_id is the caller's
    IDENTITY unconditionally; never use the filter user_id where an identity
    is meant. is_admin is the re-validated platform-admin flag the scope was
    derived from, is_org_admin the org-level one — the per-row can_move flag
    needs it and cannot re-derive it, since a platform admin and an org_admin
    both carry user_id None. folder_org_id is the caller's REAL org,
    which is a third thing again: a platform admin's runs span every org, but
    their folders do not. Binding the filter org_id into the folder join gave
    them every org's folder names, private ones included, while every
    mutation binds this value and answered 404 for the same folders.
    """

    user_id: str | None
    org_id: str | None
    caller_user_id: str | None
    is_admin: bool
    folder_org_id: str | None
    is_org_admin: bool


def history_scope(user: dict | None) -> HistoryScope:
    """The run scope for this caller.

    Platform admin (re-validated) and the token-less dev caller are unscoped:
    every org, every user. An org_admin with a concrete org gets their whole
    org with no per-user narrowing. Everyone else gets their own rows within
    their own org. folder_org_id is always the caller's own org, whoever they
    are, and it is None in two DIFFERENT cases that the registry reads apart
    using caller_user_id: no token at all — the token-less dev path, which
    keeps its historic unfiltered folder join — and a token that carries no
    org, for which NO folder resolves, so that caller sees their own runs and
    no folder names at all (ownership.caller_can_access rule 4). Login makes
    the second case unreachable in practice: _token_payload provisions an org
    for any ACTIVE user who lacks one before it mints a token.
    """
    admin = is_validated_admin(user)
    caller_user_id = None if user is None else user["user_id"]
    folder_org_id = None if user is None else user.get("org_id")
    # An org_admin claim counts only ALONGSIDE a concrete org, wherever it is
    # read — the widening below and the per-row can_move flag both depend on
    # it, and a token carrying org_id null must reach neither.
    is_org_admin = (user is not None and bool(folder_org_id)
                    and user.get("org_role") == "org_admin")
    if admin or user is None:
        return HistoryScope(None, None, caller_user_id, admin, folder_org_id,
                            is_org_admin)
    return HistoryScope(
        None if is_org_admin else user["user_id"], folder_org_id,
        caller_user_id, admin, folder_org_id, is_org_admin,
    )
