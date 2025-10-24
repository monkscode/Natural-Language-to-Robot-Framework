"""
Library Context System for Dynamic Robot Framework Code Generation.

This module provides library-specific context (syntax, examples, keywords) to AI agents,
allowing them to dynamically generate code for different Robot Framework libraries
without hardcoding keywords or syntax.
"""

from .base import LibraryContext
from .selenium_context import SeleniumLibraryContext
from .browser_context import BrowserLibraryContext

__all__ = [
    "LibraryContext",
    "SeleniumLibraryContext", 
    "BrowserLibraryContext",
    "get_library_context"
]


def get_library_context(library_type: str) -> LibraryContext:
    """
    Factory function to get the appropriate library context.
    
    Args:
        library_type: "selenium" or "browser"
        
    Returns:
        LibraryContext instance for the specified library
        
    Example:
        >>> context = get_library_context("browser")
        >>> print(context.library_import)
        Library    Browser
    """
    library_type = library_type.lower()
    
    if library_type == "selenium":
        return SeleniumLibraryContext()
    elif library_type == "browser":
        return BrowserLibraryContext()
    else:
        raise ValueError(f"Unknown library type: {library_type}. Use 'selenium' or 'browser'")
