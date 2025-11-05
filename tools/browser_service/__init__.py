"""
Browser Service Package

This package contains modular components for the browser automation service,
extracted from the monolithic browser_use_service.py file.

Modules:
    - config: Configuration management (BatchConfig, LocatorConfig, LLMConfig)
    - utils: Shared utility functions (JSON parsing, metrics, logging)
    - browser: Browser lifecycle and resource management
    - locators: Locator generation, validation, and extraction
    - prompts: Prompt building for AI agents
    - agent: Custom action definitions and registration
    - tasks: Task processing and workflow execution
    - api: HTTP API endpoints and request handling

Usage:
    # Import global config instance
    from browser_service import config

    # Import specific components
    from browser_service.config import BrowserServiceConfig, BatchConfig, LocatorConfig, LLMConfig
    from browser_service.browser import BrowserSessionManager, cleanup_browser_resources
    from browser_service.locators import generate_locators_from_attributes, validate_locator_playwright
    from browser_service.prompts import build_workflow_prompt, build_system_prompt
    from browser_service.agent import find_unique_locator_action, register_custom_actions
    from browser_service.tasks import process_workflow_task, TaskProcessor
    from browser_service.api import register_routes
    from browser_service.utils import setup_logging, record_workflow_metrics
"""

__version__ = "1.0.0"

# Import key components for convenient access
from browser_service.config import (
    config,
    BrowserServiceConfig,
    BatchConfig,
    LocatorConfig,
    LLMConfig
)

__all__ = [
    # Version
    '__version__',

    # Configuration
    'config',
    'BrowserServiceConfig',
    'BatchConfig',
    'LocatorConfig',
    'LLMConfig',
]
