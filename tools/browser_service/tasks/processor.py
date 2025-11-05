"""
Task processor module for managing background task execution.

This module provides the TaskProcessor class which handles:
- Task submission and execution in background threads
- Task status tracking
- Task result management
"""

import time
from typing import Dict, Any, Callable, Optional
from concurrent.futures import ThreadPoolExecutor
import logging

logger = logging.getLogger(__name__)


class TaskProcessor:
    """
    Manages background task execution and status tracking.

    This class provides a centralized way to submit tasks for background execution,
    track their status, and retrieve results.
    """

    def __init__(self, executor: ThreadPoolExecutor):
        """
        Initialize the TaskProcessor.

        Args:
            executor: ThreadPoolExecutor instance for running tasks in background
        """
        self.executor = executor
        self.tasks: Dict[str, Dict[str, Any]] = {}
        logger.info("TaskProcessor initialized")

    def submit_task(
        self,
        task_id: str,
        task_function: Callable,
        *args,
        **kwargs
    ) -> None:
        """
        Submit a task for background execution.

        Args:
            task_id: Unique identifier for the task
            task_function: The function to execute
            *args: Positional arguments to pass to the function
            **kwargs: Keyword arguments to pass to the function
        """
        # Initialize task status
        self.tasks[task_id] = {
            "status": "processing",
            "created_at": time.time(),
            "objective": f"Task {task_id}"
        }

        logger.info(f"Submitting task {task_id} for background execution")

        # Submit to executor
        self.executor.submit(task_function, *args, **kwargs)

    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the status of a specific task.

        Args:
            task_id: The task identifier

        Returns:
            Task status dictionary or None if task not found
        """
        return self.tasks.get(task_id)

    def list_tasks(self) -> list:
        """
        List all tasks with their status.

        Returns:
            List of task dictionaries with status information
        """
        task_list = []
        for task_id, task_data in self.tasks.items():
            task_summary = {
                "task_id": task_id,
                **task_data
            }
            task_list.append(task_summary)

        return task_list

    def get_tasks_dict(self) -> Dict[str, Dict[str, Any]]:
        """
        Get the internal tasks dictionary.

        This is used by task functions to update their status.

        Returns:
            The tasks dictionary
        """
        return self.tasks
