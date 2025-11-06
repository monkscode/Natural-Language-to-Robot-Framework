"""
Task Processing Module

This module handles asynchronous task execution and workflow management for browser automation.
It provides a unified workflow processor that executes complete user workflows in a single
browser session, preserving context across all actions.

Key Components:
- workflow.py: Workflow task processor (navigate → act → extract locators)
- processor.py: Task state management and background execution

Features:
- Single browser session for entire workflow (optimal cost)
- Context preservation across all actions
- Background task execution with ThreadPoolExecutor
- Task status tracking and querying
- Comprehensive error handling and logging

Usage:
    from browser_service.tasks import process_workflow_task, TaskProcessor

    # Initialize task processor
    executor = ThreadPoolExecutor(max_workers=1)
    task_processor = TaskProcessor(executor)

    # Submit workflow task
    task_processor.submit_task(
        task_id,
        process_workflow_task,
        task_id, elements, url, user_query, session_config, enable_custom_actions, tasks_dict
    )

    # Query task status
    status = task_processor.get_task_status(task_id)
"""

from .workflow import process_workflow_task
from .processor import TaskProcessor

__all__ = ['process_workflow_task', 'TaskProcessor']
