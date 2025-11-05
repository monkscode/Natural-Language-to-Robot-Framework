"""
Enhanced Browser Use Service - Main Application Entry Point

This service provides a Flask-based API for browser automation and web element locator extraction.
It uses browser-use library with vision AI for intelligent element identification and generates
robust, validated locators for test automation frameworks (Robot Framework, Selenium).

Architecture:
    - API Layer: Flask routes and request/response handling (browser_service.api)
    - Task Processing: Async workflow execution and task management (browser_service.tasks)
    - Agent Management: Custom actions and LLM integration (browser_service.agent)
    - Locator Management: Generation, validation, and extraction (browser_service.locators)
    - Browser Management: Session lifecycle and cleanup (browser_service.browser)
    - Prompts: LLM prompt templates and builders (browser_service.prompts)
    - Configuration: Centralized config management (browser_service.config)
    - Utilities: JSON parsing, metrics, logging (browser_service.utils)

Key Features:
    - Vision AI element identification using browser-use
    - Multiple locator strategies (ID, data-testid, name, aria-label, CSS, XPath)
    - Playwright-based locator validation (uniqueness checking)
    - Unified workflow mode (single browser session for entire workflow)
    - Custom actions for smart locator finding
    - Comprehensive error handling and logging
    - Windows UTF-8 compatibility

API Endpoints:
    GET  /           - Service information and available endpoints
    GET  /health     - Health check with service status
    GET  /probe      - Legacy health check endpoint
    POST /workflow   - Submit workflow task (primary endpoint)
    POST /batch      - Deprecated alias for /workflow
    GET  /query/<id> - Query task status by ID
    GET  /tasks      - List all tasks with summaries

Usage:
    # Start service
    python -m tools.browser_use_service

    # Or run directly
    python tools/browser_use_service.py

Environment Variables:
    GEMINI_API_KEY: Google Gemini API key (required)
    ROBOT_LIBRARY: Target library type ("browser" or "selenium", default: "browser")
    BROWSER_USE_SERVICE_URL: Service URL (default: http://localhost:4999)
    ENABLE_CUSTOM_ACTIONS: Enable custom actions (default: true)

Version: 4.0.0
"""

# ========================================
# PATH SETUP (FALLBACK)
# ========================================
# When running as a module (python -m tools.browser_use_service),
# tools/__init__.py handles path setup automatically.
# When running directly (python tools/browser_use_service.py),
# we need this fallback to ensure imports work.
import sys
import os
from pathlib import Path
_project_root = Path(__file__).parent.parent
_tools_dir = Path(__file__).parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))
if str(_tools_dir) not in sys.path:
    sys.path.insert(0, str(_tools_dir))

# ========================================
# LOGGING SETUP - MUST BE BEFORE BROWSER-USE IMPORTS
# ========================================
# CRITICAL: Configure logging BEFORE importing browser-use to ensure all loggers use UTF-8
from browser_service.utils.logging_setup import setup_logging  # noqa: E402

# Setup logging with UTF-8 support (handles Windows compatibility)
logger = setup_logging(logger_name=__name__)

# ========================================
# STANDARD LIBRARY & THIRD-PARTY IMPORTS
# ========================================
from urllib.parse import urlparse  # noqa: E402
from flask import Flask  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

# ========================================
# LOCAL MODULE IMPORTS
# ========================================
# Import modular components (order: config → utils → domain → api)
from browser_service.config import config  # noqa: E402
from browser_service.tasks import TaskProcessor  # noqa: E402
from browser_service.api import register_routes  # noqa: E402

# ========================================
# STANDARD LIBRARY & THIRD-PARTY IMPORTS
# ========================================
# Import browser-use AFTER logging is configured
from dotenv import load_dotenv  # noqa: E402

# ========================================
# LOCAL IMPORTS
# ========================================
# These imports work because:
# 1. tools/__init__.py sets up path (when imported as module)
# 2. Fallback above sets up path (when run directly)
from src.backend.core.config import settings  # noqa: E402

# Load environment variables
load_dotenv("src/backend/.env")

# ========================================
# FLASK APPLICATION
# ========================================
app = Flask(__name__)

# ========================================
# CONFIGURATION
# ========================================
# Configuration is now managed by browser_service.config module
# Access via: config.batch.max_agent_steps, config.locator.content_based_retries, etc.

logger.info(f"🔧 Browser Use Service configured for: {config.robot_library}")
logger.info(
    f"Batch Config: max_steps={config.batch.max_agent_steps}, max_retries={config.batch.max_retries_per_element}, timeout={config.batch.element_timeout}s")
logger.info(
    f"Locator Extraction Config: content_retries={config.locator.content_based_retries}, coordinate_retries={config.locator.coordinate_based_retries}, coordinate_offsets={config.locator.coordinate_offset_attempts}")

# ========================================
# SERVICE INITIALIZATION
# ========================================
# Initialize task processor with thread pool executor
executor = ThreadPoolExecutor(max_workers=1)
task_processor = TaskProcessor(executor)

# Log configuration
logger.info("🤖 LLM Configuration:")
logger.info(f"   Model: {config.llm.google_model}")

# Register API routes with Flask app
register_routes(app, task_processor)


if __name__ == '__main__':
    # Set encoding environment variables
    os.environ['PYTHONIOENCODING'] = 'utf-8'

    logger.info("Starting Enhanced Browser Use Service v4.0...")

    # Validate configuration
    validation_errors = config.validate()
    if validation_errors:
        logger.error("❌ Configuration validation failed:")
        for error in validation_errors:
            logger.error(f"   - {error}")
        logger.warning("⚠️ Service starting with invalid configuration - some features may not work correctly")
    else:
        logger.info("✅ Configuration validation passed")

    service_url = os.getenv(
        "BROWSER_USE_SERVICE_URL") or settings.BROWSER_USE_SERVICE_URL
    parsed_url = urlparse(service_url)
    resolved_port = parsed_url.port or (
        443 if parsed_url.scheme == "https" else 80)
    logger.info(
        f"Service will run on {parsed_url.scheme}://{parsed_url.hostname or '0.0.0.0'}:{resolved_port}")
    logger.info("Available endpoints:")
    logger.info("  GET  / - Service information")
    logger.info("  GET  /health - Health check")
    logger.info("  GET  /probe - Legacy health check")
    logger.info(
        "  POST /workflow - Process workflow task (unified session, primary endpoint)")
    logger.info(
        "  POST /batch - Deprecated alias for /workflow (backward compatible)")
    logger.info("  GET  /query/<task_id> - Query task status")
    logger.info("  GET  /tasks - List all tasks")

    logger.info("Enhanced features:")
    logger.info("  - Better encoding handling for international characters")
    logger.info("  - Improved CSS selector detection strategies")
    logger.info("  - Enhanced error handling and recovery")
    logger.info("  - Batch processing with persistent browser sessions (NEW)")
    logger.info("  - Context-aware popup handling (NEW)")
    logger.info("  - Proper browser session cleanup")
    logger.info("  - Fallback mechanisms for dynamic sites")

    app.run(debug=False, host='0.0.0.0', port=resolved_port, threaded=True)
