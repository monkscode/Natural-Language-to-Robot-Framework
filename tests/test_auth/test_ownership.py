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


# ---------------------------------------------------------------------------
# hint_mutation_verdict — who may change a hint
#
# Every hint mutation used to be gated on require_admin, the PLATFORM role,
# and was entirely org-blind: an admin could retract any org's hint and no one
# else could touch their own. Three tiers replace that — platform admin
# (anywhere), org admin (their own org), author (their own hints) — and the
# predicate returns THREE outcomes rather than a boolean, because the right
# refusal differs by case.
# ---------------------------------------------------------------------------

from src.backend.auth.ownership import hint_mutation_verdict

_ORG_A = "org-a"
_ORG_B = "org-b"
_AUTHOR = "user-1"
_OTHER = "user-2"


def _caller(org_id=_ORG_A, org_role="org_member", user_id=_OTHER):
    return {"org_id": org_id, "org_role": org_role, "user_id": user_id}


class TestHintMutationVerdict:

    def test_no_caller_is_allowed(self):
        """AUTH_ENFORCED off — the same permissive dev escape hatch every
        sibling predicate in this module has."""
        assert hint_mutation_verdict(
            None, _ORG_A, _AUTHOR, is_platform_admin=False) == "allow"

    def test_platform_admin_may_mutate_any_org(self):
        assert hint_mutation_verdict(
            _caller(org_id=_ORG_B), _ORG_A, _AUTHOR,
            is_platform_admin=True) == "allow"

    def test_org_admin_may_mutate_a_hint_in_their_own_org(self):
        assert hint_mutation_verdict(
            _caller(org_role="org_admin"), _ORG_A, _AUTHOR,
            is_platform_admin=False) == "allow"

    def test_the_author_may_mutate_their_own_hint_when_the_tier_applies(self):
        assert hint_mutation_verdict(
            _caller(user_id=_AUTHOR), _ORG_A, _AUTHOR,
            is_platform_admin=False, author_tier_applies=True) == "allow"

    def test_the_author_tier_is_off_unless_the_call_site_asks_for_it(self):
        """Default-deny on the last tier. The author surface this product
        builds is one control — Retract — so retract is the only route that
        passes author_tier_applies=True. patch could promote a url-scoped hint
        to 'global' (applied to every query in the org) and reactivate resets
        unused_count, defeating the never-used retirement; a hint-mutation
        route added later without thinking about tiers must land on the
        org-admin floor every mutation started from, not on author access."""
        assert hint_mutation_verdict(
            _caller(user_id=_AUTHOR), _ORG_A, _AUTHOR,
            is_platform_admin=False) == "forbidden"

    def test_the_org_admin_and_platform_tiers_ignore_the_author_flag(self):
        """Only the LAST tier is parametrised. An org admin's access to their
        own org's hints, and a platform admin's to any, are the org check the
        owner ruled on and are identical on all five routes."""
        assert hint_mutation_verdict(
            _caller(org_role="org_admin"), _ORG_A, _AUTHOR,
            is_platform_admin=False) == "allow"
        assert hint_mutation_verdict(
            _caller(org_id=_ORG_B), _ORG_A, _AUTHOR,
            is_platform_admin=True) == "allow"

    def test_a_non_author_member_in_the_same_org_is_forbidden(self):
        """403, not 404. They may well have read the hint's text in their own
        feedback panel — reinforcement requires byte-identical text, so a hint
        a user sees is one they typed themselves. Answering 404 there would be
        a lie about something they have seen."""
        assert hint_mutation_verdict(
            _caller(), _ORG_A, _AUTHOR, is_platform_admin=False) == "forbidden"

    def test_another_orgs_hint_is_not_found_not_forbidden(self):
        """404, because 403 confirms the hint exists. get_hint already 404s a
        cross-org read, so a 403 here would open an existence-leak asymmetry
        between reading and mutating."""
        assert hint_mutation_verdict(
            _caller(org_role="org_admin"), _ORG_B, _AUTHOR,
            is_platform_admin=False) == "not_found"

    def test_the_author_loses_access_after_leaving_the_org(self):
        """The hint belongs to the org it was written in. An author whose
        token now names a different org gets the cross-org answer, not an
        author exemption — the same rule caller_can_access settled on."""
        assert hint_mutation_verdict(
            _caller(org_id=_ORG_B, user_id=_AUTHOR), _ORG_A, _AUTHOR,
            is_platform_admin=False, author_tier_applies=True) == "not_found"

    def test_a_caller_with_no_org_fails_closed(self):
        """An org-less token means unscoped reads elsewhere; here it must mean
        no mutation at all, and the refusal must not confirm the hint."""
        assert hint_mutation_verdict(
            _caller(org_id=None), _ORG_A, _AUTHOR,
            is_platform_admin=False) == "not_found"

    def test_a_hint_with_no_org_is_unreachable(self):
        """Legacy rows can carry a NULL org_id. Nobody but a platform admin
        may mutate one — the org check must not be satisfied by two Nones."""
        assert hint_mutation_verdict(
            _caller(org_role="org_admin"), None, _AUTHOR,
            is_platform_admin=False) == "not_found"

    def test_an_unattributed_hint_is_not_everyones(self):
        """The author check must not match None against None. Without the
        truthiness guard, every org member with no user_id claim would be the
        author of every hint that has no author.

        Asserted with the author tier ON: with it off the answer is
        "forbidden" for a second, unrelated reason, and the truthiness guard
        would be untested."""
        assert hint_mutation_verdict(
            _caller(user_id=None), _ORG_A, None,
            is_platform_admin=False, author_tier_applies=True) == "forbidden"
