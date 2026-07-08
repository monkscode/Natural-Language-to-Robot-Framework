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
