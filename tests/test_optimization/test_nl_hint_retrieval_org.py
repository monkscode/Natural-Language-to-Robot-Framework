"""NL hint retrieval never crosses an org.

Until v20 a hint could carry `is_shared=1` and both retrieval filters had an
`OR is_shared = 1` branch, so one row reached every org. That flag was read on
retrieval ONLY: `get_hints_by_id`, `apply_hint_attribution`,
`_flag_hints_no_commit`, `_maybe_auto_disable_or_retire` and `_auto_disable_hint`
are all bare `WHERE id = ?`. A shared hint injected into any org's run therefore
had its counters, its `conflict_flagged` and its `is_active` mutated with no org
scoping — one tenant's outcomes deciding another tenant's hint lifecycle. The
column is gone; these tests pin that nothing brings the behaviour back.
"""

import pytest

pytestmark = pytest.mark.integration


def test_hints_do_not_cross_orgs(em_vec, seed_hint):
    from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine
    engine = NLFeedbackEngine(execution_memory=em_vec)
    seed_hint(text="org A private locator", domain="shared.test",
              anchor="login as admin", org_id="org-A")
    _, ids_b = engine.get_hints_with_ids(
        "login as admin", "https://shared.test/login", "assembler", org_id="org-B")
    assert ids_b == [], "org B received org A's private hint"
    _, ids_a = engine.get_hints_with_ids(
        "login as admin", "https://shared.test/login", "assembler", org_id="org-A")
    assert ids_a, "org A should see its own hint"


def test_no_hint_reaches_a_second_org_on_the_injection_path(em_vec, seed_hint):
    """The case the removed flag used to permit: a hint an admin created for
    everyone. There is no longer any column, value or scope that produces it —
    identical guidance in two orgs is two rows (copy-on-promote's manual half).
    """
    from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine
    engine = NLFeedbackEngine(execution_memory=em_vec)
    hid = seed_hint(text="generic guidance for everyone", domain="shared.test",
                    anchor="login as admin", org_id="org-A")
    for other in ("org-B", "org-C"):
        _, ids = engine.get_hints_with_ids(
            "login as admin", "https://shared.test/login", "assembler",
            org_id=other)
        assert hid not in ids, f"{other} received org A's hint"


# ---------------------------------------------------------------------------
# get_active_hints_raw org gate (conflict-detection path, no similarity filter)
# ---------------------------------------------------------------------------

def test_get_active_hints_raw_does_not_cross_orgs(em_vec, seed_hint):
    """SQL org gate is the SOLE discriminator on this path — prove it holds."""
    from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine
    engine = NLFeedbackEngine(execution_memory=em_vec)
    seed_hint(text="org A private conflict hint", domain="shared.test",
              anchor="login as admin", org_id="org-A")
    rows_b = engine.get_active_hints_raw(
        domain="shared.test", url="https://shared.test/login", org_id="org-B")
    assert rows_b == [], "org B received org A's private hint via get_active_hints_raw"
    rows_a = engine.get_active_hints_raw(
        domain="shared.test", url="https://shared.test/login", org_id="org-A")
    assert rows_a, "org A should see its own hint via get_active_hints_raw"


def test_no_hint_reaches_a_second_org_on_the_trigger_path(em_vec, seed_hint):
    """Same removal, on the path that decides which hints an LLM may FLAG.

    This is where the missing mutation scoping bit hardest: everything the
    trigger goes on to write is keyed by hint id alone, so a hint another org
    could read here was a hint another org's run could flag and disable.
    """
    from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine
    engine = NLFeedbackEngine(execution_memory=em_vec)
    hid = seed_hint(text="generic guidance for everyone", domain="shared.test",
                    anchor="login as admin", org_id="org-A")
    rows_b = engine.get_active_hints_raw(
        domain="shared.test", url="https://shared.test/login", org_id="org-B")
    assert hid not in [r["id"] for r in rows_b]
