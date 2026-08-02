"""The planner prompt may only name keys the planner is allowed to emit.

The defect this pins: PLANNING_OUTPUT_RULES rule 6 injected a worked example
reading {"keyword": "New Browser", "browser": "chromium", "headless": "True"}.
`browser` and `headless` are not PlannedStep fields, and the response schema
forwarded to capable providers sets additionalProperties: false — so the model
was instructed to emit keys it had no legal way to emit. It improvised them
into `value` instead: 56 of 60 plans across two benches carried an improvised
launch-step value in 8 distinct encodings, including whole JSON blobs and
"browser=chromium, headless=True, url=https://...".

None of it ever reached the generated code — 1,512 of 1,513 captured
test.robot files emit the same templated line, which comes from
browser_context.code_assembly_context, not from the plan. The instruction was
unreachable by construction.

The class of bug is what matters: a prompt that names a key the schema
forbids cannot be satisfied, and the model's improvisation lands somewhere
undefined. This guard fails the moment a prompt does that again.
"""

import re
from unittest.mock import MagicMock, patch

from src.backend.crew_ai.library_context import BrowserLibraryContext
from src.backend.crew_ai.tasks import PlanOutput, PlannedStep


def _rendered_plan_description():
    from src.backend.crew_ai.tasks import RobotTasks
    tasks = RobotTasks(library_context=BrowserLibraryContext())
    with patch("src.backend.crew_ai.tasks.Task") as mock_task:
        tasks.plan_steps_task(agent=MagicMock(), query="click the button")
    return mock_task.call_args.kwargs["description"]


# A JSON object key as it appears in a prompt example: "some_key":
_JSON_KEY_RE = re.compile(r'"([a-z_][a-z0-9_]*)"\s*:')


class TestPlannerPromptNamesOnlyEmittableKeys:

    def test_every_json_key_in_the_prompt_is_a_real_output_field(self):
        description = _rendered_plan_description()
        emittable = set(PlannedStep.model_fields) | set(PlanOutput.model_fields)
        named = set(_JSON_KEY_RE.findall(description))
        assert named <= emittable, (
            f"planner prompt names keys the schema forbids: "
            f"{sorted(named - emittable)}"
        )

    def test_the_guard_can_actually_fail(self):
        """A guard that cannot fail pins nothing. This is the exact shape the
        deleted rule 6 example had."""
        named = set(_JSON_KEY_RE.findall(
            '- Example: {"keyword": "New Browser", "browser": "chromium"}'))
        emittable = set(PlannedStep.model_fields) | set(PlanOutput.model_fields)
        assert named - emittable == {"browser"}

    def test_the_prompt_still_names_every_field_the_planner_must_emit(self):
        """The complement. A conformance guard that only removes text would be
        satisfied by deleting the output contract altogether, so pin the other
        direction too: every PlannedStep field is still named to the model,
        whether as a JSON key, a quoted name (rule 2) or a backticked one
        (rule 4)."""
        description = _rendered_plan_description()
        named = set(re.findall(r'[\"`]([a-z_][a-z0-9_]*)[\"`]', description))
        missing = set(PlannedStep.model_fields) - named
        assert not missing, f"planner prompt no longer names: {sorted(missing)}"
        assert "steps" in named


class TestBrowserInitInstructionsAreGone:

    def test_no_browser_init_placeholder_survives_in_the_rendered_prompt(self):
        """A placeholder whose .replace() target was deleted would ship the
        literal token to the model."""
        assert "{browser_init_placeholder}" not in _rendered_plan_description()

    def test_the_popup_rule_survives_renumbering(self):
        """Deleting rule 6 renumbers the list. The MOST CRITICAL explicit-only
        rule must survive, as it did the last renumbering (old rule 7)."""
        from src.backend.crew_ai.prompts.components import PromptComponents
        assert ("DO NOT add popup dismissal"
                in PromptComponents.PLANNING_OUTPUT_RULES)

    def test_the_web_search_default_url_rule_survives_renumbering(self):
        from src.backend.crew_ai.prompts.components import PromptComponents
        assert "https://www.google.com" in PromptComponents.PLANNING_OUTPUT_RULES
