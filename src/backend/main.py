import os
import logging
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from src.backend.api.endpoints import router as api_router
from src.backend.core.logging_config import setup_healing_logging
from src.backend.core.metrics import initialize_metrics
from src.backend.core.audit_trail import initialize_audit_trail
from src.backend.core.alerting import initialize_alerting, get_alerting_system

# --- Logging Configuration ---
# Initialize structured logging for healing system
setup_healing_logging(log_level="INFO", log_dir="logs")

# Initialize monitoring systems
initialize_metrics(retention_hours=24)
initialize_audit_trail(storage_path="logs/audit")
initialize_alerting()

# Keep basic logging for non-healing components
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

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

# --- Healing API Router ---
from src.backend.api.healing_endpoints import router as healing_router
app.include_router(healing_router, prefix="/api")

# --- Monitoring API Router ---
from src.backend.api.monitoring_endpoints import router as monitoring_router
app.include_router(monitoring_router, prefix="/api")

# --- Static Files and Root Endpoint ---
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
ROBOT_TESTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "robot_tests")

app.mount("/reports", StaticFiles(directory=ROBOT_TESTS_DIR), name="reports")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")

@app.on_event("startup")
async def startup_event():
    # Start alerting system
    alerting_system = get_alerting_system()
    await alerting_system.start()
    
    logging.info("Application startup complete with monitoring systems initialized.")

@app.on_event("shutdown")
async def shutdown_event():
    # Stop alerting system
    alerting_system = get_alerting_system()
    await alerting_system.stop()
    
    logging.info("Application shutdown complete.")
