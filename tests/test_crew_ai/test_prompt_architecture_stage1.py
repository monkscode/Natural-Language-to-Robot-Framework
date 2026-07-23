"""
TASK-24R Stage 1 target-state tests: prompt dedup + de-contradiction +
keyword_search tool retirement.

Evidence base (plan: docs/superpowers/plans/locator-enhancement/
TASK-24R-prompt-architecture.md, findings F1-F9 verified against full prompt
dumps of the 30 official 2026-07-11 bench runs):
  - F1: code_assembly_context shipped TWICE per assembler request (agent
    backstory + task description) — the description copy goes.
  - F3: contradictory output contracts — the backstory's raw-code
    "CODE PRINTER" manifesto vs the description's {"code"} JSON rules.
    The model chose JSON 30/30; the manifesto goes.
  - F4: wrong-library ghosts (Selenium-era ${options}/Open Browser/
    Click Element/Input Text examples; planner "Click search button"
    against its own Enter rule).
  - F8: LOCATOR_USAGE_RULES defined but referenced nowhere.
  - F9: planner backstory duplicates EXPLICIT_ELEMENTS_ONLY_RULES; hint
    preamble sent even when hints are absent.
  - keyword_search tool RETIRED (owner-approved 2026-07-11, reverses the
    standing "tool stays" decision): 0 invocations across 15,987 traced
    calls, unreachable by construction (prompts forbid the ReAct syntax
    CrewAI tools require), ~700 t/run production-only cost. The metrics
    FIELD keyword_search_stats stays (historical rows carry it); only the
    dead track_keyword_search writer goes.
"""

import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.backend.crew_ai.prompts.components import PromptComponents
from src.backend.crew_ai.library_context import BrowserLibraryContext


def _module_source(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


def _make_agents(library_context=None):
    """RobotAgents with spec'd LLM mocks (test_agents.py pattern)."""
    from src.backend.crew_ai.agents import RobotAgents
    from src.backend.crew_ai.cleaned_llm_wrapper import CleanedLLMWrapper

    plain_llm = MagicMock(name="plain", spec=CleanedLLMWrapper)
    planner_llm = MagicMock(name="planner", spec=CleanedLLMWrapper)
    plain_llm._monitor = MagicMock(name="monitor")
    plain_llm._token_usage = {}
    with patch("src.backend.crew_ai.agents.get_llm",
               side_effect=[plain_llm, planner_llm]):
        return RobotAgents("vertex", "gemini-2.5-flash",
                           library_context=library_context)


def _capture_plan_description(hint_context=None):
    from src.backend.crew_ai.tasks import RobotTasks
    tasks = RobotTasks(library_context=BrowserLibraryContext(),
                       hint_context=hint_context)
    with patch("src.backend.crew_ai.tasks.Task") as mock_task:
        tasks.plan_steps_task(agent=MagicMock(), query="click the button")
    return mock_task.call_args.kwargs["description"]


def _capture_assemble_description(hint_context=None):
    from src.backend.crew_ai.tasks import RobotTasks
    tasks = RobotTasks(library_context=BrowserLibraryContext(),
                       hint_context=hint_context)
    with patch("src.backend.crew_ai.tasks.Task") as mock_task:
        tasks.assemble_code_task(agent=MagicMock(),
                                 identified_steps_json='{"steps": []}')
    return mock_task.call_args.kwargs["description"]


# ═══════════════════════════════════════════════════════════════════════════
# keyword_search retirement — tool gone, no mention anywhere, plumbing out
# ═══════════════════════════════════════════════════════════════════════════

class TestKeywordSearchRetirement:

    def test_tool_module_deleted(self):
        import src.backend.crew_ai.optimization as opt
        tool_path = Path(opt.__file__).parent / "keyword_search_tool.py"
        assert not tool_path.exists(), (
            "keyword_search_tool.py must be deleted (0 invocations ever; "
            "unreachable by construction)"
        )

    def test_optimization_package_does_not_export_tool(self):
        import src.backend.crew_ai.optimization as opt
        assert "KeywordSearchTool" not in getattr(opt, "__all__", [])
        assert not hasattr(opt, "KeywordSearchTool")

    def test_no_keyword_search_mention_in_prompt_or_wiring_modules(self):
        """The string may survive ONLY as the kept metrics field name
        (keyword_search_stats, workflow_service/models/frontend) — none of
        the prompt/wiring modules below carry that field."""
        import src.backend.crew_ai.agents as agents_mod
        import src.backend.crew_ai.tasks as tasks_mod
        import src.backend.crew_ai.crew as crew_mod
        import src.backend.crew_ai.prompts.components as components_mod
        import src.backend.crew_ai.library_context.browser_context as bc_mod
        import src.backend.crew_ai.optimization.smart_keyword_provider as skp_mod
        import src.backend.crew_ai.optimization.keyword_vector_store as kvs_mod
        import src.backend.services.dryrun_service as dryrun_mod

        for mod in (agents_mod, tasks_mod, crew_mod, components_mod, bc_mod,
                    skp_mod, kvs_mod, dryrun_mod):
            assert "keyword_search" not in _module_source(mod).lower(), (
                f"{mod.__name__} still mentions keyword_search"
            )

    def test_robot_agents_signature_has_no_tool_param(self):
        from src.backend.crew_ai.agents import RobotAgents
        params = inspect.signature(RobotAgents.__init__).parameters
        assert "keyword_search_tool" not in params

    def test_assembler_agent_carries_no_tools(self):
        agents = _make_agents(library_context=BrowserLibraryContext())
        assembler = agents.code_assembler_agent()
        assert list(assembler.tools) == []

    def test_provider_zero_context_is_core_rules_only(self):
        """Tier-2 zero-context mode was built entirely around teaching the
        Action:-dance for the tool — rewritten to core-rules-only, same tier
        structure (predicted → zero → full fallback)."""
        from src.backend.crew_ai.optimization.smart_keyword_provider import (
            SmartKeywordProvider,
        )
        provider = SmartKeywordProvider(
            library_context=BrowserLibraryContext(),
            pattern_matcher=MagicMock(),
            vector_store=MagicMock(),
        )
        assert not hasattr(provider, "_format_zero_context_with_tool")
        context = provider._format_zero_context("assembler")
        assert "CORE RULES" in context
        assert "keyword_search" not in context.lower()
        assert "Action:" not in context

    def test_provider_has_no_tool_factory(self):
        from src.backend.crew_ai.optimization.smart_keyword_provider import (
            SmartKeywordProvider,
        )
        assert not hasattr(SmartKeywordProvider, "get_keyword_search_tool")

    def test_metrics_writer_gone_but_field_kept(self):
        """track_keyword_search's sole caller was the tool. The stats FIELD
        stays — historical workflow_metrics rows carry it and the frontend
        column reads it; all recorded values are already zero."""
        from src.backend.core.models.workflow_metrics_models import (
            WorkflowMetrics,
        )
        assert not hasattr(WorkflowMetrics, "track_keyword_search")
        assert "keyword_search_stats" in WorkflowMetrics.model_fields


# ═══════════════════════════════════════════════════════════════════════════
# Assembler backstory — single {"code"} contract (F3), no manifesto
# ═══════════════════════════════════════════════════════════════════════════

class TestAssemblerBackstory:

    def _backstory(self):
        agents = _make_agents(library_context=BrowserLibraryContext())
        return agents.code_assembler_agent().backstory

    def test_states_the_json_code_contract(self):
        assert '{"code"' in self._backstory()

    def test_raw_code_manifesto_deleted(self):
        """The 'start IMMEDIATELY with *** Settings ***' manifesto described
        a contract that never won (model chose JSON 30/30) and contradicted
        ASSEMBLY_OUTPUT_RULES."""
        backstory = self._backstory()
        assert "CODE PRINTER" not in backstory
        assert "Starts IMMEDIATELY with *** Settings ***" not in backstory
        assert "Any text before *** Settings ***" not in backstory

    def test_still_carries_library_knowledge(self):
        """code_assembly_context stays in the backstory (system) — the
        description's duplicate copy is the one that goes (F1)."""
        assert "BROWSER LIBRARY CODE STRUCTURE" in self._backstory()


# ═══════════════════════════════════════════════════════════════════════════
# Assembler description — F1 dedup, dead blocks gone, merged locator rules
# ═══════════════════════════════════════════════════════════════════════════

class TestAssemblerDescription:

    def test_code_assembly_context_not_duplicated(self):
        """F1: ~1,130 t of code_assembly_context shipped twice per request.
        Learning-ON was WORSE — optimized context replaced the backstory
        copy but the description still shipped the static one."""
        desc = _capture_assemble_description()
        assert "BROWSER LIBRARY CODE STRUCTURE" not in desc
        assert "MANDATORY STRUCTURE" not in desc

    def test_viewport_block_not_duplicated(self):
        """The viewport rule lives ONCE, in code_assembly_context (system).
        The description's dedicated viewport block (~13 repetitions of the
        rule across the request) goes."""
        desc = _capture_assemble_description()
        assert "--- VIEWPORT CONFIGURATION" not in desc

    def test_single_output_rules_block(self):
        desc = _capture_assemble_description()
        assert desc.count("OUTPUT JSON FORMAT") == 1
        assert "--- OUTPUT FORMAT ---" not in desc  # ASSEMBLY_FORMAT_RULES dup

    def test_merged_locator_rules_wired(self):
        desc = _capture_assemble_description()
        assert "LOCATOR RULES" in desc
        # merged semantics preserved: exact-copy + found:false placeholder
        assert "PLACEHOLDER_FOR" in desc

    def test_steps_json_still_embedded(self):
        desc = _capture_assemble_description()
        assert '{"steps": []}' in desc


class TestRobotTasksDeadCaches:

    def test_description_side_caches_removed(self):
        """_cached_code_structure/_cached_viewport existed only to re-ship
        system-prompt content into the description (F1) — gone with their
        builder methods."""
        from src.backend.crew_ai.tasks import RobotTasks
        tasks = RobotTasks(library_context=BrowserLibraryContext())
        assert not hasattr(tasks, "_cached_code_structure")
        assert not hasattr(tasks, "_cached_viewport")
        assert not hasattr(tasks, "_get_code_structure_template")
        assert not hasattr(tasks, "_get_viewport_instructions")


# ═══════════════════════════════════════════════════════════════════════════
# PromptComponents — merged LOCATOR_RULES, dead components deleted, F4 ghosts
# ═══════════════════════════════════════════════════════════════════════════

class TestLocatorRulesMerge:

    def test_dead_and_merged_components_deleted(self):
        assert not hasattr(PromptComponents, "LOCATOR_USAGE_RULES")   # F8
        assert not hasattr(PromptComponents, "ASSEMBLY_FORMAT_RULES")
        assert not hasattr(PromptComponents, "USE_PROVIDED_LOCATORS_RULES")
        assert not hasattr(PromptComponents, "LOCATOR_MAPPING_RULES")

    def test_locator_rules_keeps_exact_copy_semantics(self):
        body = PromptComponents.LOCATOR_RULES
        assert "EXACT" in body
        assert "DO NOT modify" in body

    def test_locator_rules_keeps_found_false_placeholder_contract(self):
        """Task 12 contract: found=false → WARNING comment + PLACEHOLDER_FOR
        locator + still-valid code."""
        body = PromptComponents.LOCATOR_RULES
        assert "PLACEHOLDER_FOR" in body
        assert "# WARNING: Locator not found" in body

    def test_locator_rules_has_no_selenium_ghosts(self):
        assert "Input Text" not in PromptComponents.LOCATOR_RULES


class TestVariableDeclarationBrowserOnly:
    """F4: the block taught ${options} + Open Browser + add_argument
    (Selenium era) while three other blocks forbid 'options'."""

    def test_selenium_ghosts_gone(self):
        body = PromptComponents.VARIABLE_DECLARATION_RULES
        assert "options" not in body
        assert "Open Browser" not in body
        assert "add_argument" not in body

    def test_teaches_browser_library_variables(self):
        body = PromptComponents.VARIABLE_DECLARATION_RULES
        assert "${browser}" in body
        assert "${headless}" in body


class TestCheckboxRadioConcreteSyntax:
    """Retirement follow-through: the block's only instruction was 'ask the
    keyword_search tool'. Replaced with the libdoc-verified syntax:
    Check Checkbox(selector, force: bool = False) — 'checks the checkbox or
    selects radio button'; Click has NO force param."""

    def test_uses_check_checkbox_force(self):
        body = PromptComponents.CHECKBOX_RADIO_HANDLING
        assert "Check Checkbox    ${locator}    force=True" in body

    def test_covers_uncheck(self):
        assert "Uncheck Checkbox" in PromptComponents.CHECKBOX_RADIO_HANDLING

    def test_still_routes_on_element_type(self):
        body = PromptComponents.CHECKBOX_RADIO_HANDLING
        assert "radio" in body
        assert "checkbox" in body


class TestCollectionCardinalityRules:
    """The block is now gated on element_type == "collection" too, so it has
    to answer the question that gating raises: what do you do with a locator
    that matches many when the planner asked for a singular keyword?

    Verified against the runner image's own libdoc (Browser Library):
      Get Text          -> resolves strictly, one element
      Get Attribute     -> resolves strictly, one element
      Get Elements      -> returns the list (the collection read)
      Get Element Count -> takes a multi-match selector BY DESIGN
    """

    def test_names_the_strict_resolving_keywords(self):
        body = PromptComponents.LOOP_HANDLING
        assert "strict mode violation" in body
        assert "Get Text" in body
        assert "Get Attribute" in body

    def test_points_collections_at_get_elements(self):
        body = PromptComponents.LOOP_HANDLING
        assert "Get Elements" in body

    def test_does_not_forbid_get_element_count(self):
        """Get Element Count is CORRECT on a many-match selector — it counts
        them. A blanket 'never point a singular keyword at a collection' rule
        would push the assembler off the one keyword that is right here."""
        body = PromptComponents.LOOP_HANDLING
        assert "Get Element Count" in body

    def test_does_not_invent_get_texts(self):
        """`Get Texts` does NOT exist in Browser Library (checked against the
        runner image's libdoc). It is the obvious wrong guess for a bulk read,
        so the guidance must never suggest it."""
        assert "Get Texts" not in PromptComponents.LOOP_HANDLING


class TestGetSelectedOptionsReturnShape:
    """Same family as the collection-cardinality bug: the assembler guessing a
    Browser Library keyword's contract instead of being told it.

    Verified against the runner image's own libdoc (Browser 19.14.2):
      Get Selected Options(selector, option_attribute=label, assertion_operator=None,
                           *assertion_expected, message=None) -> list[str | int]

    It returns that ATTRIBUTE's values — a flat list of strings. Three of the nine
    bench runs that used the keyword assumed a list of objects instead:
    `${sel}[0][label]`, `Get From Dictionary ${sel}[0] text`, and
    `Get From Dictionary ${sel}[0] label`. `--dryrun` cannot catch any of them —
    it never evaluates variables.
    """

    def test_states_the_return_shape(self):
        body = PromptComponents.DROPDOWN_HANDLING
        assert "Get Selected Options" in body
        assert "list of strings" in body

    def test_rules_out_dict_access_in_every_observed_form(self):
        """Naming only the `[0][label]` index form would miss two of the three
        real failures, which reached for Get From Dictionary instead."""
        body = PromptComponents.DROPDOWN_HANDLING
        assert "[label]" in body
        assert "Get From Dictionary" in body

    def test_keeps_the_keywords_own_assertion_idiom_legal(self):
        """Get Selected Options takes an assertion_operator. Guidance must state
        the shape, not ban the assertion form the keyword ships with."""
        body = PromptComponents.DROPDOWN_HANDLING
        assert "==" in body

    def test_guidance_is_not_in_the_always_on_context(self):
        """library_context is the assembler's always-on system prompt. This fact
        costs tokens only on dropdown runs, matching the Task 24R F1/F2 dedup that
        moved the Tom Select recipe out of that context and into this block."""
        from src.backend.crew_ai.library_context import get_library_context

        assert "Get Selected Options" not in get_library_context("browser").code_assembly_context


class TestWrongLibraryGhosts:

    def test_loop_examples_use_browser_click(self):
        body = PromptComponents.LOOP_HANDLING
        assert "Click Element" not in body
        assert "Click" in body

    def test_conditional_example_uses_fill_text(self):
        body = PromptComponents.CONDITIONAL_LOGIC_HANDLING
        assert "Input Text" not in body
        assert "Fill Text" in body


# ═══════════════════════════════════════════════════════════════════════════
# Planner — F9 backstory dedup, conditional hint preamble, F4 example fix
# ═══════════════════════════════════════════════════════════════════════════

class TestPlannerPrompt:

    def test_backstory_duplicate_rule_list_removed(self):
        """The 8-rule CRITICAL RULES list duplicated
        EXPLICIT_ELEMENTS_ONLY_RULES (~400 t) — rules live once, in the
        description component."""
        agents = _make_agents(library_context=BrowserLibraryContext())
        backstory = agents.step_planner_agent().backstory
        assert "CRITICAL RULES:" not in backstory
        assert "DO NOT add popup dismissal" not in backstory
        # library planning guidance stays (it is not duplicated anywhere)
        assert "PLANNING CONSIDERATIONS" in backstory

    def test_description_keeps_explicit_only_rules(self):
        desc = _capture_plan_description()
        assert "ONLY EXPLICIT ELEMENTS" in desc

    def test_hint_preamble_absent_without_hints(self):
        """F9: 'USER FEEDBACK blocks above' shipped even when no hints
        existed (always, in bench). Conditional on hints now."""
        desc = _capture_plan_description(hint_context=None)
        assert "USER FEEDBACK blocks above" not in desc

    def test_hint_preamble_present_with_hints(self):
        desc = _capture_plan_description(
            hint_context={"planner": "⚠️ USER FEEDBACK: do the thing"})
        assert "USER FEEDBACK blocks above" in desc
        assert "do the thing" in desc

    def test_decomposition_example_matches_enter_rule(self):
        """F4: the Decomposition example said 'Click search button' while
        rule 7 mandates Press Keys + Enter for search."""
        desc = _capture_plan_description()
        assert "Click search button" not in desc


# ═══════════════════════════════════════════════════════════════════════════
# browser_context — Tom Select out of code_assembly_context, tool refs gone
# ═══════════════════════════════════════════════════════════════════════════

class TestCodeAssemblyContextSlimming:

    def test_tom_select_lives_only_in_dropdown_block(self):
        """The Tom Select JS recipe appeared ~8x per assembler request via
        the double-shipped context. It belongs to DROPDOWN_HANDLING."""
        cac = BrowserLibraryContext().code_assembly_context
        assert "TOM SELECT" not in cac
        assert "tomselect" not in cac
        assert "tomselect" in PromptComponents.DROPDOWN_HANDLING

    def test_keyword_reference_keeps_common_keywords(self):
        cac = BrowserLibraryContext().code_assembly_context
        assert "Common keywords:" in cac
        assert "keyword_search" not in cac
    # NOTE: the viewport=None pin (q03 fix) lives in test_tasks_viewport.py,
    # retargeted to code_assembly_context — the rule's one remaining home.
