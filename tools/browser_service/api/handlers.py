"""
Request/Response Handlers for Browser Service API.

This module provides utilities for validating incoming requests and formatting
outgoing responses. It centralizes request validation logic and ensures
consistent response formats across all API endpoints.

Functions:
    validate_workflow_request: Validate workflow request data
    format_task_response: Format task data for API response
    format_error_response: Format error messages for API response
"""

import logging
from typing import Dict, Any, Optional, Tuple, List
from flask import Request

logger = logging.getLogger(__name__)


def validate_workflow_request(request: Request) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
    """
    Validate workflow request and extract data.

    Args:
        request: Flask request object

    Returns:
        Tuple of (is_valid, error_message, data)
        - is_valid: True if request is valid, False otherwise
        - error_message: Error message if invalid, None if valid
        - data: Extracted request data if valid, None if invalid

    Example:
        >>> is_valid, error, data = validate_workflow_request(request)
        >>> if not is_valid:
        >>>     return jsonify({"status": "error", "message": error}), 400
    """
    # Check if request has JSON data
    if not request.is_json:
        return False, "Request must be JSON with Content-Type: application/json", None

    data = request.get_json()
    if not data:
        return False, "No JSON data provided.", None

    # Validate elements field
    elements = data.get("elements")
    if not elements or not isinstance(elements, list):
        return False, "Missing or invalid 'elements' field. Must be a list of element descriptions.", None

    if len(elements) == 0:
        return False, "Elements list cannot be empty.", None

    # Extract other fields (url and user_query are optional but recommended)
    url = data.get("url")
    user_query = data.get("user_query", "")
    session_config = data.get("session_config", {})
    enable_custom_actions = data.get("enable_custom_actions")

    # Return validated data
    validated_data = {
        "elements": elements,
        "url": url,
        "user_query": user_query,
        "session_config": session_config,
        "enable_custom_actions": enable_custom_actions
    }

    return True, None, validated_data


def format_task_response(
    task_data: Dict[str, Any],
    include_results: bool = True,
    truncate_objective: int = 200
) -> Dict[str, Any]:
    """
    Format task data for API response.

    Args:
        task_data: Raw task data dictionary
        include_results: Whether to include full results (for completed tasks)
        truncate_objective: Maximum length for objective field (0 = no truncation)

    Returns:
        Formatted response dictionary

    Example:
        >>> response = format_task_response(task_data, include_results=True)
        >>> return jsonify(response), 200
    """
    task_id = task_data.get("task_id")
    status = task_data.get("status", "unknown")
    objective = task_data.get("objective", "")

    # Truncate objective if requested
    if truncate_objective > 0 and len(objective) > truncate_objective:
        objective = objective[:truncate_objective]

    # Base response data
    response = {
        "task_id": task_id,
        "status": status,
        "objective": objective,
        "created_at": task_data.get("created_at")
    }

    # Add status-specific fields
    if status in ["running", "completed"]:
        response["started_at"] = task_data.get("started_at")

    if status == "completed":
        response["completed_at"] = task_data.get("completed_at")
        response["message"] = task_data.get("message")

        if include_results:
            response["results"] = task_data.get("results", {})

        # Calculate total time if timestamps available
        created_at = task_data.get("created_at")
        completed_at = task_data.get("completed_at")
        if created_at and completed_at:
            response["total_time"] = completed_at - created_at

    elif status == "running":
        # Calculate running time
        import time
        started_at = task_data.get("started_at")
        if started_at:
            response["running_time"] = time.time() - started_at

    return response


def format_error_response(
    message: str,
    status_code: int = 500,
    additional_data: Optional[Dict[str, Any]] = None
) -> Tuple[Dict[str, Any], int]:
    """
    Format error response with consistent structure.

    Args:
        message: Error message
        status_code: HTTP status code (default: 500)
        additional_data: Optional additional data to include in response

    Returns:
        Tuple of (response_dict, status_code)

    Example:
        >>> return format_error_response("Task not found", 404)
        >>> return format_error_response("Invalid request", 400, {"field": "elements"})
    """
    response = {
        "status": "error",
        "message": message
    }

    if additional_data:
        response.update(additional_data)

    return response, status_code


def format_task_list_response(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Format list of tasks for API response.

    Args:
        tasks: List of task data dictionaries

    Returns:
        Formatted response with task summaries and counts

    Example:
        >>> all_tasks = task_processor.list_tasks()
        >>> response = format_task_list_response(all_tasks)
        >>> return jsonify(response), 200
    """
    task_list = []
    active_count = 0

    for task_data in tasks:
        status = task_data.get("status", "unknown")

        # Count active tasks
        if status in ['processing', 'running']:
            active_count += 1

        # Create task summary
        task_summary = {
            "task_id": task_data.get("task_id"),
            "status": status,
            "objective": task_data.get("objective", "")[:100],  # Truncate for display
            "created_at": task_data.get("created_at")
        }

        # Add completion info if available
        if task_data.get("completed_at"):
            task_summary["completed_at"] = task_data["completed_at"]
            task_summary["success"] = task_data.get("results", {}).get("success", False)

        task_list.append(task_summary)

    return {
        "tasks": task_list,
        "total_tasks": len(task_list),
        "active_tasks": active_count
    }
