"""Unit tests for the org-scoped ownership predicate. No DB, no Postgres."""

from src.backend.auth.ownership import caller_can_read

_OWNER = {"user_id": "u-owner", "org_id": "org-A", "org_role": "org_member"}
_ADMIN_OF_A = {"user_id": "u-admin", "org_id": "org-A", "org_role": "org_admin"}
_OTHER_ORG = {"user_id": "u-x", "org_id": "org-B", "org_role": "org_admin"}
_LEGACY = {"user_id": "u-owner", "org_id": None, "org_role": None}  # pre-tenancy token
# Differs from _OWNER in ORG ONLY (same user_id) — isolates the org-move rule
# from the identity dimension the cross-org tests above already cover.
_OWNER_MOVED = {"user_id": "u-owner", "org_id": "org-B", "org_role": "org_member"}


def test_none_caller_allowed_dev_escape_hatch():
    # AUTH_ENFORCED off -> require_user yields None -> permissive.
    assert caller_can_read(None, "u-owner", "org-A", is_platform_admin=False) is True


def test_platform_admin_sees_any_org():
    assert caller_can_read(_OTHER_ORG, "u-owner", "org-A", is_platform_admin=True) is True


def test_owner_in_same_org_allowed():
    assert caller_can_read(_OWNER, "u-owner", "org-A", is_platform_admin=False) is True


def test_org_admin_sees_other_members_in_same_org():
    assert caller_can_read(_ADMIN_OF_A, "u-owner", "org-A", is_platform_admin=False) is True


def test_member_cannot_see_other_members_run():
    other = {"user_id": "u-other", "org_id": "org-A", "org_role": "org_member"}
    assert caller_can_read(other, "u-owner", "org-A", is_platform_admin=False) is False


def test_different_org_denied_even_for_org_admin():
    assert caller_can_read(_OTHER_ORG, "u-owner", "org-A", is_platform_admin=False) is False


def test_legacy_token_falls_back_to_bare_owner_check():
    # No org claim (token minted before this phase) -> exact pre-tenancy behaviour.
    assert caller_can_read(_LEGACY, "u-owner", "org-A", is_platform_admin=False) is True
    assert caller_can_read(_LEGACY, "u-someone-else", "org-A", is_platform_admin=False) is False


def test_unattributed_resource_denied_to_non_admin():
    # owner_id None (legacy/unknown run): fail closed for non-platform-admin.
    assert caller_can_read(_OWNER, None, None, is_platform_admin=False) is False
    assert caller_can_read(_OWNER, None, "org-A", is_platform_admin=False) is False


def test_owner_can_read_own_run_after_moving_orgs():
    # F9: an internal org move must not lock an author out of work they
    # wrote. _OWNER_MOVED is the SAME user_id as the run's owner but sits in
    # a DIFFERENT org than the run (org-B caller, org-A run) — varying only
    # the org axis, so this genuinely isolates the new rule.
    assert caller_can_read(_OWNER_MOVED, "u-owner", "org-A", is_platform_admin=False) is True


def test_owner_moved_rule_guarded_against_unattributed_row():
    # The owner-keeps-access rule must require owner_id is not None, or an
    # unattributed row (owner_id None) would match a caller whose own
    # user_id claim is also None/missing — flipping the fail-closed
    # guarantee for unattributed rows.
    no_uid_caller = {"user_id": None, "org_id": "org-B", "org_role": "org_member"}
    assert caller_can_read(no_uid_caller, None, "org-B", is_platform_admin=False) is False


def test_unattributed_resource_denied_even_to_same_org_admin():
    # An org_admin sees attributed rows of other members, but an unattributed
    # row (owner_id None) in their own org stays platform-admin-only — /reports
    # exposes typed credentials, so the org-admin shortcut must not grant it.
    assert caller_can_read(_ADMIN_OF_A, None, "org-A", is_platform_admin=False) is False
    assert caller_can_read(_ADMIN_OF_A, None, "org-A", is_platform_admin=True) is True


# ---------------------------------------------------------------------------
# caller_can_act — the same predicate WITHOUT the owner-across-orgs rule
# ---------------------------------------------------------------------------


def test_act_refuses_an_owner_who_left_the_org():
    """An ex-member may re-READ work they wrote; they may not ACT on the org.

    Rule 3 (owner_id == caller) exists so an internal org move does not lock an
    author out of their own history. Nothing in a token distinguishes a move
    from a removal, so the same rule handed a removed member two ongoing
    powers they do not otherwise have: POST /api/feedback mutates the org's
    learning store, and a re-run executes a container against the org's
    environment. caller_can_act is caller_can_read without rule 3.
    """
    from src.backend.auth.ownership import caller_can_act
    assert caller_can_read(_OWNER_MOVED, "u-owner", "org-A", is_platform_admin=False) is True
    assert caller_can_act(_OWNER_MOVED, "u-owner", "org-A", is_platform_admin=False) is False


def test_act_still_allows_the_owner_inside_their_own_org():
    from src.backend.auth.ownership import caller_can_act
    assert caller_can_act(_OWNER, "u-owner", "org-A", is_platform_admin=False) is True


def test_act_still_allows_a_platform_admin_and_the_dev_escape_hatch():
    from src.backend.auth.ownership import caller_can_act
    assert caller_can_act(_OTHER_ORG, "u-owner", "org-A", is_platform_admin=True) is True
    assert caller_can_act(None, "u-owner", "org-A", is_platform_admin=False) is True


def test_act_still_allows_a_same_org_admin():
    from src.backend.auth.ownership import caller_can_act
    assert caller_can_act(_ADMIN_OF_A, "u-owner", "org-A", is_platform_admin=False) is True


def test_act_keeps_the_legacy_coexistence_fallback():
    """A token with no org claim still falls back to the bare owner check.

    This is the one branch where caller_can_act still grants on identity
    alone, so it is worth pinning what makes it safe: an ex-member cannot
    reach it. remove_member bumps token_version
    (admin_access_endpoints.py, remove_org_member), require_user re-validates
    it on every request (jwt_utils._revalidate_active_user), and the forced
    re-login self-heals a personal org before minting
    (auth/endpoints.py, _token_payload) — so a removed member always arrives
    with a concrete org_id and is refused on the org mismatch above. If a
    future change drops that self-heal, this test is where to come back to.
    """
    from src.backend.auth.ownership import caller_can_act
    assert caller_can_act(_LEGACY, "u-owner", "org-A", is_platform_admin=False) is True
    assert caller_can_act(_LEGACY, "u-someone-else", "org-A", is_platform_admin=False) is False


def test_act_keeps_an_unattributed_row_platform_admin_only():
    """Dropping rule 3 must not disturb the fail-closed guarantee."""
    from src.backend.auth.ownership import caller_can_act
    assert caller_can_act(_OWNER, None, "org-A", is_platform_admin=False) is False
    assert caller_can_act(_ADMIN_OF_A, None, "org-A", is_platform_admin=False) is False
    assert caller_can_act(_ADMIN_OF_A, None, "org-A", is_platform_admin=True) is True


# ---------------------------------------------------------------------------
# is_dashboard_viewer — dashboard gate (Task 9)
# ---------------------------------------------------------------------------

from src.backend.auth.ownership import is_dashboard_viewer  # noqa: E402


def test_dashboard_viewer_platform_admin():
    assert is_dashboard_viewer({"org_role": "org_member"}, is_platform_admin=True) is True


def test_dashboard_viewer_org_admin():
    assert is_dashboard_viewer(
        {"org_role": "org_admin", "org_id": "org-A"}, is_platform_admin=False
    ) is True


def test_dashboard_viewer_org_admin_without_org_id_denied():
    # Fail closed: an org_admin claim with no resolvable org_id would let callers
    # derive scope_org=None and run unscoped/all-org dashboard queries.
    assert is_dashboard_viewer(
        {"org_role": "org_admin", "org_id": None}, is_platform_admin=False
    ) is False
    assert is_dashboard_viewer(
        {"org_role": "org_admin"}, is_platform_admin=False
    ) is False


def test_dashboard_viewer_member_denied():
    assert is_dashboard_viewer(
        {"org_role": "org_member", "org_id": "org-A"}, is_platform_admin=False
    ) is False


def test_dashboard_viewer_none_caller_allowed():
    assert is_dashboard_viewer(None, is_platform_admin=False) is True
