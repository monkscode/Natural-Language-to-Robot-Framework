import os
import sys
import logging
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

# Auth (Phase 1): JWT/role guards + /auth router + Postgres users store.
from fastapi import Depends
from src.backend.core.config import settings
from src.backend.auth.jwt_utils import require_admin
from src.backend.auth.endpoints import auth_router
from src.backend.auth.db import init_auth_db, close_pool

# --- FastAPI App ---
app = FastAPI(title="Mark 1 - AI Test Automation Platform")

# CORS — restricted to the SPA origins (dev Vite :5173, nginx container :3000,
# fastapi :5000). allow_credentials stays on for the Google OAuth state cookie.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Global Exception Handler ---
from src.backend.api.error_handlers import register_error_handlers
register_error_handlers(app)

# --- API Routers ---
# Auth routes (public entry points): /auth/register, /auth/login, /auth/me, /auth/google/*
app.include_router(auth_router)

# Generate/execute/feedback routes carry their own per-route guards (require_user).
app.include_router(api_router)

# Admin-only dashboards. require_admin is permissive while settings.AUTH_ENFORCED
# is False (coexistence with the legacy unauthenticated UI), strict at cutover.
from src.backend.api.workflow_metrics_endpoints import router as workflow_metrics_router
app.include_router(workflow_metrics_router, prefix="/api", dependencies=[Depends(require_admin)])

from src.backend.api.trace_endpoints import router as trace_router
app.include_router(trace_router, prefix="/api", dependencies=[Depends(require_admin)])

from src.backend.api.learning_endpoints import router as learning_router
app.include_router(learning_router, prefix="/api/learning", dependencies=[Depends(require_admin)])

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

    # Initialize the auth/users Postgres store. Best-effort: a DB outage must not
    # block the rest of the app from starting (auth fails until Postgres is up).
    try:
        init_auth_db()
    except Exception as e:
        logging.warning(
            f"[AUTH] init_auth_db failed — auth unavailable until Postgres is reachable: {e}"
        )

    # Clean up orphaned temp metrics files left by crashed/incomplete workflows
    try:
        from src.backend.core.temp_metrics_storage import get_temp_metrics_storage
        get_temp_metrics_storage().cleanup_old_files(max_age_hours=24)
    except Exception as e:
        logging.warning(f"Startup temp metrics cleanup failed (non-fatal): {e}")


def _check_learning_health():
    """Pre-flight validation for the learning store (Postgres + pgvector).

    Runs at startup when OPTIMIZATION_ENABLED=true. Ensures Postgres is
    reachable, the consolidated learning schema exists (created if missing),
    and the pgvector extension is installed. Logs warnings on failure but
    never blocks startup.
    """
    from src.backend.core.config import settings

    if not settings.OPTIMIZATION_ENABLED:
        logging.info("[LEARNING HEALTH] Learning system disabled (OPTIMIZATION_ENABLED=false)")
        return

    issues = []
    try:
        import psycopg
        from src.backend.crew_ai.optimization import pg_schema

        conn = psycopg.connect(settings.DATABASE_URL, autocommit=True)
        try:
            pg_schema.ensure_schema(conn)  # idempotent — creates if absent
            conn.execute("SELECT 1 FROM execution_records LIMIT 1")
            if not conn.execute(
                "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
            ).fetchone():
                issues.append("pgvector extension 'vector' is not installed")
        finally:
            conn.close()
    except Exception as e:
        issues.append(f"Postgres learning store unreachable: {e}")

    if issues:
        for issue in issues:
            logging.warning(f"[LEARNING HEALTH] {issue}")
        logging.warning(
            "[LEARNING HEALTH] Learning system may not function correctly. "
            "Fix the issues above or set OPTIMIZATION_ENABLED=false."
        )
    else:
        logging.info(
            "[LEARNING HEALTH] All checks passed — learning system ready (Postgres + pgvector)")

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
            logging.info("Learning store connection closed.")
    except Exception as e:
        logging.warning(f"Learning write queue shutdown error: {e}")

    # Close the auth Postgres pool.
    try:
        close_pool()
    except Exception as e:
        logging.warning(f"[AUTH] close_pool error: {e}")

    logging.info("Application shutdown complete.")
