"""
Flask Route Definitions for Browser Service API.

This module contains all Flask route handlers for the browser automation service.
It separates API endpoint definitions from business logic, making the codebase
more maintainable and testable.

Routes:
    GET  /           - Service information and available endpoints
    GET  /health     - Health check with service status
    GET  /probe      - Legacy health check endpoint
    POST /workflow   - Submit workflow task (primary endpoint)
    POST /batch      - Deprecated alias for /workflow
    GET  /query/<id> - Query task status by ID
    GET  /tasks      - List all tasks with summaries
"""

import logging
import time
import uuid
from flask import Flask, request, jsonify
from typing import Any

from browser_service.api.handlers import (
    validate_workflow_request,
    format_task_response,
    format_error_response,
    format_task_list_response
)
from browser_service.config import config
from browser_service.tasks import process_workflow_task
from src.backend.core.config import settings

logger = logging.getLogger(__name__)


def register_routes(app: Flask, task_processor: Any) -> None:
    """
    Register all API routes with the Flask app.

    Args:
        app: Flask application instance
        task_processor: TaskProcessor instance for managing tasks

    Example:
        >>> app = Flask(__name__)
        >>> task_processor = TaskProcessor(executor)
        >>> register_routes(app, task_processor)
    """

    @app.route('/', methods=['GET'])
    def root():
        """Root endpoint to verify service is running."""
        return jsonify({
            "service": "Enhanced Browser Use Service with Vision-Based Locators",
            "status": "running",
            "version": "4.0.0",
            "improvements": [
                "Vision AI for element identification (built-in browser-use)",
                "Structured JSON locator output",
                "Multiple locator strategies (10+ options)",
                "Locator stability scoring",
                "Validation and uniqueness checking",
                "Smart fallback mechanisms",
                "Better encoding handling",
                "Proper session cleanup",
                "NEW: Batch processing mode for multiple elements in one session",
                "NEW: Persistent browser session across element lookups",
                "NEW: Context-aware popup handling"
            ],
            "endpoints": [
                "GET / - This endpoint",
                "GET /health - Health check",
                "GET /probe - Legacy health check",
                "POST /workflow - Process workflow task (unified session, RECOMMENDED)",
                "POST /batch - Deprecated alias for /workflow (backward compatible)",
                "GET /query/<task_id> - Query task status",
                "GET /tasks - List all tasks"
            ]
        }), 200

    @app.route('/health', methods=['GET'])
    def health():
        """Health check endpoint."""
        all_tasks = task_processor.get_tasks_dict()
        return jsonify({
            "status": "healthy",
            "service": "enhanced_browser_use_service",
            "timestamp": time.time(),
            "active_tasks": len([t for t in all_tasks.values() if t.get('status') in ['processing', 'running']]),
            "total_tasks": len(all_tasks),
            "encoding": "utf-8",
            "google_api_configured": bool(config.llm.google_api_key and config.llm.google_api_key != 'your_api_key_here')
        }), 200

    @app.route('/probe', methods=['GET'])
    def probe():
        """Legacy probe endpoint for backward compatibility."""
        return jsonify({"status": "alive", "message": "enhanced_browser_use_service is alive"}), 200

    @app.route('/workflow', methods=['POST'])
    @app.route('/batch', methods=['POST'])  # Deprecated alias for backward compatibility
    def workflow_submit():
        """
        Process a workflow task with multiple elements in a single browser session.

        This endpoint handles complete user workflows (navigate → act → extract locators).
        All elements are processed in ONE browser session for context preservation.

        Endpoints:
            /workflow - Primary endpoint (recommended)
            /batch - Deprecated alias (backward compatible)

        Request JSON:
            {
                "elements": [{"id": "elem_1", "description": "...", "action": "input"}, ...],
                "url": "https://example.com",
                "user_query": "search for shoes and get product name",
                "enable_custom_actions": true  // Optional: Enable/disable custom actions (defaults to config value)
            }

        Response JSON:
            {
                "task_id": "uuid",
                "status": "processing",
                "message": "Workflow task submitted (N elements in single session)"
            }
        """
        # Log deprecation warning if /batch endpoint is used
        if request.path == '/batch':
            logger.warning("⚠️  /batch endpoint is deprecated. Please use /workflow instead.")

        logger.info(f"📥 Received workflow request via {request.path}")

        try:
            # Validate request
            is_valid, error_message, data = validate_workflow_request(request)
            if not is_valid:
                return format_error_response(error_message, 400)

            # Extract validated data
            elements = data["elements"]
            url = data["url"]
            user_query = data["user_query"]
            session_config = data["session_config"]
            enable_custom_actions = data["enable_custom_actions"]

            # Feature flag: enable_custom_actions (defaults to config value if not provided)
            if enable_custom_actions is None:
                enable_custom_actions = settings.ENABLE_CUSTOM_ACTIONS
                logger.info(f"🔧 enable_custom_actions not provided in request, using config default: {enable_custom_actions}")
            else:
                logger.info(f"🔧 enable_custom_actions provided in request: {enable_custom_actions}")

            # All tasks are processed as unified workflows
            logger.info("✅ Using unified workflow mode (all tasks processed as workflows)")

            # Check if service is busy (only one task at a time)
            active_tasks = [t for t in task_processor.get_tasks_dict().values()
                            if t.get('status') in ['processing', 'running']]
            if active_tasks:
                return jsonify({
                    "status": "busy",
                    "message": "Service is currently processing another task. Please try again later.",
                    "active_tasks": len(active_tasks)
                }), 429

            # Generate a unique task ID
            task_id = str(uuid.uuid4())

            # Log task submission
            logger.info(f"🚀 Workflow task {task_id} submitted with {len(elements)} elements for URL: {url}")
            logger.info("   Processing mode: Unified workflow (single Agent session)")
            logger.info(f"📝 User query: {user_query[:100]}{'...' if len(user_query) > 100 else ''}")

            # Submit to task processor
            task_processor.submit_task(
                task_id,
                process_workflow_task,
                task_id,
                elements,
                url,
                user_query,
                session_config,
                enable_custom_actions,
                task_processor.get_tasks_dict()
            )

            # Return the task ID immediately
            return jsonify({
                "status": "processing",
                "task_id": task_id,
                "message": f"Workflow task submitted with {len(elements)} elements (unified session)",
                "elements_count": len(elements),
                "mode": "workflow"
            }), 202

        except Exception as e:
            logger.error(f"Error in workflow submit endpoint: {e}", exc_info=True)
            return format_error_response(f"Internal server error: {str(e)}", 500)

    @app.route('/query/<task_id>', methods=['GET'])
    def query(task_id: str):
        """Query the status of a specific task."""
        try:
            task = task_processor.get_task_status(task_id)

            if task is None:
                return format_error_response("Task ID not found.", 404)

            status = task.get("status")

            # Format response based on status
            if status == "processing":
                response = format_task_response(task, include_results=False, truncate_objective=200)
                return jsonify(response), 202

            elif status == "running":
                response = format_task_response(task, include_results=False, truncate_objective=200)
                return jsonify(response), 202

            elif status == "completed":
                response = format_task_response(task, include_results=True, truncate_objective=200)
                logger.info(f"Task {task_id} query completed: {task.get('results', {}).get('success', False)}")
                return jsonify(response), 200

            else:
                response = format_task_response(task, include_results=False, truncate_objective=200)
                return jsonify(response), 200

        except Exception as e:
            logger.error(f"Error in query endpoint: {e}", exc_info=True)
            return format_error_response(f"Internal server error: {str(e)}", 500)

    @app.route('/tasks', methods=['GET'])
    def list_tasks():
        """List all tasks with their status."""
        try:
            all_tasks = task_processor.list_tasks()
            response = format_task_list_response(all_tasks)
            return jsonify(response), 200

        except Exception as e:
            logger.error(f"Error in list_tasks endpoint: {e}", exc_info=True)
            return format_error_response(f"Internal server error: {str(e)}", 500)

    @app.errorhandler(404)
    def not_found(error):
        """Handle 404 errors."""
        return jsonify({
            "status": "error",
            "message": "Endpoint not found",
            "available_endpoints": [
                "GET / - Service info",
                "GET /health - Health check",
                "GET /probe - Legacy health check",
                "POST /workflow - Process workflow task (RECOMMENDED)",
                "POST /batch - Deprecated alias for /workflow",
                "GET /query/<task_id> - Query task",
                "GET /tasks - List tasks"
            ]
        }), 404

    @app.errorhandler(500)
    def internal_error(error):
        """Handle 500 errors."""
        return jsonify({
            "status": "error",
            "message": "Internal server error",
            "error": str(error)
        }), 500
