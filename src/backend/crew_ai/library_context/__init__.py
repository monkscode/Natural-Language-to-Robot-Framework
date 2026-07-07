"""
Library Context System for Dynamic Robot Framework Code Generation.

This module provides library-specific context (syntax, examples, keywords) to AI agents,
allowing them to dynamically generate code without hardcoding keywords or syntax.

Browser Library (Playwright) is the only supported target (Task 11/E8) — the
locator pipeline emits Playwright-only syntax, so SeleniumLibrary support was
removed. The factory shape is kept so a future second library is an emission-layer
feature, not a rewrite.
"""

from .base import LibraryContext
from .browser_context import BrowserLibraryContext

__all__ = [
    "LibraryContext",
    "BrowserLibraryContext",
    "get_library_context"
]


def get_library_context(library_type: str) -> LibraryContext:
    """
    Factory function to get the appropriate library context.

    Args:
        library_type: "browser" (the only supported library)

    Returns:
        LibraryContext instance for the specified library

    Example:
        >>> context = get_library_context("browser")
        >>> print(context.library_import)
        Library    Browser
    """
    if library_type.lower() == "browser":
        return BrowserLibraryContext()
    raise ValueError(f"Unknown library type: {library_type}. Only 'browser' is supported")
