"""
Locator Management Module

This module provides locator generation, validation, and extraction functionality
for browser automation. It uses Playwright's built-in methods for fast, reliable
locator validation without requiring large JavaScript code generation.

Key Components:
- generation: Generate locators from element attributes with priority ordering
- validation: Validate locators using Playwright API (uniqueness checking)
- extraction: Extract element attributes and validate locators in a pipeline

Priority Order (highest to lowest):
1. id - Most stable, fastest
2. data-testid - Designed for testing
3. name - Semantic, stable
4. aria-label - Accessibility, semantic
5. text - Content-based (can be fragile)
6. role - Playwright-specific, semantic
7. css-class - Lower priority, can change
"""

from .generation import generate_locators_from_attributes
from .validation import validate_locator_playwright
from .extraction import extract_element_attributes, extract_and_validate_locators

__all__ = [
    'generate_locators_from_attributes',
    'validate_locator_playwright',
    'extract_element_attributes',
    'extract_and_validate_locators',
]
