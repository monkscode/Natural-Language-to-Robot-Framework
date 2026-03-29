"""
Tests for ContextPruner — keyword category mapping and pruning.

Validates:
- KEYWORD_CATEGORIES covers all expected categories
- prune_keywords correctly filters by category
- get_pruning_stats returns correct calculations
"""

import pytest

from src.backend.crew_ai.optimization.context_pruner import ContextPruner


@pytest.fixture
def pruner():
    """Create a ContextPruner instance."""
    return ContextPruner()


# ===================================================================
# Category 1: KEYWORD_CATEGORIES Consistency
# ===================================================================

class TestKeywordCategories:

    def test_all_expected_categories_present(self):
        """KEYWORD_CATEGORIES contains all six expected action categories."""
        expected = {"navigation", "input", "interaction", "extraction", "assertion", "wait"}
        assert set(ContextPruner.KEYWORD_CATEGORIES.keys()) == expected

    def test_every_category_has_keywords(self):
        """No category is empty."""
        for cat, keywords in ContextPruner.KEYWORD_CATEGORIES.items():
            assert len(keywords) > 0, f"Category '{cat}' has no keywords"

    def test_no_duplicate_keywords_within_category(self):
        """No keyword appears twice within the same category."""
        for cat, keywords in ContextPruner.KEYWORD_CATEGORIES.items():
            assert len(keywords) == len(set(keywords)), (
                f"Category '{cat}' has duplicate keywords"
            )


# ===================================================================
# Category 2: Keyword Pruning
# ===================================================================

class TestPruneKeywords:

    def test_filters_to_matched_categories(self, pruner):
        """prune_keywords keeps only keywords in the given categories."""
        all_kws = [
            {"name": "Click"},         # interaction
            {"name": "Fill Text"},     # input
            {"name": "Go To"},         # navigation
        ]
        pruned = pruner.prune_keywords(all_kws, ["interaction"])
        names = [kw["name"] for kw in pruned]
        assert "Click" in names
        assert "Fill Text" not in names
        assert "Go To" not in names

    def test_multiple_categories(self, pruner):
        """prune_keywords with multiple categories keeps keywords from both."""
        all_kws = [
            {"name": "Click"},         # interaction
            {"name": "Fill Text"},     # input
            {"name": "Go To"},         # navigation
        ]
        pruned = pruner.prune_keywords(all_kws, ["interaction", "input"])
        names = [kw["name"] for kw in pruned]
        assert "Click" in names
        assert "Fill Text" in names
        assert "Go To" not in names

    def test_all_categories_returns_everything(self, pruner):
        """Passing all categories should return all keywords (fallback behaviour)."""
        all_kws = [
            {"name": "Click"},
            {"name": "Fill Text"},
            {"name": "Go To"},
        ]
        all_cats = list(ContextPruner.KEYWORD_CATEGORIES.keys())
        pruned = pruner.prune_keywords(all_kws, all_cats)
        assert len(pruned) == len(all_kws)

    def test_empty_keywords(self, pruner):
        """Empty keyword list produces empty result."""
        pruned = pruner.prune_keywords([], ["interaction"])
        assert pruned == []

    def test_unknown_category_ignored(self, pruner):
        """Unknown category name is safely ignored."""
        all_kws = [{"name": "Click"}]
        pruned = pruner.prune_keywords(all_kws, ["nonexistent_category"])
        assert pruned == []

    def test_keyword_not_in_any_category(self, pruner):
        """A keyword not listed in any category is excluded regardless."""
        all_kws = [{"name": "Custom Keyword Not In Categories"}]
        all_cats = list(ContextPruner.KEYWORD_CATEGORIES.keys())
        pruned = pruner.prune_keywords(all_kws, all_cats)
        assert pruned == []


# ===================================================================
# Category 3: Pruning Stats
# ===================================================================

class TestPruningStats:

    def test_normal_stats(self, pruner):
        """get_pruning_stats returns correct calculation."""
        stats = pruner.get_pruning_stats(original_count=10, pruned_count=4)
        assert stats["original_count"] == 10
        assert stats["pruned_count"] == 4
        assert stats["retention_rate"] == 0.4
        assert stats["reduction_rate"] == 0.6
        assert stats["reduction_percentage"] == 60.0

    def test_zero_input(self, pruner):
        """get_pruning_stats handles zero input without division error."""
        stats = pruner.get_pruning_stats(original_count=0, pruned_count=0)
        assert stats["retention_rate"] == 0.0
        assert stats["reduction_percentage"] == 0.0

    def test_no_reduction(self, pruner):
        """When nothing is pruned, reduction is 0%."""
        stats = pruner.get_pruning_stats(original_count=5, pruned_count=5)
        assert stats["retention_rate"] == 1.0
        assert stats["reduction_percentage"] == 0.0

    def test_full_reduction(self, pruner):
        """When everything is pruned, reduction is 100%."""
        stats = pruner.get_pruning_stats(original_count=5, pruned_count=0)
        assert stats["retention_rate"] == 0.0
        assert stats["reduction_percentage"] == 100.0
