"""
Unit tests for src.backend.crew_ai.library_context.

Covers:
  - get_library_context factory function (browser-only since Task 11/E8)
  - BrowserLibraryContext properties and methods
  - LibraryContext.get_full_context() dispatch
  - Abstract contract enforcement
"""

import pytest
from src.backend.crew_ai.library_context import (
    get_library_context,
    BrowserLibraryContext,
    LibraryContext,
)


class TestGetLibraryContextFactory:
    """Tests for the get_library_context factory function."""

    def test_browser_type_returns_browser_context(self):
        ctx = get_library_context("browser")
        assert isinstance(ctx, BrowserLibraryContext)

    def test_case_insensitive_browser(self):
        ctx = get_library_context("Browser")
        assert isinstance(ctx, BrowserLibraryContext)

    def test_selenium_raises_value_error(self):
        """SeleniumLibrary support was removed (Task 11/E8) — the factory
        rejects it like any other unknown library."""
        with pytest.raises(ValueError, match="Unknown library type"):
            get_library_context("selenium")

    def test_unknown_type_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown library type"):
            get_library_context("playwright")

    def test_empty_string_raises_value_error(self):
        with pytest.raises(ValueError):
            get_library_context("")


class TestBrowserLibraryContext:
    """Tests for BrowserLibraryContext."""

    @pytest.fixture
    def ctx(self):
        return BrowserLibraryContext()

    def test_library_name(self, ctx):
        assert ctx.library_name == "Browser"

    def test_library_import(self, ctx):
        assert "Browser" in ctx.library_import

    def test_browser_init_params_has_browser(self, ctx):
        params = ctx.browser_init_params
        assert "browser" in params
        assert params["browser"] == "chromium"

    def test_browser_init_params_has_headless(self, ctx):
        params = ctx.browser_init_params
        assert "headless" in params

    def test_requires_viewport_config_is_true(self, ctx):
        assert ctx.requires_viewport_config is True

    def test_viewport_config_code_contains_new_context(self, ctx):
        code = ctx.get_viewport_config_code()
        assert "New Context" in code
        assert "viewport=None" in code

    def test_core_rules_not_empty(self, ctx):
        rules = ctx.core_rules
        assert isinstance(rules, str)
        assert len(rules) > 50

    def test_core_rules_mentions_viewport(self, ctx):
        assert "viewport" in ctx.core_rules.lower()

    def test_core_rules_mentions_new_browser(self, ctx):
        assert "New Browser" in ctx.core_rules

    def test_planning_rules_not_empty(self, ctx):
        rules = ctx.planning_rules
        assert isinstance(rules, str)
        assert len(rules) > 10

    def test_planning_context_is_string(self, ctx):
        assert isinstance(ctx.planning_context, str)

    def test_planning_context_caching(self, ctx):
        """Two accesses return the same object (caching)."""
        first = ctx.planning_context
        second = ctx.planning_context
        assert first is second

    def test_code_assembly_context_is_string(self, ctx):
        assert isinstance(ctx.code_assembly_context, str)

    def test_code_assembly_context_mentions_new_browser(self, ctx):
        assert "New Browser" in ctx.code_assembly_context

    def test_code_assembly_context_caching(self, ctx):
        first = ctx.code_assembly_context
        second = ctx.code_assembly_context
        assert first is second

    def test_code_assembly_context_includes_tom_select_template(self, ctx):
        """Phase 3.1 — Tom Select interaction template must be present so the
        Code Assembler agent can route ``dropdown_framework='tom-select'``
        elements to the id-anchored / positional templates from
        docs/ELEMENT_TYPE_CLASSIFIER_ARCHITECTURE.md Section 6."""
        ctx_str = ctx.code_assembly_context
        assert "TOM SELECT INTERACTION" in ctx_str
        assert "tom-select" in ctx_str
        # Preferred path: Evaluate JavaScript targeting id=${select_id} (the hidden <select>).
        assert "Evaluate JavaScript" in ctx_str
        assert "id=${select_id}" in ctx_str
        assert "el.tomselect.setValue" in ctx_str
        # Fallback path: class-based lookup via select.tomselected — position-independent.
        assert "closest('.ts-wrapper').parentElement.querySelector('select.tomselected')" in ctx_str
        assert "if (sel && sel.tomselect)" in ctx_str
        # Old click-chain class selectors must be absent.
        assert ".ts-option" not in ctx_str

    def test_get_full_context_planner(self, ctx):
        context = ctx.get_full_context("planner")
        assert isinstance(context, str)
        assert len(context) > 0

    def test_get_full_context_assembler(self, ctx):
        context = ctx.get_full_context("assembler")
        assert isinstance(context, str)
        assert len(context) > 0

    def test_get_full_context_unknown_role_raises(self, ctx):
        with pytest.raises(ValueError, match="Unknown agent role"):
            ctx.get_full_context("unknown_role")


class TestLibraryContextContract:
    """Verify the abstract contract is enforced."""

    def test_cannot_instantiate_base_class(self):
        with pytest.raises(TypeError):
            LibraryContext()

    def test_browser_context_is_subclass(self):
        assert issubclass(BrowserLibraryContext, LibraryContext)
