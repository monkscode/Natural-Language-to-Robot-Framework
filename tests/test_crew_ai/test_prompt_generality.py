"""Prompt-generality pins for the search-Enter rule (2026-07-16 review).

The rule was stated TWICE (SEARCH_OPTIMIZATION_RULES + PLANNING_OUTPUT_RULES
old rule 7) as an absolute — "This applies to ALL websites" — which directly
contradicted the explicit-elements-only contract: a user who explicitly asks
to click the search button would have been overridden. It is now a DEFAULT
with a user-intent exception, stated once, with no brand names in the rule
text (worked examples elsewhere keep their concrete sites — examples are not
the genericity problem, rules are).

NOTE: behavioral planner-prompt change — gated on its own bench run before
merging (bench discipline), like every planner behavior change.
"""

from unittest.mock import MagicMock, patch

from src.backend.crew_ai.prompts.components import PromptComponents
from src.backend.crew_ai.library_context import BrowserLibraryContext


def _capture_plan_description(hint_context=None):
    from src.backend.crew_ai.tasks import RobotTasks
    tasks = RobotTasks(library_context=BrowserLibraryContext(),
                       hint_context=hint_context)
    with patch("src.backend.crew_ai.tasks.Task") as mock_task:
        tasks.plan_steps_task(agent=MagicMock(), query="click the button")
    return mock_task.call_args.kwargs["description"]


class TestSearchEnterRule:

    def test_enter_is_a_default_with_user_intent_exception(self):
        """User's explicit instructions must win over the Enter heuristic."""
        rules = PromptComponents.SEARCH_OPTIMIZATION_RULES
        assert "DEFAULT" in rules
        assert "explicitly asks to click" in rules

    def test_no_absolute_all_websites_claim(self):
        desc = _capture_plan_description()
        assert "applies to ALL websites" not in desc

    def test_rule_not_duplicated_in_output_rules(self):
        """The near-verbatim restatement (old rule 7) is gone — the rule
        lives once, in SEARCH_OPTIMIZATION_RULES."""
        assert "Press Keys" not in PromptComponents.PLANNING_OUTPUT_RULES

    def test_popup_rule_survives_renumbering(self):
        """Removing old rule 7 renumbers the list — the MOST CRITICAL
        popup/explicit-only output rule must survive."""
        assert ("DO NOT add popup dismissal"
                in PromptComponents.PLANNING_OUTPUT_RULES)

    def test_search_rule_carries_no_brand_names(self):
        """Rules stay generic; concrete sites belong to worked examples
        only (EXPLICIT_ELEMENTS_ONLY_RULES keeps its Flipkart example)."""
        rules = PromptComponents.SEARCH_OPTIMIZATION_RULES.lower()
        for brand in ("flipkart", "amazon", "google"):
            assert brand not in rules

    def test_web_search_default_url_rule_stays(self):
        """Unchanged neighbor: the no-URL web-search fallback (output rule 5)
        is a different rule and stays."""
        assert "https://www.google.com" in PromptComponents.PLANNING_OUTPUT_RULES

    def test_exception_reaches_composed_description(self):
        desc = _capture_plan_description()
        assert "explicitly asks to click" in desc
