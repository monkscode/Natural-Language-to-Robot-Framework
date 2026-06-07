import os
import sys
import sqlite3
import logging
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

# Windows: reconfigure stdout/stderr to UTF-8 for emoji log compatibility.
if sys.platform.startswith('win'):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    os.environ['PYTHONIOENCODING'] = 'utf-8'

# --- Structured Logging (must be FIRST — before any logger calls) ---
from src.backend.config.logging_config import setup_logging
setup_logging()

# Disable crewai tracing prompts to prevent log spam
os.environ['CREWAI_TRACING_ENABLED'] = 'false'

logger = logging.getLogger(__name__)
logger.info("Starting application with structured logging enabled at logs/application.log")

# --- LLM Observability (must be BEFORE any import that loads CrewAI/LiteLLM) ---
from src.backend.core.observability import init_observability
init_observability()

# This import chain pulls in endpoints → workflow_service → crew_ai → litellm.
# Observability must be initialized before this line so OpenLLMetry patches apply.
from src.backend.api.endpoints import router as api_router

# --- FastAPI App ---
app = FastAPI(title="Mark 1 - AI Test Automation Platform")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Global Exception Handler ---
from src.backend.api.error_handlers import register_error_handlers
register_error_handlers(app)

# --- API Routers ---
app.include_router(api_router)

from src.backend.api.workflow_metrics_endpoints import router as workflow_metrics_router
app.include_router(workflow_metrics_router, prefix="/api")

from src.backend.api.trace_endpoints import router as trace_router
app.include_router(trace_router, prefix="/api")

from src.backend.api.learning_endpoints import router as learning_router
app.include_router(learning_router, prefix="/api/learning")

# --- Health Check Endpoints ---
from src.backend.api.health import health_check, api_health_check

app.get("/health")(health_check)
app.get("/api/health")(api_health_check)

# --- Static Files and Root Endpoint ---
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
ROBOT_TESTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "robot_tests")

# Create robot_tests directory if it doesn't exist
os.makedirs(ROBOT_TESTS_DIR, exist_ok=True)

# /learning SPA catch-all — must be registered before the static mount so
# clean URLs like /learning and /learning/hints/5 serve learning.html
_learning_html = os.path.join(FRONTEND_DIR, "learning.html")

@app.get("/learning")
@app.get("/learning/{path:path}")
async def learning_spa():
    return FileResponse(_learning_html)

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
            feedback_loop.execution_memory.close()
            logging.info("Learning SQLite writer connection closed.")
    except Exception as e:
        logging.warning(f"Learning write queue shutdown error: {e}")
    logging.info("Application shutdown complete.")
