"""
Browser management module for browser service.

This module handles browser lifecycle management including:
- Browser session creation and management
- Browser resource cleanup and process termination
- Browser process tracking and monitoring

Modules:
    session: Browser session lifecycle management
    cleanup: Browser cleanup utilities and process termination
"""

from .cleanup import get_browser_process_id, cleanup_browser_resources
from .session import BrowserSessionManager

__all__ = [
    'get_browser_process_id',
    'cleanup_browser_resources',
    'BrowserSessionManager',
]
