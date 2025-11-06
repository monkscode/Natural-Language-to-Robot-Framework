"""
API Layer for Browser Service

This package provides the Flask-based HTTP API for browser automation and locator extraction.
It separates API concerns (routing, validation, formatting) from business logic (task processing,
browser management, locator extraction).

Key Components:
- routes.py: Flask route definitions and endpoint handlers
- handlers.py: Request validation and response formatting utilities

API Endpoints:
    GET  /           - Service information and available endpoints
    GET  /health     - Health check with service status
    GET  /probe      - Legacy health check endpoint
    POST /workflow   - Submit workflow task (primary endpoint)
    POST /batch      - Deprecated alias for /workflow
    GET  /query/<id> - Query task status by ID
    GET  /tasks      - List all tasks with summaries

Usage:
    from browser_service.api import register_routes
    from flask import Flask

    app = Flask(__name__)
    register_routes(app, task_processor)
    app.run(host='0.0.0.0', port=4999)
"""

from browser_service.api.routes import register_routes
from browser_service.api.handlers import (
    validate_workflow_request,
    format_task_response,
    format_error_response
)

__all__ = [
    'register_routes',
    'validate_workflow_request',
    'format_task_response',
    'format_error_response'
]
