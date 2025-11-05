"""
Prompt Management Module

This module handles all prompt generation for the browser automation agent.
It provides functions to build workflow prompts, system prompts, and manages
prompt templates for consistent agent behavior.

Key Components:
- workflow.py: Workflow prompt builder for task execution
- system.py: System prompt builder for agent instructions
- templates.py: Reusable prompt templates and fragments

The prompts guide the agent through element identification, locator extraction,
and validation workflows.
"""

from .workflow import build_workflow_prompt
from .system import build_system_prompt

__all__ = [
    'build_workflow_prompt',
    'build_system_prompt',
]
