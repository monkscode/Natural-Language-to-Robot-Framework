"""
Agent management module for browser service.

This module provides custom action definitions and registration for the browser-use agent.
Custom actions allow the agent to call deterministic Python code for locator finding and validation.

Key Components:
- actions.py: Custom action implementations (find_unique_locator_action)
- registration.py: Custom action registration with browser-use agent

Usage:
    from browser_service.agent import find_unique_locator_action, register_custom_actions

    # Register custom actions with agent
    success = register_custom_actions(agent, page=None)
"""

from browser_service.agent.actions import find_unique_locator_action
from browser_service.agent.registration import register_custom_actions

__all__ = [
    'find_unique_locator_action',
    'register_custom_actions'
]
