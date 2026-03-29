"""
Unit tests for src.backend.crew_ai.library_context.

Covers:
  - get_library_context factory function
  - BrowserLibraryContext properties and methods
  - SeleniumLibraryContext properties and methods
  - LibraryContext.get_full_context() dispatch
  - Abstract contract enforcement
"""

import pytest
from src.backend.crew_ai.library_context import (
    get_library_context,
    BrowserLibraryContext,
    SeleniumLibraryContext,
    LibraryContext,
)


class TestGetLibraryContextFactory:
    """Tests for the get_library_context factory function."""

    def test_browser_type_returns_browser_context(self):
        ctx = get_library_context("browser")
        assert isinstance(ctx, BrowserLibraryContext)

    def test_selenium_type_returns_selenium_context(self):
        ctx = get_library_context("selenium")
        assert isinstance(ctx, SeleniumLibraryContext)

    def test_case_insensitive_browser(self):
        ctx = get_library_context("Browser")
        assert isinstance(ctx, BrowserLibraryContext)

    def test_case_insensitive_selenium(self):
        ctx = get_library_context("SELENIUM")
        assert isinstance(ctx, SeleniumLibraryContext)

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

    def test_validation_context_is_string(self, ctx):
        assert isinstance(ctx.validation_context, str)

    def test_validation_context_warns_about_selenium_keywords(self, ctx):
        context = ctx.validation_context
        # Should mention that SeleniumLibrary keywords are invalid
        assert "Input Text" in context or "Open Browser" in context

    def test_get_full_context_planner(self, ctx):
        context = ctx.get_full_context("planner")
        assert isinstance(context, str)
        assert len(context) > 0

    def test_get_full_context_assembler(self, ctx):
        context = ctx.get_full_context("assembler")
        assert isinstance(context, str)
        assert len(context) > 0

    def test_get_full_context_validator(self, ctx):
        context = ctx.get_full_context("validator")
        assert isinstance(context, str)
        assert len(context) > 0

    def test_get_full_context_unknown_role_raises(self, ctx):
        with pytest.raises(ValueError, match="Unknown agent role"):
            ctx.get_full_context("unknown_role")


class TestSeleniumLibraryContext:
    """Tests for SeleniumLibraryContext."""

    @pytest.fixture
    def ctx(self):
        return SeleniumLibraryContext()

    def test_library_name_contains_selenium(self, ctx):
        assert "Selenium" in ctx.library_name or "selenium" in ctx.library_name.lower()

    def test_library_import_is_string(self, ctx):
        assert isinstance(ctx.library_import, str)
        assert len(ctx.library_import) > 0

    def test_browser_init_params_is_dict(self, ctx):
        params = ctx.browser_init_params
        assert isinstance(params, dict)

    def test_requires_viewport_config_is_bool(self, ctx):
        assert isinstance(ctx.requires_viewport_config, bool)

    def test_viewport_config_code_is_string(self, ctx):
        code = ctx.get_viewport_config_code()
        assert isinstance(code, str)

    def test_core_rules_is_non_empty_string(self, ctx):
        rules = ctx.core_rules
        assert isinstance(rules, str)
        assert len(rules) > 0

    def test_planning_rules_is_non_empty_string(self, ctx):
        rules = ctx.planning_rules
        assert isinstance(rules, str)
        assert len(rules) > 0

    def test_planning_context_is_string(self, ctx):
        assert isinstance(ctx.planning_context, str)

    def test_code_assembly_context_is_string(self, ctx):
        assert isinstance(ctx.code_assembly_context, str)

    def test_validation_context_is_string(self, ctx):
        assert isinstance(ctx.validation_context, str)

    def test_get_full_context_all_roles(self, ctx):
        for role in ("planner", "assembler", "validator"):
            result = ctx.get_full_context(role)
            assert isinstance(result, str)
            assert len(result) > 0


class TestLibraryContextContract:
    """Verify the abstract contract is enforced."""

    def test_cannot_instantiate_base_class(self):
        with pytest.raises(TypeError):
            LibraryContext()

    def test_browser_context_is_subclass(self):
        assert issubclass(BrowserLibraryContext, LibraryContext)

    def test_selenium_context_is_subclass(self):
        assert issubclass(SeleniumLibraryContext, LibraryContext)

    def test_both_contexts_have_different_library_names(self):
        browser = BrowserLibraryContext()
        selenium = SeleniumLibraryContext()
        assert browser.library_name != selenium.library_name
