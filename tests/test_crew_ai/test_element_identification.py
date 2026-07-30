"""
Tests for src.backend.crew_ai.element_identification — the deterministic
element→step hand-off that replaced the element-identifier LLM agent (Task 16).

The module has three jobs, all pure Python:
  1. BUILD:  decide which plan steps need locators (keyword whitelist), extract
     the URL, write element specs (description verbatim + the ported
     FORM_ELEMENT_HANDLING rewrite + keyword→action mapping), dedup by
     description, and produce the ONE batch_browser_automation payload.
  2. CALL:   invoke the tool exactly once. Tool error OR found:false → the step
     gets NO locator → the Assembler's existing placeholder path. NO retry,
     NO second call, NO reformulation (contract — see Task 16 §F: the old LLM
     made a rogue second call and returned a fabricated locator).
  3. MERGE:  staple the FULL locator_mapping entry onto each step that used the
     element: locator, element_type (element_info.tagName fallback),
     dropdown_framework, select_id, datepicker_framework, element_classes,
     aria_invalid, parent_classes, stability, all_locators, and the ASTPP
     flags (visibility_filtered/row_anchored/row_anchor_ambiguous, only when
     True). Steps are filtered to PlannedStep fields first, so raw-fallback
     dicts with LLM key drift can neither crash the merge nor leak fabricated
     locator keys.

No LLM, no network — the tool is injected as a plain callable.
"""

import pytest

from src.backend.crew_ai.element_identification import (
    action_for_keyword,
    build_elements,
    extract_plan_url,
    identify_elements,
    merge_locators,
    rewrite_form_description,
    step_needs_locator,
)


# ─── step builders ───────────────────────────────────────────────────────────

def _step(keyword, description=None, value=None, **extra):
    s = {"step_description": f"{keyword} step", "keyword": keyword}
    if description is not None:
        s["element_description"] = description
    if value is not None:
        s["value"] = value
    s.update(extra)
    return s


OPEN = _step("Open Browser", value="https://example.com")
CLOSE = _step("Close Browser")


def _mapping_entry(locator="id=x", **extra):
    entry = {
        "best_locator": locator,
        "all_locators": [{"locator": locator, "type": "id"}],
        "validation": {},
        "element_info": {},
        "found": True,
        "element_type": None,
        "dropdown_framework": "",
        "select_id": None,
        "datepicker_framework": "",
        "stability": "stable",
        "visibility_filtered": False,
        "row_anchored": False,
        "row_anchor_ambiguous": False,
    }
    entry.update(extra)
    return entry


# ─── which steps need locators (port checklist #1) ───────────────────────────

class TestStepNeedsLocator:

    @pytest.mark.parametrize("keyword", [
        "Open Browser", "Close Browser", "New Browser", "New Context",
        "New Page", "Go To", "Should Be True", "Should Contain",
        "Maximize Browser Window", "Set Viewport Size",
    ])
    def test_browser_init_and_assertion_keywords_do_not(self, keyword):
        assert step_needs_locator(_step(keyword, description="anything")) is False

    @pytest.mark.parametrize("keyword", [
        "Input Text", "Fill Text", "Type Text", "Click", "Click Element",
        "Get Text", "Get Elements", "Select Options By", "Select From List By Label",
        "Get Classes", "Get Attribute", "Press Keys", "Get Element Count",
        "Get Selected Options", "Wait For Elements State",
    ])
    def test_interaction_keywords_do(self, keyword):
        assert step_needs_locator(_step(keyword, description="the search box")) is True

    def test_interaction_keyword_without_description_still_needs_locator(self):
        """Planner defect (missing element_description on an interaction step)
        must surface as a found:false placeholder — NOT be silently skipped."""
        assert step_needs_locator(_step("Click")) is True

    def test_unknown_keyword_with_description_needs_locator(self):
        assert step_needs_locator(_step("Hover Over Thing", description="the menu")) is True

    def test_unknown_keyword_without_description_does_not(self):
        assert step_needs_locator(_step("Get Length")) is False

    def test_keyword_match_is_case_insensitive(self):
        assert step_needs_locator(_step("open browser", description="x")) is False
        assert step_needs_locator(_step("INPUT TEXT", description="x")) is True


# ─── URL extraction (port checklist #2) ──────────────────────────────────────

class TestExtractPlanUrl:

    def test_open_browser_value(self):
        assert extract_plan_url([OPEN, CLOSE]) == "https://example.com"

    def test_new_page_value(self):
        steps = [_step("New Browser"), _step("New Page", value="https://astpp.example.org")]
        assert extract_plan_url(steps) == "https://astpp.example.org"

    def test_first_navigation_step_wins(self):
        steps = [
            _step("Open Browser", value="https://first.com"),
            _step("New Page", value="https://second.com"),
        ]
        assert extract_plan_url(steps) == "https://first.com"

    def test_none_when_no_navigation_step(self):
        assert extract_plan_url([_step("Click", description="x"), CLOSE]) is None

    def test_none_when_navigation_step_has_no_value(self):
        assert extract_plan_url([_step("Open Browser")]) is None

    def test_url_shaped_value_on_non_navigation_keyword(self):
        """Planner keyword variance (2026-07-11 bench, q03 rep2): the plan's
        only URL sat on a 'New Browser' step's value — not a navigation
        keyword, so the browser call was skipped and every element got a
        found:false placeholder. A literal URL anywhere in the plan's values
        must win over that guaranteed failure."""
        steps = [_step("New Browser", value="https://nutronsystems.com/"),
                 _step("Click Element", description="Solutions link")]
        assert extract_plan_url(steps) == "https://nutronsystems.com/"

    def test_navigation_keyword_beats_url_shaped_fallback(self):
        steps = [
            _step("New Browser", value="https://fallback.com"),
            _step("New Page", value="https://explicit-nav.com"),
        ]
        assert extract_plan_url(steps) == "https://explicit-nav.com"

    def test_non_url_values_never_extracted(self):
        """browser-type / input-text values must not be mistaken for the URL."""
        steps = [_step("New Browser", value="chromium"),
                 _step("Input Text", description="search box", value="Cierra")]
        assert extract_plan_url(steps) is None

    def test_about_blank_is_skipped_for_the_real_url(self):
        """Measured on 4 of 897 captured runs (q08/q09/q10): the planner emits
        'New Page -> about:blank' and then 'Go To -> <real url>'. First-match
        wins, so the real URL was discarded and the agent was told to open
        https://about:blank. The browser-use LLM silently repaired it on 3 of 3
        runs by reading the URL out of the goal text — a net that disappears
        once navigation moves to Agent(initial_actions=...)."""
        steps = [
            _step("New Page", value="about:blank"),
            _step("Go To", value="https://books.toscrape.com"),
        ]
        assert extract_plan_url(steps) == "https://books.toscrape.com"

    def test_about_blank_alone_yields_no_url(self):
        """No real URL anywhere is a loud skip (found:false placeholders), not
        a navigation to a blank page."""
        assert extract_plan_url([_step("New Page", value="about:blank")]) is None

    def test_value_with_trailing_junk_is_trimmed_to_the_url(self):
        """Measured once: value 'https://sujal.astppbilling.org/    commit'.
        strip() only touches the ends, so the junk rode through into
        NavigateAction."""
        steps = [_step("New Page", value="https://example.org/    commit")]
        assert extract_plan_url(steps) == "https://example.org/"

    def test_bare_hostname_on_a_navigation_step_is_still_returned(self):
        """The planner's own prompt example is 'Open Browser -> Flipkart'.
        A bare hostname is a URL the downstream normalizer can complete;
        only values that cannot be one are rejected."""
        steps = [_step("Open Browser", value="books.toscrape.com")]
        assert extract_plan_url(steps) == "books.toscrape.com"

    def test_dotless_site_name_on_a_navigation_step_is_returned(self):
        """The literal exemplar shipped in the planner prompt
        (prompts/components.py:699, labelled CORRECT) is
        'Open Browser -> Flipkart' — a site name with no dot. Rejecting it
        makes extract_plan_url return None, which skips the browser call
        entirely (element_identification.py:462) and hands every element a
        found:false placeholder. PlannedStep has no browser field, so a
        navigation step's value is always the destination."""
        steps = [_step("Open Browser", value="Flipkart")]
        assert extract_plan_url(steps) == "Flipkart"

    def test_scheme_less_host_port_is_returned(self):
        """Same class as the exemplar: no dot, still a real destination."""
        steps = [_step("New Page", value="localhost:3000")]
        assert extract_plan_url(steps) == "localhost:3000"

    def test_prose_on_a_navigation_step_is_rejected(self):
        """'the login page' is not a URL. Returning it produces
        https://the login page — a guaranteed navigation failure with no
        agent left to correct it.

        Multi-token is what separates prose from a bare site name: 'Flipkart'
        is one token, 'the login page' is three. A dot alone cannot tell them
        apart, and requiring one rejected the shipped exemplar."""
        steps = [_step("New Page", value="the login page")]
        assert extract_plan_url(steps) is None

    def test_dotless_first_token_of_prose_is_rejected(self):
        """The trailing-junk trim takes tokens[0], so prose must not survive
        it: 'Flipkart homepage' would otherwise navigate to https://Flipkart."""
        steps = [_step("New Page", value="Flipkart homepage")]
        assert extract_plan_url(steps) is None

    @pytest.mark.parametrize("value", [
        "mailto:help@example.com",
        "tel:+1234567890",
        "blob:https://example.com/9f8e",
        "chrome-extension://abcdefg/popup.html",
        "ws://example.com/socket",
        "ftp://files.example.com/pub",
        "view-source:https://example.com",
    ])
    def test_a_non_navigable_scheme_is_rejected(self, value):
        """The old guard blacklisted five schemes by prefix, so every scheme
        outside the list rode through: 'mailto:help@example.com' qualified on
        the dot rule and 'tel:+1234567890' on the single-token rule. A scheme
        the browser cannot open as a page must never win the navigation slot
        and displace a real URL on a later step."""
        steps = [_step("New Page", value=value)]
        assert extract_plan_url(steps) is None

    @pytest.mark.parametrize("value", [
        "localhost:3000",
        "127.0.0.1:8080",
        "example.com:443/path",
    ])
    def test_a_host_port_is_not_read_as_a_scheme(self, value):
        """The regression an allowlist invites. urlsplit('localhost:3000')
        reports scheme='localhost' and urlsplit('example.com:443/path')
        reports scheme='example.com', so a naive scheme allowlist would
        discard exactly the internal destinations this framework is used
        against. A colon followed by a bare port is a port, not a scheme."""
        steps = [_step("New Page", value=value)]
        assert extract_plan_url(steps) == value

    def test_a_non_navigable_scheme_does_not_displace_a_later_url(self):
        """Same shape as the about:blank defect, one scheme further out."""
        steps = [
            _step("New Page", value="mailto:support@example.com"),
            _step("Go To", value="https://books.toscrape.com"),
        ]
        assert extract_plan_url(steps) == "https://books.toscrape.com"

    def test_an_uppercased_scheme_is_still_recognised(self):
        """Schemes are case-insensitive; the value keeps its original case."""
        assert extract_plan_url([_step("New Page", value="HTTPS://Example.com/A")]) == \
            "HTTPS://Example.com/A"
        assert extract_plan_url([_step("New Page", value="MAILTO:a@b.com")]) is None


# ─── FORM_ELEMENT_HANDLING port (port checklist #3) ──────────────────────────

class TestRewriteFormDescription:

    def test_checkbox_description_rewritten_to_input_template(self):
        assert rewrite_form_description("remember me checkbox") == \
            'the checkbox INPUT element for "remember me checkbox"'

    def test_radio_description_rewritten(self):
        assert rewrite_form_description("male radio button") == \
            'the radio button INPUT element for "male radio button"'

    def test_toggle_description_rewritten(self):
        assert rewrite_form_description("dark mode toggle") == \
            'the toggle INPUT element for "dark mode toggle"'

    def test_plain_description_unchanged(self):
        assert rewrite_form_description("search box in header") == "search box in header"

    def test_case_insensitive_trigger(self):
        assert rewrite_form_description("Remember Me CHECKBOX") == \
            'the checkbox INPUT element for "Remember Me CHECKBOX"'

    def test_idempotent_when_already_targeting_input_element(self):
        """If the planner already wrote the INPUT-element phrasing, do not
        wrap it a second time."""
        already = 'the checkbox INPUT element for "remember me"'
        assert rewrite_form_description(already) == already


# ─── keyword→action mapping (port checklist #4) ──────────────────────────────

class TestActionForKeyword:

    @pytest.mark.parametrize("keyword,action", [
        ("Input Text", "input"),
        ("Fill Text", "input"),
        ("Type Text", "input"),
        ("Click", "click"),
        ("Click Element", "click"),
        ("Check Checkbox", "click"),
        ("Get Text", "get_text"),
        ("Get Classes", "get_text"),
        ("Get Attribute", "get_text"),
        ("Get Elements", "get_text"),
        ("Get Element Count", "get_text"),
        ("Select Options By", "select"),
        ("Select From List By Label", "select"),
    ])
    def test_mapping(self, keyword, action):
        assert action_for_keyword(keyword) == action

    def test_unknown_keyword_defaults_to_locate_only(self):
        """get_text is locate-only — the safe default for keywords we can't
        classify (never click an unknown element in the exploration session)."""
        assert action_for_keyword("Frobnicate Widget") == "get_text"


# ─── element building + dedup (port checklist #3, #5) ────────────────────────

class TestBuildElements:

    def test_descriptions_forwarded_verbatim_with_spatial_context(self):
        steps = [OPEN, _step("Get Text", description="first product name in results list, not in sidebar")]
        elements, _ = build_elements(steps)
        assert elements == [{
            "id": "elem_1",
            "description": "first product name in results list, not in sidebar",
            "action": "get_text",
        }]

    def test_input_value_included(self):
        steps = [OPEN, _step("Input Text", description="search box", value="shoes")]
        elements, _ = build_elements(steps)
        assert elements[0]["action"] == "input"
        assert elements[0]["value"] == "shoes"

    def test_select_value_included(self):
        steps = [OPEN, _step("Select Options By", description="country dropdown", value="label    India")]
        elements, _ = build_elements(steps)
        assert elements[0]["action"] == "select"
        assert elements[0]["value"] == "label    India"

    def test_click_has_no_value_key(self):
        steps = [OPEN, _step("Click", description="login button", value="ignored")]
        elements, _ = build_elements(steps)
        assert "value" not in elements[0]

    def test_dedup_by_identical_description(self):
        """Input Text + Press Keys on the search box → ONE element with
        action=input and the Input Text step's value (§F worked example)."""
        steps = [
            OPEN,
            _step("Input Text", description="search box", value="shoes"),
            _step("Press Keys", description="search box", value="Enter"),
        ]
        elements, step_element_ids = build_elements(steps)
        assert len(elements) == 1
        assert elements[0] == {
            "id": "elem_1", "description": "search box",
            "action": "input", "value": "shoes",
        }
        # Both steps map to the same element id.
        assert step_element_ids == {1: "elem_1", 2: "elem_1"}

    def test_dedup_upgrades_action_when_input_step_comes_second(self):
        """Press Keys first, Input Text second — still ONE element with
        action=input and the input step's value (precedence, not order)."""
        steps = [
            OPEN,
            _step("Press Keys", description="search box", value="Enter"),
            _step("Input Text", description="search box", value="shoes"),
        ]
        elements, step_element_ids = build_elements(steps)
        assert len(elements) == 1
        assert elements[0]["action"] == "input"
        assert elements[0]["value"] == "shoes"
        assert step_element_ids == {1: "elem_1", 2: "elem_1"}

    def test_standalone_press_keys_is_click(self):
        """A Press Keys step with no sibling sharing the element focuses the
        element — click is the least-side-effect action for the session."""
        steps = [OPEN, _step("Press Keys", description="search box", value="Enter")]
        elements, _ = build_elements(steps)
        assert elements[0]["action"] == "click"
        assert "value" not in elements[0]

    def test_element_ids_are_sequential_in_first_appearance_order(self):
        steps = [
            OPEN,
            _step("Input Text", description="username field", value="bob"),
            _step("Input Text", description="password field", value="secret"),
            _step("Click", description="login button"),
        ]
        elements, step_element_ids = build_elements(steps)
        assert [e["id"] for e in elements] == ["elem_1", "elem_2", "elem_3"]
        assert step_element_ids == {1: "elem_1", 2: "elem_2", 3: "elem_3"}

    def test_form_element_rewrite_applied_before_dedup(self):
        """Two steps naming the same checkbox must land on the same rewritten
        description — one element, one rewrite."""
        steps = [
            OPEN,
            _step("Click", description="remember me checkbox"),
            _step("Get Classes", description="remember me checkbox"),
        ]
        elements, step_element_ids = build_elements(steps)
        assert len(elements) == 1
        assert elements[0]["description"] == 'the checkbox INPUT element for "remember me checkbox"'
        assert step_element_ids == {1: "elem_1", 2: "elem_1"}

    def test_no_locator_steps_build_no_elements(self):
        elements, step_element_ids = build_elements([OPEN, CLOSE, _step("Should Be True")])
        assert elements == []
        assert step_element_ids == {}

    def test_interaction_step_without_description_gets_no_element(self):
        """Planner defect: an interaction step missing element_description
        cannot be located — no element spec is built for it (the merge marks
        it found:false → Assembler placeholder)."""
        steps = [OPEN, _step("Click")]
        elements, step_element_ids = build_elements(steps)
        assert elements == []
        assert step_element_ids == {}


# ─── merge: stapling the full contract (port checklist #8) ───────────────────

class TestMergeLocators:

    def test_full_contract_stapled_onto_step(self):
        steps = [OPEN, _step("Input Text", description="email field", value="a@b.c"), CLOSE]
        entry = _mapping_entry(
            locator="id=email",
            element_type="input",
            dropdown_framework="",
            select_id=None,
            datepicker_framework="",
            element_info={
                "tagName": "input",
                "className": "form-control invalid",
                "ariaInvalid": "true",
                "parentClassName": "form-group has-error",
            },
            stability="stable",
            all_locators=[{"locator": "id=email", "type": "id"}],
        )
        merged = merge_locators(steps, {1: "elem_1"}, {"elem_1": entry})

        step = merged[1]
        assert step["locator"] == "id=email"
        assert step["found"] is True
        assert step["element_type"] == "input"
        assert step["dropdown_framework"] == ""
        assert step["datepicker_framework"] == ""
        assert step["element_classes"] == "form-control invalid"
        assert step["aria_invalid"] == "true"
        assert step["parent_classes"] == "form-group has-error"
        assert step["stability"] == "stable"
        assert step["all_locators"] == [{"locator": "id=email", "type": "id"}]

    def test_element_type_falls_back_to_tag_name(self):
        """element_type null → element_info.tagName (the old prompt's rule,
        now code)."""
        steps = [OPEN, _step("Click", description="save button")]
        entry = _mapping_entry(element_type=None, element_info={"tagName": "button"})
        merged = merge_locators(steps, {1: "elem_1"}, {"elem_1": entry})
        assert merged[1]["element_type"] == "button"

    def test_explicit_element_type_wins_over_tag_name(self):
        steps = [OPEN, _step("Click", description="date field")]
        entry = _mapping_entry(element_type="date-picker", element_info={"tagName": "input"})
        merged = merge_locators(steps, {1: "elem_1"}, {"elem_1": entry})
        assert merged[1]["element_type"] == "date-picker"

    def test_tom_select_fields_stapled(self):
        steps = [OPEN, _step("Select Options By", description="rate group dropdown", value="label    default")]
        entry = _mapping_entry(
            locator="css=#pricelist_id-ts-control",
            dropdown_framework="tom-select",
            select_id="pricelist_id",
        )
        merged = merge_locators(steps, {1: "elem_1"}, {"elem_1": entry})
        assert merged[1]["dropdown_framework"] == "tom-select"
        assert merged[1]["select_id"] == "pricelist_id"

    def test_datepicker_framework_stapled(self):
        steps = [OPEN, _step("Fill Text", description="from date field", value="2026-07-01")]
        entry = _mapping_entry(element_type="date-picker", datepicker_framework="flatpickr")
        merged = merge_locators(steps, {1: "elem_1"}, {"elem_1": entry})
        assert merged[1]["datepicker_framework"] == "flatpickr"

    def test_volatile_stability_stapled(self):
        steps = [OPEN, _step("Click", description="submit button")]
        entry = _mapping_entry(locator="xpath=//div[4]/input", stability="volatile")
        merged = merge_locators(steps, {1: "elem_1"}, {"elem_1": entry})
        assert merged[1]["stability"] == "volatile"

    def test_shared_element_staples_same_contract_on_both_steps(self):
        steps = [
            OPEN,
            _step("Input Text", description="search box", value="shoes"),
            _step("Press Keys", description="search box", value="Enter"),
        ]
        entry = _mapping_entry(locator="name=q", element_info={"tagName": "input"})
        merged = merge_locators(steps, {1: "elem_1", 2: "elem_1"}, {"elem_1": entry})
        assert merged[1]["locator"] == "name=q"
        assert merged[2]["locator"] == "name=q"
        # Original per-step fields survive.
        assert merged[1]["value"] == "shoes"
        assert merged[2]["value"] == "Enter"

    def test_not_found_step_gets_no_locator(self):
        """found:false → NO locator key, found False — the Assembler's
        placeholder path (Task 12 contract)."""
        steps = [OPEN, _step("Get Text", description="phantom element")]
        merged = merge_locators(
            steps, {1: "elem_1"},
            {"elem_1": {"found": False, "error": "Element not found"}},
        )
        assert merged[1]["found"] is False
        assert "locator" not in merged[1]
        assert "stability" not in merged[1]

    def test_missing_mapping_entry_gets_no_locator(self):
        """Tool returned no entry for the element at all (error path) —
        same placeholder contract."""
        steps = [OPEN, _step("Get Text", description="phantom element")]
        merged = merge_locators(steps, {1: "elem_1"}, {})
        assert merged[1]["found"] is False
        assert "locator" not in merged[1]

    def test_interaction_step_without_description_marked_not_found(self):
        """Planner defect surfaces as found:false — never smoothed over."""
        steps = [OPEN, _step("Click")]
        merged = merge_locators(steps, {}, {})
        assert merged[1]["found"] is False
        assert "locator" not in merged[1]

    def test_non_locator_steps_pass_through_without_found_key(self):
        merged = merge_locators([OPEN, CLOSE], {}, {})
        assert merged[0]["keyword"] == "Open Browser"
        assert merged[0]["value"] == "https://example.com"
        assert "found" not in merged[0]
        assert "locator" not in merged[0]

    def test_condition_and_loop_keys_survive(self):
        steps = [
            OPEN,
            _step("Input Text", description="discount field", value="SAVE10",
                  condition_type="IF", condition_value="${total} > 100"),
            _step("Should Be True", condition_expression="${price} < 9999"),
        ]
        merged = merge_locators(steps, {1: "elem_1"}, {"elem_1": _mapping_entry()})
        assert merged[1]["condition_type"] == "IF"
        assert merged[1]["condition_value"] == "${total} > 100"
        assert merged[2]["condition_expression"] == "${price} < 9999"


# ─── identify_elements: the one-call orchestration (port checklist #6, #7) ───

class TestIdentifyElements:

    def _tool_recorder(self, response):
        calls = []

        def run_tool(elements, url, user_query):
            calls.append({"elements": elements, "url": url, "user_query": user_query})
            return response

        return run_tool, calls

    def _success_response(self, mapping, total=None):
        return {
            "status": "success",
            "success": True,
            "locator_mapping": mapping,
            "summary": {"total_elements": total if total is not None else len(mapping)},
        }

    def test_tool_called_once_with_all_elements_url_and_verbatim_query(self):
        steps = [
            OPEN,
            _step("Input Text", description="search box", value="shoes"),
            _step("Get Text", description="first product name"),
        ]
        run_tool, calls = self._tool_recorder(self._success_response({
            "elem_1": _mapping_entry(locator="name=q"),
            "elem_2": _mapping_entry(locator="css=.product"),
        }))
        result = identify_elements(steps, "search for shoes on flipkart.com and get the first product name", run_tool=run_tool)

        assert len(calls) == 1
        assert calls[0]["url"] == "https://example.com"
        assert calls[0]["user_query"] == "search for shoes on flipkart.com and get the first product name"
        assert [e["id"] for e in calls[0]["elements"]] == ["elem_1", "elem_2"]
        assert result["steps"][1]["locator"] == "name=q"
        assert result["steps"][2]["locator"] == "css=.product"

    def test_tool_error_means_no_retry_and_placeholder_path(self):
        """CONTRACT: tool error → one call only, every locator step
        found:false. The old LLM's rogue second call is the failure mode
        this codifies away."""
        steps = [OPEN, _step("Click", description="login button")]
        run_tool, calls = self._tool_recorder({
            "status": "error", "success": False,
            "message": "Service is busy", "results": [],
        })
        result = identify_elements(steps, "login", run_tool=run_tool)

        assert len(calls) == 1
        assert result["steps"][1]["found"] is False
        assert "locator" not in result["steps"][1]

    def test_tool_exception_is_caught_no_retry(self):
        steps = [OPEN, _step("Click", description="login button")]
        calls = []

        def run_tool(elements, url, user_query):
            calls.append(1)
            raise RuntimeError("connection reset")

        result = identify_elements(steps, "login", run_tool=run_tool)
        assert len(calls) == 1
        assert result["steps"][1]["found"] is False

    def test_partial_failure_keeps_found_elements(self):
        steps = [
            OPEN,
            _step("Input Text", description="username field", value="bob"),
            _step("Get Text", description="phantom banner"),
        ]
        run_tool, _ = self._tool_recorder(self._success_response({
            "elem_1": _mapping_entry(locator="id=username"),
            "elem_2": {"found": False, "error": "Element not found"},
        }))
        result = identify_elements(steps, "q", run_tool=run_tool)
        assert result["steps"][1]["locator"] == "id=username"
        assert result["steps"][2]["found"] is False

    def test_no_url_skips_tool_and_marks_placeholders(self):
        steps = [_step("Click", description="login button")]
        run_tool, calls = self._tool_recorder(self._success_response({}))
        result = identify_elements(steps, "login", run_tool=run_tool)
        assert calls == []
        assert result["steps"][0]["found"] is False

    def test_no_locator_steps_skips_tool(self):
        run_tool, calls = self._tool_recorder(self._success_response({}))
        result = identify_elements([OPEN, CLOSE], "open the page", run_tool=run_tool)
        assert calls == []
        assert [s["keyword"] for s in result["steps"]] == ["Open Browser", "Close Browser"]

    def test_result_is_json_serializable_steps_dict(self):
        import json
        steps = [OPEN, _step("Input Text", description="search box", value="shoes")]
        run_tool, _ = self._tool_recorder(self._success_response({
            "elem_1": _mapping_entry(locator="name=q"),
        }))
        result = identify_elements(steps, "q", run_tool=run_tool)
        json.dumps(result["steps"])  # must not raise

    def test_accepts_pydantic_planned_steps(self):
        """crew.py hands over PlanOutput.steps (PlannedStep models), not dicts."""
        from src.backend.crew_ai.tasks import PlannedStep
        steps = [
            PlannedStep(step_description="open", keyword="Open Browser", value="https://example.com"),
            PlannedStep(step_description="type", keyword="Input Text",
                        element_description="search box", value="shoes"),
        ]
        run_tool, calls = self._tool_recorder(self._success_response({
            "elem_1": _mapping_entry(locator="name=q"),
        }))
        result = identify_elements(steps, "q", run_tool=run_tool)
        assert calls[0]["elements"][0]["description"] == "search box"
        assert result["steps"][1]["locator"] == "name=q"

    def test_progress_callback_receives_stage_events(self):
        steps = [OPEN, _step("Input Text", description="search box", value="shoes")]
        run_tool, _ = self._tool_recorder(self._success_response({
            "elem_1": _mapping_entry(locator="name=q"),
        }, total=1))
        events = []
        identify_elements(steps, "q", run_tool=run_tool,
                          on_progress=lambda progress, message: events.append((progress, message)))
        progresses = [p for p, _ in events]
        assert progresses == sorted(progresses), "progress must move forward"
        assert 22 in progresses   # stage start
        assert 30 in progresses   # navigating / tool call
        assert 55 in progresses   # tool finished (element count)
        assert 60 in progresses   # stage complete
        count_msg = [m for p, m in events if p == 55][0]
        assert "1" in count_msg

    def test_progress_callback_errors_never_break_identification(self):
        steps = [OPEN, _step("Input Text", description="search box", value="shoes")]
        run_tool, _ = self._tool_recorder(self._success_response({
            "elem_1": _mapping_entry(locator="name=q"),
        }))

        def broken(progress, message):
            raise ValueError("SSE queue gone")

        result = identify_elements(steps, "q", run_tool=run_tool, on_progress=broken)
        assert result["steps"][1]["locator"] == "name=q"

    def test_summary_reports_counts(self):
        steps = [
            OPEN,
            _step("Input Text", description="username field", value="bob"),
            _step("Get Text", description="phantom banner"),
        ]
        run_tool, _ = self._tool_recorder(self._success_response({
            "elem_1": _mapping_entry(locator="id=username"),
            "elem_2": {"found": False, "error": "Element not found"},
        }))
        result = identify_elements(steps, "q", run_tool=run_tool)
        assert result["summary"]["total_elements"] == 2
        assert result["summary"]["found"] == 1
        assert result["summary"]["not_found"] == 1


# ─── merge robustness: raw-fallback steps with LLM key drift ─────────────────

class TestMergeRobustnessAgainstKeyDrift:
    """crew._extract_plan_steps' raw-JSON fallback can hand over dicts exactly
    as the LLM emitted them. Hallucinated locator-contract keys must never
    crash the merge (TypeError: multiple values for keyword argument) or leak
    into the output over the service's values."""

    def test_hallucinated_contract_keys_do_not_crash_found_path(self):
        steps = [
            OPEN,
            _step("Input Text", description="search box", value="shoes",
                  locator="id=WRONG", found=True, element_type="hallucinated",
                  stability="volatile", unexpected_key="junk"),
        ]
        merged = merge_locators(steps, {1: "elem_1"},
                                {"elem_1": _mapping_entry(locator="name=q")})
        # The service's contract wins; the LLM's fabrications are dropped.
        assert merged[1]["locator"] == "name=q"
        assert merged[1]["stability"] == "stable"
        assert "unexpected_key" not in merged[1]

    def test_hallucinated_contract_keys_do_not_crash_placeholder_path(self):
        steps = [OPEN, _step("Get Text", description="phantom",
                             locator="id=WRONG", found=True)]
        merged = merge_locators(steps, {1: "elem_1"},
                                {"elem_1": {"found": False}})
        assert merged[1]["found"] is False
        assert "locator" not in merged[1]

    def test_hallucinated_keys_on_non_locator_step_are_dropped(self):
        steps = [_step("Open Browser", value="https://example.com",
                       locator="id=WRONG", extra="junk")]
        merged = merge_locators(steps, {}, {})
        assert "locator" not in merged[0]
        assert "extra" not in merged[0]

    def test_found_without_best_locator_is_placeholder(self):
        """found:true with an empty best_locator is unusable — the single
        _entry_found criterion sends it down the placeholder path."""
        steps = [OPEN, _step("Get Text", description="banner")]
        merged = merge_locators(steps, {1: "elem_1"},
                                {"elem_1": {"found": True, "best_locator": ""}})
        assert merged[1]["found"] is False
        assert "locator" not in merged[1]


# ─── ASTPP flags: stapled only when True (Task 16 boundary, completed) ───────

class TestAstppFlagStapling:

    def _merge_one(self, entry):
        steps = [OPEN, _step("Click", description="edit link in first row")]
        return merge_locators(steps, {1: "elem_1"}, {"elem_1": entry})[1]

    def test_flags_stapled_when_true(self):
        step = self._merge_one(_mapping_entry(
            visibility_filtered=True, row_anchored=True, row_anchor_ambiguous=True,
        ))
        assert step["visibility_filtered"] is True
        assert step["row_anchored"] is True
        assert step["row_anchor_ambiguous"] is True

    def test_flags_absent_when_false(self):
        step = self._merge_one(_mapping_entry())  # all three default False
        assert "visibility_filtered" not in step
        assert "row_anchored" not in step
        assert "row_anchor_ambiguous" not in step

    def test_flags_absent_when_service_omits_them(self):
        entry = _mapping_entry()
        for key in ("visibility_filtered", "row_anchored", "row_anchor_ambiguous"):
            entry.pop(key)
        step = self._merge_one(entry)
        assert "row_anchored" not in step


# ─── honest stage telemetry ──────────────────────────────────────────────────

class TestStageCompletionMessages:

    def _events_for(self, steps, response):
        events = []

        def run_tool(elements, url, user_query):
            if isinstance(response, Exception):
                raise response
            return response

        identify_elements(steps, "q", run_tool=run_tool,
                          on_progress=lambda p, m: events.append((p, m)))
        return events

    def _final_message(self, events):
        return [m for p, m in events if p == 60][0]

    def test_all_found_reads_as_success(self):
        events = self._events_for(
            [OPEN, _step("Input Text", description="search box", value="x")],
            {"status": "success",
             "locator_mapping": {"elem_1": _mapping_entry(locator="name=q")},
             "summary": {}},
        )
        assert "All page elements identified" in self._final_message(events)

    def test_tool_failure_does_not_read_as_success(self):
        events = self._events_for(
            [OPEN, _step("Input Text", description="search box", value="x")],
            RuntimeError("service down"),
        )
        final = self._final_message(events)
        assert "0 of 1" in final
        assert "All page elements identified" not in final

    def test_partial_failure_reports_the_ratio(self):
        events = self._events_for(
            [OPEN,
             _step("Input Text", description="search box", value="x"),
             _step("Get Text", description="phantom banner")],
            {"status": "success",
             "locator_mapping": {"elem_1": _mapping_entry(locator="name=q"),
                                 "elem_2": {"found": False}},
             "summary": {}},
        )
        assert "1 of 2" in self._final_message(events)

    def test_no_elements_needed_message(self):
        events = self._events_for([OPEN, CLOSE], {"status": "success"})
        assert "No page elements needed" in self._final_message(events)
