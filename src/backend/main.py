import os
import sys
import sqlite3
import logging
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# ========================================
# FIX: Unicode/Emoji Encoding on Windows
# ========================================
# Reconfigure stdout/stderr to use UTF-8 encoding
# This fixes UnicodeEncodeError for emojis (🚀, 🐳, etc.) in logs
if sys.platform.startswith('win'):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    os.environ['PYTHONIOENCODING'] = 'utf-8'

from src.backend.api.endpoints import router as api_router

# --- Logging Configuration ---
# Create logs directory if it doesn't exist
os.makedirs("logs", exist_ok=True)

# Configure logging with both console and file handlers
log_format = '%(asctime)s - %(levelname)-8s [%(name)s] %(message)s'
date_format = '%Y-%m-%d %H:%M:%S'

# Create formatters
formatter = logging.Formatter(log_format, datefmt=date_format)

# File handler - rotates logs to prevent huge files
from logging.handlers import RotatingFileHandler
try:
    file_handler = RotatingFileHandler(
        'logs/application.log',
        maxBytes=10*1024*1024,  # 10MB per file
        backupCount=5,           # Keep 5 backup files
        encoding='utf-8'
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
except (OSError, IOError, Exception) as e:
    fallback_handler = logging.StreamHandler()
    fallback_handler.setLevel(logging.INFO)
    fallback_handler.setFormatter(formatter)
    file_handler = fallback_handler
    logging.warning(f"Failed to initialize file logging, using stream fallback: {type(e).__name__}: {e}")

# Console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)

# Configure root logger
logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, console_handler],
    force=True,
)

# Disable crewai tracing prompts to prevent log spam
os.environ['CREWAI_TRACING_ENABLED'] = 'false'

logger = logging.getLogger(__name__)
logger.info("🚀 Starting application with file logging enabled at logs/application.log")

# --- FastAPI App ---
app = FastAPI(title="Mark 1 - AI Test Automation Platform")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- API Router ---
app.include_router(api_router)

# --- Workflow Metrics API Router ---
from src.backend.api.workflow_metrics_endpoints import router as workflow_metrics_router
app.include_router(workflow_metrics_router, prefix="/api")

# --- Health Check Endpoint ---
from src.backend.core.config import settings
from src.backend.services.workflow_service import get_active_workflow_count

@app.get("/health")
async def health_check():
    """Health check endpoint for Docker health monitoring."""
    active = get_active_workflow_count()
    max_wf = settings.MAX_CONCURRENT_WORKFLOWS
    return {
        "status": "healthy",
        "service": "nlrf-fastapi",
        "active_workflows": active,
        "max_workflows": max_wf,
        "available_slots": max_wf - active,
    }

@app.get("/api/health")
async def api_health_check():
    """API health check endpoint."""
    active = get_active_workflow_count()
    max_wf = settings.MAX_CONCURRENT_WORKFLOWS
    return {
        "status": "healthy",
        "service": "nlrf-api",
        "active_workflows": active,
        "max_workflows": max_wf,
        "available_slots": max_wf - active,
    }

# --- Static Files and Root Endpoint ---
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
ROBOT_TESTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "robot_tests")

# Create robot_tests directory if it doesn't exist
os.makedirs(ROBOT_TESTS_DIR, exist_ok=True)

# Mount static files
app.mount("/reports", StaticFiles(directory=ROBOT_TESTS_DIR), name="reports")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")

@app.on_event("startup")
async def startup_event():
    logging.info("Application startup complete.")
    _check_learning_health()

    # Clean up orphaned temp metrics files left by crashed/incomplete workflows
    try:
        from src.backend.core.temp_metrics_storage import get_temp_metrics_storage
        get_temp_metrics_storage().cleanup_old_files(max_age_hours=24)
    except Exception as e:
        logging.warning(f"Startup temp metrics cleanup failed (non-fatal): {e}")


def _check_learning_health():
    """Pre-flight validation for learning system databases and paths.

    Runs at startup when OPTIMIZATION_ENABLED=true. Validates that all
    required paths exist, SQLite databases pass integrity checks, and
    the schema is current. Logs warnings on failure but never blocks startup.
    """
    from src.backend.core.config import settings

    if not settings.OPTIMIZATION_ENABLED:
        logging.info("[LEARNING HEALTH] Learning system disabled (OPTIMIZATION_ENABLED=false)")
        return

    issues = []

    # Check 1: data/ directory exists (required for execution_memory.db)
    from src.backend.crew_ai.optimization.learning_config import LEARNING_CONFIG
    exec_db_path = LEARNING_CONFIG["EXECUTION_MEMORY_DB"]
    data_dir = str(Path(exec_db_path).parent)
    if not os.path.isdir(data_dir):
        try:
            os.makedirs(data_dir, exist_ok=True)
            logging.info(f"[LEARNING HEALTH] Created directory: {data_dir}")
        except OSError as e:
            issues.append(f"Cannot create data directory '{data_dir}': {e}")

    # Check 2: execution_memory.db is accessible and healthy
    try:
        conn = sqlite3.connect(exec_db_path)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if result[0] != "ok":
            issues.append(f"execution_memory.db integrity check failed: {result[0]}")
        conn.close()
    except Exception as e:
        issues.append(f"execution_memory.db cannot be opened: {e}")

    # Check 3: ChromaDB directory is writable
    chroma_dir = settings.OPTIMIZATION_CHROMA_DB_PATH
    chroma_parent = str(Path(chroma_dir).parent) if chroma_dir else "."
    if chroma_parent and not os.access(chroma_parent, os.W_OK):
        issues.append(f"ChromaDB parent directory not writable: {chroma_parent}")

    # Report results
    if issues:
        for issue in issues:
            logging.warning(f"[LEARNING HEALTH] {issue}")
        logging.warning(
            "[LEARNING HEALTH] Learning system may not function correctly. "
            "Fix the issues above or set OPTIMIZATION_ENABLED=false."
        )
    else:
        logging.info("[LEARNING HEALTH] All checks passed — learning system ready")

@app.on_event("shutdown")
async def shutdown_event():
    # Drain pending learning writes before exit
    try:
        from src.backend.crew_ai.optimization.learning_registry import get_feedback_loop
        feedback_loop = get_feedback_loop()
        if feedback_loop is not None:
            feedback_loop.write_queue.shutdown(timeout=5.0)
            logging.info("Learning write queue drained successfully.")
    except Exception as e:
        logging.warning(f"Learning write queue shutdown error: {e}")
    logging.info("Application shutdown complete.")
