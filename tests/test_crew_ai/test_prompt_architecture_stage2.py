"""
TASK-24R Stage 2 target-state tests: deterministic prompt composer.

Evidence base (plan: docs/superpowers/plans/locator-enhancement/
TASK-24R-prompt-architecture.md §B/§C, findings verified against full prompt
dumps of the 30 official 2026-07-11 bench runs):
  - F5: irrelevant element-type blocks shipped every run — median 5,042
    tokens/run of rules for element types the run does not contain
    (DROPDOWN needed 5/30, STABILITY 6/30, LOOP 3/30, STATE_VERIF 1/30,
    CHECKBOX/FILE_UPLOAD/DATE_PICKER/CONDITIONAL 0/30).
  - F7: steps-JSON dead weight ~203 t/run — all_locators (10-field blob,
    always exactly 1 entry), empty-string fields, stability:"stable".

Design under test (plan §B):
  - Trigger table: 8 conditional blocks included only when a MERGED step's
    own routing fields need them — the same fields each block routes on.
  - Fail-open: ANY doubt or exception → include ALL blocks + ship the
    original steps JSON verbatim = today's prompt exactly.
  - Slim steps-JSON view is PROMPT-ONLY: merge_locators output untouched.
    Drop all_locators, ""/[]/None values, stability=="stable"; ALWAYS keep
    `found` (LOCATOR_RULES routes on it).
  - Steps JSON placed LAST in the description (dynamic tail).
  - One INFO log line per run listing included blocks (Task 25 correlation).
"""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from src.backend.crew_ai.prompts.components import PromptComponents
from src.backend.crew_ai.library_context import BrowserLibraryContext
from src.backend.crew_ai.tasks import (
    RobotTasks,
    select_conditional_blocks,
    slim_steps_view,
)


# Today's description order for the 8 conditional blocks — the fail-open
# path must reproduce it exactly.
ALL_CONDITIONAL_BLOCKS = [
    "STABILITY_WARNING_RULES",
    "CONDITIONAL_LOGIC_HANDLING",
    "LOOP_HANDLING",
    "DROPDOWN_HANDLING",
    "CHECKBOX_RADIO_HANDLING",
    "FILE_UPLOAD_HANDLING",
    "DATE_PICKER_HANDLING",
    "STATE_VERIFICATION_HANDLING",
]


def _block_header(name: str) -> str:
    """First content line of a component — a unique presence signature."""
    return getattr(PromptComponents, name).strip().splitlines()[0]


def _simple_steps():
    """A realistic simple run (navigate → fill → press keys → read text),
    merged-steps shape (element_identification.merge_locators output):
    no dropdowns, loops, conditions, state checks, or unstable locators."""
    return [
        {
            "step_description": "Open the browser and navigate to the site",
            "keyword": "New Page",
            "value": "https://example.com",
        },
        {
            "step_description": "Fill the search bar with shoes",
            "element_description": "search bar",
            "value": "shoes",
            "keyword": "Fill Text",
            "locator": "id=search",
            "found": True,
            "element_type": "input",
            "dropdown_framework": "",
            "datepicker_framework": "",
            "element_classes": "form-input",
            "aria_invalid": "",
            "parent_classes": "",
            "stability": "stable",
            "all_locators": [
                {
                    "locator": "id=search",
                    "strategy": "id",
                    "score": 95,
                    "validated": True,
                }
            ],
        },
        {
            "step_description": "Press Enter to search",
            "keyword": "Press Keys",
            "value": "Enter",
        },
        {
            "step_description": "Get the first product name",
            "element_description": "first product name",
            "keyword": "Get Text",
            "locator": "css=.product-name >> nth=0",
            "found": True,
            "element_type": "a",
            "dropdown_framework": "",
            "datepicker_framework": "",
            "element_classes": "",
            "aria_invalid": "",
            "parent_classes": "",
            "stability": "stable",
            "all_locators": [
                {
                    "locator": "css=.product-name >> nth=0",
                    "strategy": "css",
                    "score": 80,
                    "validated": True,
                }
            ],
        },
    ]


def _step_with(**overrides):
    """One found element step with trigger-relevant overrides applied."""
    step = {
        "step_description": "interact with the element",
        "element_description": "the element",
        "keyword": "Click",
        "locator": "id=target",
        "found": True,
        "element_type": "button",
        "dropdown_framework": "",
        "datepicker_framework": "",
        "stability": "stable",
    }
    step.update(overrides)
    return step


def _capture_description(steps_json: str, hint_context=None) -> str:
    tasks = RobotTasks(library_context=BrowserLibraryContext(),
                       hint_context=hint_context)
    with patch("src.backend.crew_ai.tasks.Task") as mock_task:
        tasks.assemble_code_task(agent=MagicMock(),
                                 identified_steps_json=steps_json)
    return mock_task.call_args.kwargs["description"]


# ═══════════════════════════════════════════════════════════════════════════
# Trigger table — per-trigger selection (plan §B table)
# ═══════════════════════════════════════════════════════════════════════════

class TestTriggerTable:

    def test_simple_steps_need_no_conditional_blocks(self):
        assert select_conditional_blocks(_simple_steps()) == []

    @pytest.mark.parametrize("overrides,expected", [
        # DROPDOWN: element_type ∈ {select, dropdown} OR dropdown_framework
        # truthy OR a select-family keyword (planner phrasing drifts —
        # element_identification._ACTION_EXACT accepts the whole family, so
        # the trigger must too; checkbox selects are click-family, excluded)
        ({"element_type": "select"}, "DROPDOWN_HANDLING"),
        ({"element_type": "dropdown"}, "DROPDOWN_HANDLING"),
        ({"dropdown_framework": "tom-select"}, "DROPDOWN_HANDLING"),
        ({"keyword": "Select Options By"}, "DROPDOWN_HANDLING"),
        ({"keyword": "Select Options By Label"}, "DROPDOWN_HANDLING"),
        ({"keyword": "Select From List By Value"}, "DROPDOWN_HANDLING"),
        # CHECKBOX_RADIO: element_type ∈ {radio, checkbox}
        ({"element_type": "radio"}, "CHECKBOX_RADIO_HANDLING"),
        ({"element_type": "checkbox"}, "CHECKBOX_RADIO_HANDLING"),
        # FILE_UPLOAD: element_type == "file-upload"
        ({"element_type": "file-upload"}, "FILE_UPLOAD_HANDLING"),
        # DATE_PICKER: element_type == "date-picker" OR datepicker_framework
        ({"element_type": "date-picker"}, "DATE_PICKER_HANDLING"),
        ({"datepicker_framework": "flatpickr"}, "DATE_PICKER_HANDLING"),
        # STATE_VERIFICATION: keyword ∈ {"Get Classes", "Get Attribute"}
        ({"keyword": "Get Classes"}, "STATE_VERIFICATION_HANDLING"),
        ({"keyword": "Get Attribute"}, "STATE_VERIFICATION_HANDLING"),
        # LOOP: loop_type truthy OR keyword == "Get Elements"
        ({"loop_type": "FOR", "loop_source": "id=items"}, "LOOP_HANDLING"),
        ({"keyword": "Get Elements"}, "LOOP_HANDLING"),
        # CONDITIONAL: condition_type or condition_value truthy
        ({"condition_type": "IF"}, "CONDITIONAL_LOGIC_HANDLING"),
        ({"condition_value": "${total} > 100"}, "CONDITIONAL_LOGIC_HANDLING"),
        # STABILITY: any stability ∉ {None, "stable"}
        ({"stability": "positional"}, "STABILITY_WARNING_RULES"),
        ({"stability": "volatile"}, "STABILITY_WARNING_RULES"),
    ])
    def test_single_trigger_includes_exactly_its_block(self, overrides, expected):
        steps = _simple_steps() + [_step_with(**overrides)]
        assert select_conditional_blocks(steps) == [expected]

    def test_triggers_tolerate_case_drift(self):
        """element_type falls back to the DOM tagName when the classifier
        did not classify (merge_locators) — tagName is uppercase ('SELECT').
        Keyword casing can drift too. A missed trigger silently drops a
        NEEDED block, so matching must be case-insensitive."""
        assert select_conditional_blocks(
            [_step_with(element_type="SELECT")]) == ["DROPDOWN_HANDLING"]
        assert select_conditional_blocks(
            [_step_with(keyword="get classes")]) == ["STATE_VERIFICATION_HANDLING"]

    def test_multiple_triggers_preserve_description_order(self):
        """Included blocks keep today's description order regardless of
        which step triggered them."""
        steps = [
            _step_with(keyword="Get Classes"),
            _step_with(element_type="select"),
            _step_with(stability="positional"),
        ]
        assert select_conditional_blocks(steps) == [
            "STABILITY_WARNING_RULES",
            "DROPDOWN_HANDLING",
            "STATE_VERIFICATION_HANDLING",
        ]

    def test_select_family_keyword_triggers_dropdown_on_found_false(self):
        """The real defect scenario: locator not found → no element_type from
        the service, so the KEYWORD is the only dropdown signal left. An exact
        match on 'select options by' missed the rest of the family and
        silently dropped the NEEDED block."""
        steps = [{
            "step_description": "pick the timezone",
            "element_description": "timezone dropdown",
            "keyword": "Select From List By Label",
            "value": "label    Asia/Kolkata",
            "found": False,
        }]
        assert select_conditional_blocks(steps) == ["DROPDOWN_HANDLING"]

    def test_select_checkbox_keywords_do_not_trigger_dropdown(self):
        """'Select Checkbox' / 'Unselect Checkbox' are click-family
        (element_identification._ACTION_EXACT) — shipping the ~1.9k-token
        dropdown block for them is exactly the waste the composer removes."""
        steps = [_step_with(keyword="Select Checkbox", element_type="checkbox")]
        assert select_conditional_blocks(steps) == ["CHECKBOX_RADIO_HANDLING"]
        steps = [_step_with(keyword="Unselect Checkbox", element_type="checkbox")]
        assert select_conditional_blocks(steps) == ["CHECKBOX_RADIO_HANDLING"]

    @pytest.mark.parametrize("keyword,expected", [
        ("Check Checkbox", "CHECKBOX_RADIO_HANDLING"),
        ("Uncheck Checkbox", "CHECKBOX_RADIO_HANDLING"),
        ("Select Checkbox", "CHECKBOX_RADIO_HANDLING"),
        ("Unselect Checkbox", "CHECKBOX_RADIO_HANDLING"),
        ("Upload File By Selector", "FILE_UPLOAD_HANDLING"),
    ])
    def test_keyword_only_signal_triggers_block_on_found_false(
            self, keyword, expected):
        """Same defect class as the select family: with found:false there is
        no element_type from the service, so the KEYWORD is the only signal
        left. Without this the assembler loses force=True (checkbox) and the
        never-Click rule (upload) on exactly the steps a human must repair.

        Date pickers are deliberately NOT covered — the planner emits plain
        'Fill Text' for them, so no keyword signal exists to trigger on.
        """
        steps = [{
            "step_description": "s",
            "element_description": "the control",
            "keyword": keyword,
            "found": False,
        }]
        assert select_conditional_blocks(steps) == [expected]

    def test_loop_source_without_loop_type_triggers_loop(self):
        """_needs_conditional accepts EITHER condition key; the loop trigger
        must be symmetric. LOOP_HANDLING routes on loop_type AND loop_source,
        so a step carrying only loop_source still needs the block."""
        steps = [_step_with(loop_source="the main menu")]
        assert select_conditional_blocks(steps) == ["LOOP_HANDLING"]

    def test_found_false_placeholder_step_needs_no_blocks(self):
        """found:false steps carry only plan fields + found=False — the
        placeholder contract lives in LOCATOR_RULES (core, always sent)."""
        steps = [{
            "step_description": "click the missing button",
            "element_description": "missing button",
            "keyword": "Click",
            "found": False,
        }]
        assert select_conditional_blocks(steps) == []

    def test_fail_open_on_malformed_step(self):
        """ANY exception during trigger evaluation → ALL blocks (today's
        prompt exactly). A non-dict step is the canonical malformed input."""
        assert select_conditional_blocks(
            ["not a dict"]) == ALL_CONDITIONAL_BLOCKS


# ═══════════════════════════════════════════════════════════════════════════
# Slim steps-JSON view (F7) — prompt-only, merge output untouched
# ═══════════════════════════════════════════════════════════════════════════

class TestSlimStepsView:

    def test_drops_all_locators(self):
        slim = slim_steps_view(_simple_steps())
        assert all("all_locators" not in step for step in slim)

    def test_drops_empty_string_list_and_none_values(self):
        slim = slim_steps_view([_step_with(
            dropdown_framework="", datepicker_framework="",
            element_classes="", aria_invalid="", parent_classes="",
            all_locators=[], select_id=None,
        )])
        for key in ("dropdown_framework", "datepicker_framework",
                    "element_classes", "aria_invalid", "parent_classes",
                    "all_locators", "select_id"):
            assert key not in slim[0]

    def test_drops_stability_stable_keeps_other_verdicts(self):
        stable, positional = slim_steps_view([
            _step_with(stability="stable"),
            _step_with(stability="positional"),
        ])
        assert "stability" not in stable
        assert positional["stability"] == "positional"

    def test_always_keeps_found_true_and_false(self):
        """LOCATOR_RULES routes on found — it must survive slimming even
        though False is falsy."""
        slim = slim_steps_view([
            _step_with(found=True),
            {"step_description": "s", "keyword": "Click", "found": False},
        ])
        assert slim[0]["found"] is True
        assert slim[1]["found"] is False

    def test_keeps_substantive_values_verbatim(self):
        slim = slim_steps_view([_step_with(
            dropdown_framework="tom-select", select_id="permission_id",
            element_classes="form-control invalid",
        )])
        step = slim[0]
        assert step["locator"] == "id=target"
        assert step["keyword"] == "Click"
        assert step["dropdown_framework"] == "tom-select"
        assert step["select_id"] == "permission_id"
        assert step["element_classes"] == "form-control invalid"

    def test_does_not_mutate_input(self):
        """PROMPT-ONLY view: merge_locators output (workflow metrics, any
        future healing consumer) must stay intact."""
        steps = _simple_steps()
        before = json.dumps(steps, sort_keys=True)
        slim_steps_view(steps)
        assert json.dumps(steps, sort_keys=True) == before

    def test_non_locator_step_passes_through(self):
        slim = slim_steps_view([{
            "step_description": "Press Enter to search",
            "keyword": "Press Keys",
            "value": "Enter",
        }])
        assert slim[0] == {
            "step_description": "Press Enter to search",
            "keyword": "Press Keys",
            "value": "Enter",
        }


# ═══════════════════════════════════════════════════════════════════════════
# assemble_code_task composition — wired when triggered, absent when not,
# steps JSON last, fail-open on malformed input, INFO log
# ═══════════════════════════════════════════════════════════════════════════

class TestAssembleTaskComposition:

    TRIGGER_FOR_BLOCK = {
        "STABILITY_WARNING_RULES": {"stability": "positional"},
        "CONDITIONAL_LOGIC_HANDLING": {"condition_type": "IF"},
        "LOOP_HANDLING": {"loop_type": "FOR", "loop_source": "id=items"},
        "DROPDOWN_HANDLING": {"element_type": "select"},
        "CHECKBOX_RADIO_HANDLING": {"element_type": "checkbox"},
        "FILE_UPLOAD_HANDLING": {"element_type": "file-upload"},
        "DATE_PICKER_HANDLING": {"element_type": "date-picker"},
        "STATE_VERIFICATION_HANDLING": {"keyword": "Get Classes"},
    }

    @pytest.mark.parametrize("block", ALL_CONDITIONAL_BLOCKS)
    def test_block_wired_when_triggered(self, block):
        steps = _simple_steps() + [_step_with(**self.TRIGGER_FOR_BLOCK[block])]
        desc = _capture_description(json.dumps({"steps": steps}))
        assert _block_header(block) in desc

    @pytest.mark.parametrize("block", ALL_CONDITIONAL_BLOCKS)
    def test_block_absent_when_not_triggered(self, block):
        desc = _capture_description(json.dumps({"steps": _simple_steps()}))
        assert _block_header(block) not in desc

    def test_core_sections_always_present(self):
        """Output rules, variable/locator/validation rules, and the
        libraries section are unconditional."""
        desc = _capture_description(json.dumps({"steps": _simple_steps()}))
        assert "OUTPUT JSON FORMAT" in desc
        assert _block_header("VARIABLE_DECLARATION_RULES") in desc
        assert _block_header("LOCATOR_RULES") in desc
        assert _block_header("VALIDATION_RULES") in desc
        assert "--- LIBRARIES TO INCLUDE ---" in desc

    def test_steps_json_is_slim(self):
        """The description ships the slim view — all_locators and empty
        fields never reach the LLM (F7)."""
        desc = _capture_description(json.dumps({"steps": _simple_steps()}))
        assert "all_locators" not in desc
        assert '"aria_invalid"' not in desc
        # substantive content survives
        assert "id=search" in desc
        assert '"found": true' in desc

    def test_steps_json_placed_last(self):
        """Dynamic tail: the slim steps JSON is the FINAL content of the
        description — recency salience now, prefix-cachability later."""
        steps = _simple_steps()
        desc = _capture_description(json.dumps({"steps": steps}))
        expected_tail = json.dumps({"steps": slim_steps_view(steps)})
        assert desc.rstrip().endswith(expected_tail)
        assert desc.index("--- LIBRARIES TO INCLUDE ---") \
            < desc.index(expected_tail)

    def test_extraction_instruction_kept_with_steps(self):
        desc = _capture_description(json.dumps({"steps": _simple_steps()}))
        assert "Extract the steps array from the 'steps' key" in desc

    def test_hints_stay_at_top(self):
        """Standing salience decision: learning hints lead the description."""
        desc = _capture_description(
            json.dumps({"steps": _simple_steps()}),
            hint_context={"assembler": "always close the browser"})
        assert desc.index("always close the browser") \
            < desc.index("OUTPUT JSON FORMAT")

    def test_fail_open_on_unparseable_steps_json(self):
        """Malformed JSON → ALL blocks + the original payload verbatim
        (today's prompt exactly)."""
        desc = _capture_description("not json at all {{{")
        for block in ALL_CONDITIONAL_BLOCKS:
            assert _block_header(block) in desc
        assert "not json at all {{{" in desc

    def test_fail_open_on_unexpected_steps_shape(self):
        """A parseable payload without a well-formed steps list is doubt —
        fail open, ship verbatim."""
        payload = json.dumps({"steps": "oops-not-a-list"})
        desc = _capture_description(payload)
        for block in ALL_CONDITIONAL_BLOCKS:
            assert _block_header(block) in desc
        assert "oops-not-a-list" in desc

    def test_info_log_lists_included_blocks(self, caplog):
        """One INFO line per run naming the included blocks — Task 25
        correlates failures against active blocks with it."""
        steps = _simple_steps() + [_step_with(element_type="select")]
        with caplog.at_level(logging.INFO, logger="src.backend.crew_ai.tasks"):
            _capture_description(json.dumps({"steps": steps}))
        composer_lines = [r.getMessage() for r in caplog.records
                          if "PROMPT COMPOSER" in r.getMessage()]
        assert len(composer_lines) == 1
        assert "DROPDOWN_HANDLING" in composer_lines[0]
