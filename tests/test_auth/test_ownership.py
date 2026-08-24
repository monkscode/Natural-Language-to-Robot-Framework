"""Unit tests for the org-scoped ownership predicate. No DB, no Postgres."""

from src.backend.auth.ownership import caller_can_access

_OWNER = {"user_id": "u-owner", "org_id": "org-A", "org_role": "org_member"}
_ADMIN_OF_A = {"user_id": "u-admin", "org_id": "org-A", "org_role": "org_admin"}
_OTHER_ORG = {"user_id": "u-x", "org_id": "org-B", "org_role": "org_admin"}
_LEGACY = {"user_id": "u-owner", "org_id": None, "org_role": None}  # pre-tenancy token
# Differs from _OWNER in ORG ONLY (same user_id) — isolates the org-move rule
# from the identity dimension the cross-org tests above already cover.
_OWNER_MOVED = {"user_id": "u-owner", "org_id": "org-B", "org_role": "org_member"}


def test_none_caller_allowed_dev_escape_hatch():
    # AUTH_ENFORCED off -> require_user yields None -> permissive.
    assert caller_can_access(None, "u-owner", "org-A", is_platform_admin=False) is True


def test_platform_admin_sees_any_org():
    assert caller_can_access(_OTHER_ORG, "u-owner", "org-A", is_platform_admin=True) is True


def test_owner_in_same_org_allowed():
    assert caller_can_access(_OWNER, "u-owner", "org-A", is_platform_admin=False) is True


def test_org_admin_sees_other_members_in_same_org():
    assert caller_can_access(_ADMIN_OF_A, "u-owner", "org-A", is_platform_admin=False) is True


def test_member_cannot_see_other_members_run():
    other = {"user_id": "u-other", "org_id": "org-A", "org_role": "org_member"}
    assert caller_can_access(other, "u-owner", "org-A", is_platform_admin=False) is False


def test_different_org_denied_even_for_org_admin():
    assert caller_can_access(_OTHER_ORG, "u-owner", "org-A", is_platform_admin=False) is False


def test_legacy_token_falls_back_to_bare_owner_check():
    # No org claim (token minted before this phase) -> exact pre-tenancy behaviour.
    assert caller_can_access(_LEGACY, "u-owner", "org-A", is_platform_admin=False) is True
    assert caller_can_access(_LEGACY, "u-someone-else", "org-A", is_platform_admin=False) is False


def test_unattributed_resource_denied_to_non_admin():
    # owner_id None (legacy/unknown run): fail closed for non-platform-admin.
    assert caller_can_access(_OWNER, None, None, is_platform_admin=False) is False
    assert caller_can_access(_OWNER, None, "org-A", is_platform_admin=False) is False


def test_author_loses_their_own_run_after_leaving_the_org():
    """INVERTED 2026-08-23. There is no author-keeps-it rule any more: the
    org's data stays with the org.

    _OWNER_MOVED is the SAME user_id as the run's owner but sits in a
    DIFFERENT org than the run (org-B caller, org-A run), so this varies only
    the org axis. Nothing in a token distinguishes an internal transfer from
    an offboarding, so a rule keyed on authorship grants "anyone who ever
    authored anything here, forever" — which is what GitHub, Notion and Figma
    all decline to do. Rejoining org-A restores everything, because access is
    computed from the caller's CURRENT org.
    """
    assert caller_can_access(_OWNER_MOVED, "u-owner", "org-A",
                             is_platform_admin=False) is False


def test_a_published_run_reaches_a_peer_but_not_someone_outside_the_org():
    """The one rule that DOES cross the owner boundary — and it stops at the
    org edge, which is what keeps a leaver out."""
    peer = {"user_id": "u-peer", "org_id": "org-A", "org_role": "org_member"}
    assert caller_can_access(peer, "u-owner", "org-A",
                             is_platform_admin=False, is_grouped=True) is True
    # The author who left is now simply outside org-A, published or not.
    assert caller_can_access(_OWNER_MOVED, "u-owner", "org-A",
                             is_platform_admin=False, is_grouped=True) is False


def test_published_rule_guarded_against_unattributed_row():
    # is_grouped must require owner_id is not None, or an unattributed row
    # would become org-readable the moment somebody filed it into a folder —
    # flipping the fail-closed guarantee /reports depends on.
    peer = {"user_id": "u-peer", "org_id": "org-A", "org_role": "org_member"}
    assert caller_can_access(peer, None, "org-A",
                             is_platform_admin=False, is_grouped=True) is False


def test_unattributed_resource_denied_even_to_same_org_admin():
    # An org_admin sees attributed rows of other members, but an unattributed
    # row (owner_id None) in their own org stays platform-admin-only — /reports
    # exposes typed credentials, so the org-admin shortcut must not grant it.
    assert caller_can_access(_ADMIN_OF_A, None, "org-A", is_platform_admin=False) is False
    assert caller_can_access(_ADMIN_OF_A, None, "org-A", is_platform_admin=True) is True
