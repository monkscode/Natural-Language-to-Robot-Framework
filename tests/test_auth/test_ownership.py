"""Unit tests for the org-scoped ownership predicate. No DB, no Postgres."""

from src.backend.auth.ownership import caller_can_read

_OWNER = {"user_id": "u-owner", "org_id": "org-A", "org_role": "org_member"}
_ADMIN_OF_A = {"user_id": "u-admin", "org_id": "org-A", "org_role": "org_admin"}
_OTHER_ORG = {"user_id": "u-x", "org_id": "org-B", "org_role": "org_admin"}
_LEGACY = {"user_id": "u-owner", "org_id": None, "org_role": None}  # pre-tenancy token


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


def test_unattributed_resource_denied_even_to_same_org_admin():
    # An org_admin sees attributed rows of other members, but an unattributed
    # row (owner_id None) in their own org stays platform-admin-only — /reports
    # exposes typed credentials, so the org-admin shortcut must not grant it.
    assert caller_can_read(_ADMIN_OF_A, None, "org-A", is_platform_admin=False) is False
    assert caller_can_read(_ADMIN_OF_A, None, "org-A", is_platform_admin=True) is True


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
