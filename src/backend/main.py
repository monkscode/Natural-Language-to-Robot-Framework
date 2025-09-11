import os
import logging
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from src.backend.api.endpoints import router as api_router

# --- Logging Configuration ---
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

# --- Static Files and Root Endpoint ---
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
ROBOT_TESTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "robot_tests")

app.mount("/reports", StaticFiles(directory=ROBOT_TESTS_DIR), name="reports")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")

@app.on_event("startup")
async def startup_event():
    logging.info("Application startup complete.")

@app.on_event("shutdown")
async def shutdown_event():
    logging.info("Application shutdown complete.")
