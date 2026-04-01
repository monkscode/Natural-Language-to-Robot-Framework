"""
Context Pruner for Smart Keyword Filtering

This module prunes keyword context to include only relevant categories,
reducing token usage while maintaining code generation accuracy.

Category selection is done via reverse-lookup from predicted keywords
(see SmartKeywordProvider._format_predicted_context). ChromaDB is not
needed — the KEYWORD_CATEGORIES dict is the single source of truth.
"""

import logging
from typing import List, Dict

logger = logging.getLogger(__name__)


class ContextPruner:
    """
    Prune context to relevant keyword categories.

    Maps Robot Framework keywords to action categories (navigation, input,
    interaction, extraction, assertion, wait) and filters keyword lists to
    only those in the requested categories.
    """

    # Keyword category mappings
    KEYWORD_CATEGORIES = {
        "navigation": [
            "New Browser", "New Page", "Go To", "Go Back", "Go Forward",
            "Close Browser", "Close Page", "Switch Page", "New Context"
        ],
        "input": [
            "Fill Text", "Input Text", "Type Text", "Press Keys",
            "Upload File", "Type Secret", "Clear Text"
        ],
        "interaction": [
            "Click", "Click Element", "Hover", "Drag And Drop",
            "Select Options By", "Check Checkbox", "Uncheck Checkbox"
        ],
        "extraction": [
            "Get Text", "Get Attribute", "Get Element Count",
            "Get Property", "Get Style", "Get Url", "Get Title"
        ],
        "assertion": [
            "Should Be Equal", "Should Contain", "Should Be Visible",
            "Should Not Be Visible", "Should Be Enabled", "Should Be Disabled"
        ],
        "wait": [
            "Wait For Elements State", "Wait Until Element Is Visible",
            "Wait For Condition", "Wait For Load State", "Sleep"
        ]
    }

    def prune_keywords(
        self,
        all_keywords: List[Dict],
        categories: List[str]
    ) -> List[Dict]:
        """
        Filter keywords to only those in relevant categories.

        Args:
            all_keywords: List of keyword dicts with 'name' field
            categories: List of relevant category names

        Returns:
            Filtered list of keyword dicts
        """
        logger.debug(f"Pruning keywords for categories: {categories}")

        # Build set of relevant keyword names
        relevant_names = set()
        for category in categories:
            if category in self.KEYWORD_CATEGORIES:
                relevant_names.update(self.KEYWORD_CATEGORIES[category])

        # Filter keywords
        pruned = [
            kw for kw in all_keywords
            if kw.get("name") in relevant_names
        ]

        if all_keywords:
            logger.info(f"Pruned {len(all_keywords)} keywords to {len(pruned)} ({len(pruned)/len(all_keywords)*100:.1f}% retained)")
        else:
            logger.info("No keywords to prune (empty input)")

        return pruned

    def get_pruning_stats(
        self,
        original_count: int,
        pruned_count: int
    ) -> Dict[str, float]:
        """
        Calculate pruning statistics.

        Args:
            original_count: Number of keywords before pruning
            pruned_count: Number of keywords after pruning

        Returns:
            Dict with original_count, pruned_count, retention_rate, reduction_rate, and reduction_percentage
        """
        if original_count == 0:
            return {
                "original_count": 0,
                "pruned_count": 0,
                "retention_rate": 0.0,
                "reduction_rate": 0.0,
                "reduction_percentage": 0.0
            }

        retention = pruned_count / original_count
        reduction = 1.0 - retention

        return {
            "original_count": original_count,
            "pruned_count": pruned_count,
            "retention_rate": retention,
            "reduction_rate": reduction,
            "reduction_percentage": reduction * 100
        }
