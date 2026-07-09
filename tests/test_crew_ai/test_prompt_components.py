"""
Smoke tests for src.backend.crew_ai.prompts.components.PromptComponents.

DROPDOWN_HANDLING teaches the Code Assembler how to generate dropdown
interaction code.  Tom Select uses Evaluate JavaScript (not Click chains)
because TomSelect's setValue() API is viewport-agnostic and works across
all websites regardless of internal option values.

These tests guard:
  - Framework routing order (dropdown_framework checked before element_type)
  - Correct JS templates for both code paths (select_id known / unknown)
  - Decision logic priority (Tom Select before native select)
  - Worked examples present for both paths

Reference: docs/ELEMENT_TYPE_CLASSIFIER_ARCHITECTURE.md Section 6.
"""

from src.backend.crew_ai.prompts.components import PromptComponents


class TestDropdownHandlingPromptComponent:

    def test_prompt_is_non_empty_string(self):
        assert isinstance(PromptComponents.DROPDOWN_HANDLING, str)
        assert len(PromptComponents.DROPDOWN_HANDLING) > 0

    def test_framework_routing_precedes_element_type_fallback(self):
        """dropdown_framework routing must be introduced before element_type
        rules so the agent reads it first and doesn't shadow Tom Select with a
        generic type match."""
        body = PromptComponents.DROPDOWN_HANDLING
        assert "dropdown_framework" in body
        assert "tom-select" in body
        framework_idx = body.index("dropdown_framework")
        element_type_idx = body.index("element_type")
        assert framework_idx < body.rindex("element_type"), (
            "Framework routing must be introduced before element_type fallback"
        )
        assert framework_idx <= element_type_idx + 200, (
            "Framework routing must appear early in the prompt, not buried"
        )

    def test_tom_select_preferred_path_uses_evaluate_javascript(self):
        """When select_id is set the template must call Evaluate JavaScript
        targeting id=${select_id} (the hidden <select>) and resolve the option
        by display text via Array.from(el.options).find(...)."""
        body = PromptComponents.DROPDOWN_HANDLING
        assert "Evaluate JavaScript" in body
        assert "id=${select_id}" in body
        assert "Array.from(el.options).find" in body
        assert "el.tomselect.setValue" in body
        # Must NOT contain the old broken click-chain selectors.
        assert "css=#${select_id}-ts-control" not in body
        assert "css=#${select_id}-ts-dropdown" not in body
        assert ".ts-option" not in body

    def test_tom_select_fallback_path_uses_dom_traversal(self):
        """When select_id is null the template must call Evaluate JavaScript
        on the locator and find the hidden <select> via select.tomselected —
        position-independent, not relying on DOM sibling order."""
        body = PromptComponents.DROPDOWN_HANDLING
        assert "closest('.ts-wrapper').parentElement.querySelector('select.tomselected')" in body
        # Null-guard must be present — querySelector returns null if TomSelect
        # hasn't initialised or the class is absent.
        assert "if (sel && sel.tomselect)" in body
        # Old position-dependent traversal must be absent.
        assert "previousElementSibling" not in body

    def test_decision_logic_lists_tom_select_first(self):
        """Within the DECISION LOGIC block the Tom Select check must precede
        the element_type rules — otherwise element_type routing would shadow
        it on elements that carry both dropdown_framework and a generic type."""
        body = PromptComponents.DROPDOWN_HANDLING
        decision_idx = body.find("--- DECISION LOGIC ---")
        assert decision_idx != -1, "DECISION LOGIC section missing"
        decision_block = body[decision_idx:]

        framework_check = decision_block.find("dropdown_framework='tom-select'")
        select_check = decision_block.find("element_type='select'")
        assert framework_check != -1, "Tom Select framework rule missing in DECISION LOGIC"
        assert select_check != -1, "element_type='select' rule missing in DECISION LOGIC"
        assert framework_check < select_check, (
            "Tom Select framework check must take priority over element_type routing"
        )

    def test_includes_tom_select_examples_for_both_paths(self):
        """A worked example must exist for each code path so the LLM has a
        concrete substitution pattern to follow."""
        body = PromptComponents.DROPDOWN_HANDLING
        # Preferred path example uses a real select_id.
        assert "id=permission_id" in body
        assert "Customer_permission" in body
        # Fallback path example uses an xpath label-based locator
        # (the shape produced by browser-service Strategy 2/3 output).
        assert "xpath=//label" in body
        assert "Asia/Kolkata" in body


class TestFileUploadHandlingPromptComponent:
    """Task E (G5): file-upload steps must emit Upload File By Selector on
    the (usually hidden) input - never Click, which opens the browser's
    NATIVE file dialog and hangs the test until timeout."""

    def test_prompt_is_non_empty_string(self):
        assert isinstance(PromptComponents.FILE_UPLOAD_HANDLING, str)
        assert len(PromptComponents.FILE_UPLOAD_HANDLING) > 0

    def test_routes_on_element_type(self):
        body = PromptComponents.FILE_UPLOAD_HANDLING
        assert "element_type='file-upload'" in body

    def test_mandates_upload_keyword_and_forbids_click(self):
        body = PromptComponents.FILE_UPLOAD_HANDLING
        assert "Upload File By Selector" in body
        assert "NEVER use Click" in body
        # The why matters for LLM compliance: native dialog hangs.
        assert "native" in body.lower()

    def test_hidden_input_is_expected_not_an_error(self):
        """The locator targets a display:none input by design - the agent
        must not try to make it visible or click the styled button."""
        body = PromptComponents.FILE_UPLOAD_HANDLING
        assert "HIDDEN" in body or "hidden" in body
        assert "legal" in body.lower() or "expected" in body.lower()

    def test_placeholder_variable_when_no_file_value(self):
        """Review-then-execute contract: with no file path in the step,
        declare a Variables-section placeholder the user fills in."""
        body = PromptComponents.FILE_UPLOAD_HANDLING
        assert "${UPLOAD_FILE}" in body
        assert "*** Variables ***" in body

    def test_worked_example_present(self):
        body = PromptComponents.FILE_UPLOAD_HANDLING
        assert "customer_import_mapper" in body

    def test_wired_into_assemble_task(self):
        """The block only helps if assemble_code_task actually includes it."""
        from pathlib import Path
        import src.backend.crew_ai.tasks as tasks_mod
        src_text = Path(tasks_mod.__file__).read_text(encoding="utf-8")
        assert "FILE_UPLOAD_HANDLING" in src_text


class TestDatePickerHandlingPromptComponent:
    """Task D (G4): flatpickr date pickers render READONLY inputs — Fill
    Text waits for editability and dies with a timeout, every run. The
    only robust path is the widget's own API via one Evaluate JavaScript
    line (verified live on ASTPP 2026-07-08). Native input[type=date]
    keeps plain Fill Text."""

    def test_prompt_is_non_empty_string(self):
        assert isinstance(PromptComponents.DATE_PICKER_HANDLING, str)
        assert len(PromptComponents.DATE_PICKER_HANDLING) > 0

    def test_routes_on_element_type_and_framework(self):
        body = PromptComponents.DATE_PICKER_HANDLING
        assert "element_type='date-picker'" in body
        assert "datepicker_framework='flatpickr'" in body

    def test_flatpickr_path_uses_setdate_js(self):
        """The template must call setDate(value, true) on el._flatpickr —
        state update + change event in one call, no calendar clicking."""
        body = PromptComponents.DATE_PICKER_HANDLING
        assert "Evaluate JavaScript" in body
        assert "el._flatpickr" in body
        assert "setDate" in body
        # Null-guard: the instance can be absent if the page re-rendered.
        assert "if (fp)" in body

    def test_flatpickr_path_forbids_fill_text(self):
        """The readonly input is EXPECTED — the agent must not 'fix' it
        by trying Fill Text first."""
        body = PromptComponents.DATE_PICKER_HANDLING
        assert "readonly" in body.lower()
        assert "do NOT try Fill Text" in body

    def test_native_path_keeps_fill_text(self):
        """Plain input[type=date] accepts Fill Text with an ISO date —
        no JS needed."""
        body = PromptComponents.DATE_PICKER_HANDLING
        assert "datepicker_framework='native'" in body
        assert "Fill Text" in body

    def test_worked_example_present(self):
        """A worked example with the real ASTPP locator gives the LLM a
        concrete substitution pattern."""
        body = PromptComponents.DATE_PICKER_HANDLING
        assert "customer_cdr_from_date" in body

    def test_wired_into_assemble_task(self):
        from pathlib import Path
        import src.backend.crew_ai.tasks as tasks_mod
        src_text = Path(tasks_mod.__file__).read_text(encoding="utf-8")
        assert "DATE_PICKER_HANDLING" in src_text

    def test_identify_task_forwards_datepicker_framework(self):
        """The Assembler only sees what the identify agent copies to the
        step — the extraction instruction must name datepicker_framework
        (same pipe as dropdown_framework/select_id)."""
        from pathlib import Path
        import src.backend.crew_ai.tasks as tasks_mod
        src_text = Path(tasks_mod.__file__).read_text(encoding="utf-8")
        assert "'datepicker_framework' → 'datepicker_framework'" in src_text


class TestStateVerificationPlanning:
    """Task G (G7): planner side of class-state verification.

    'Verify the Email field shows an error' on ASTPP: the site shows NO
    error text anywhere — the server round-trip adds class `invalid` to
    the input. The planner's only verification patterns were read-text
    and check-number, so it planned a Get Text hunt for a message that
    does not exist → dead step. The planner must instead target the
    FIELD ITSELF with Get Classes."""

    def test_prompt_is_non_empty_string(self):
        assert isinstance(PromptComponents.PLANNING_STATE_VERIFICATION, str)
        assert len(PromptComponents.PLANNING_STATE_VERIFICATION) > 0

    def test_plans_get_classes_against_the_field(self):
        body = PromptComponents.PLANNING_STATE_VERIFICATION
        assert "Get Classes" in body
        assert "field itself" in body

    def test_forbids_hunting_for_message_text(self):
        """The failure mode being fixed: planning a Get Text step for an
        error message element that does not exist on class-only sites."""
        body = PromptComponents.PLANNING_STATE_VERIFICATION
        assert "do NOT plan a step to read an error message" in body

    def test_covers_the_state_phrasing_family(self):
        """'shows an error' is one of a family: disabled, highlighted,
        marked invalid — all are element-state checks, not text checks."""
        body = PromptComponents.PLANNING_STATE_VERIFICATION
        assert "shows an error" in body
        assert "disabled" in body
        assert "highlighted" in body

    def test_wired_into_plan_task(self):
        from pathlib import Path
        import src.backend.crew_ai.tasks as tasks_mod
        src_text = Path(tasks_mod.__file__).read_text(encoding="utf-8")
        assert "PLANNING_STATE_VERIFICATION" in src_text


class TestStateVerificationHandling:
    """Task G (G7): assembler side of class-state verification.

    The identify agent forwards the classes the locator engine OBSERVED
    on the field at locate time — captured after the preceding steps ran,
    so on ASTPP the empty-form Save has already happened and the observed
    list is 'text field medium form-control invalid'. The assembler picks
    the state marker from that observed evidence; it never guesses. When
    no token reads as a state marker, it emits a loud placeholder that
    fails until a human fills it — never a silently-green base-class
    assertion (Bootstrap 3 puts has-error on the PARENT, so the field's
    own list can legitimately contain no marker)."""

    def test_prompt_is_non_empty_string(self):
        assert isinstance(PromptComponents.STATE_VERIFICATION_HANDLING, str)
        assert len(PromptComponents.STATE_VERIFICATION_HANDLING) > 0

    def test_routes_on_get_classes_keyword(self):
        body = PromptComponents.STATE_VERIFICATION_HANDLING
        assert "Get Classes" in body
        assert "element_classes" in body

    def test_auto_picks_marker_from_observed_classes(self):
        """The common case must be fully automatic: pick the error-family
        token from the observed class list — no user input needed."""
        body = PromptComponents.STATE_VERIFICATION_HANDLING
        assert "invalid" in body
        assert "error" in body
        assert "danger" in body

    def test_base_classes_never_qualify(self):
        """Picking a base/layout class (present error or not) produces a
        test that passes forever — the silent-green disease. The prompt
        must name form-control as a NEVER-qualifying example."""
        body = PromptComponents.STATE_VERIFICATION_HANDLING
        assert "form-control" in body
        assert "NEVER" in body

    def test_user_word_is_cross_check_only(self):
        """Owner decision 2026-07-09: observation decides. A user-named
        state word is used only when it appears in the observed list."""
        body = PromptComponents.STATE_VERIFICATION_HANDLING
        assert "observed" in body
        assert "cross-check" in body

    def test_placeholder_fallback_is_loud(self):
        """No marker in the observed list → EXPECTED_STATE_CLASS placeholder
        with a TODO comment listing what WAS observed, so the test fails
        loudly until a human fills it (Task 12 contract)."""
        body = PromptComponents.STATE_VERIFICATION_HANDLING
        assert "EXPECTED_STATE_CLASS" in body
        assert "TODO" in body

    def test_worked_example_uses_astpp_reality(self):
        """A worked example with the live-verified ASTPP data gives the
        LLM a concrete substitution pattern."""
        body = PromptComponents.STATE_VERIFICATION_HANDLING
        assert "form-control invalid" in body

    def test_wired_into_assemble_task(self):
        from pathlib import Path
        import src.backend.crew_ai.tasks as tasks_mod
        src_text = Path(tasks_mod.__file__).read_text(encoding="utf-8")
        assert "STATE_VERIFICATION_HANDLING" in src_text

    def test_identify_task_forwards_element_classes(self):
        """The Assembler only sees what the identify agent copies to the
        step — the extraction instruction must map element_info.className
        to element_classes (the pipe verified in browser-service
        smart_locator.py element_info payload)."""
        from pathlib import Path
        import src.backend.crew_ai.tasks as tasks_mod
        src_text = Path(tasks_mod.__file__).read_text(encoding="utf-8")
        assert "'element_info.className' → 'element_classes'" in src_text
