import os
import sys
import logging
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
file_handler = RotatingFileHandler(
    'logs/application.log',
    maxBytes=10*1024*1024,  # 10MB per file
    backupCount=5,           # Keep 5 backup files
    encoding='utf-8'
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)

# Configure root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

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
@app.get("/health")
async def health_check():
    """Health check endpoint for Docker health monitoring."""
    return {"status": "healthy", "service": "nlrf-fastapi"}

@app.get("/api/health")
async def api_health_check():
    """API health check endpoint."""
    return {"status": "healthy", "service": "nlrf-api"}

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

@app.on_event("shutdown")
async def shutdown_event():
    logging.info("Application shutdown complete.")
