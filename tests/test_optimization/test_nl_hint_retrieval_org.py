"""NL hint retrieval is org-scoped by default; is_shared hints cross orgs."""

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


def test_shared_hint_visible_to_other_org(em_vec, seed_hint):
    from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine
    engine = NLFeedbackEngine(execution_memory=em_vec)
    seed_hint(text="globally promoted locator", domain="shared.test",
              anchor="login as admin", org_id="org-A", is_shared=1)
    _, ids_b = engine.get_hints_with_ids(
        "login as admin", "https://shared.test/login", "assembler", org_id="org-B")
    assert ids_b, "promoted (is_shared) hint should be visible cross-org"
