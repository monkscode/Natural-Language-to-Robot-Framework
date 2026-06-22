"""SmartKeywordProvider only retrieves hints for the org it was built with.

Task 9: proves that org_id threading from __init__ through _get_learning_hints
into the NL and anti-pattern engine calls prevents cross-org hint leakage.
"""

import pytest
from unittest.mock import MagicMock

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Minimal stubs  — read SmartKeywordProvider.__init__ for the full parameter
# list; these satisfy every attribute access made during get_agent_context().
# ---------------------------------------------------------------------------

def _stub_library_context():
    ctx = MagicMock()
    ctx.library_name = "SeleniumLibrary"
    ctx.core_rules = "# Core rules stub"
    ctx.planning_context = "# Planning context stub"
    ctx.code_assembly_context = "# Assembly context stub"
    return ctx


def _stub_pattern_matcher():
    pm = MagicMock()
    pm.get_relevant_keywords.return_value = []
    return pm


def _stub_vector_store():
    vs = MagicMock()
    vs.search.return_value = []
    return vs


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def test_provider_scopes_nl_hints_to_its_org(em_vec, seed_hint):
    """A provider built for org-B must not receive org-A's private NL hint.

    The hint text "org A only" is seeded for org-A with an anchor that exactly
    matches the query, so it WOULD be retrieved absent org scoping.  If
    org_id is properly threaded into get_hints_with_ids, org-B's provider
    returns an empty hint block and the assertion passes.
    """
    seed_hint(
        text="org A only",
        domain="shared.test",
        anchor="login as admin",
        org_id="org-A",
    )

    from src.backend.crew_ai.optimization.smart_keyword_provider import SmartKeywordProvider

    provider = SmartKeywordProvider(
        library_context=_stub_library_context(),
        pattern_matcher=_stub_pattern_matcher(),
        vector_store=_stub_vector_store(),
        execution_memory=em_vec,
        org_id="org-B",
    )

    bundle = provider.get_agent_context(
        "login as admin", "assembler", url="https://shared.test/login"
    )

    # hint_text holds the formatted Tier-0 hint block; context holds Tier 1-3.
    # "org A only" must not appear in either when the provider is built for org-B.
    combined = bundle.hint_text + bundle.context
    assert "org A only" not in combined, (
        f"org-B provider leaked org-A hint. hint_text={bundle.hint_text!r}"
    )
