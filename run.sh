#!/bin/bash

# Force UTF-8 encoding for all Python operations (fixes emoji/Unicode issues on Windows)
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

# Fix protobuf compatibility with Python 3.12 and robotframework-browser
# This uses pure Python protobuf parsing (slower but compatible)
export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

# Add Docker Desktop binaries to PATH so docker-credential-desktop is resolvable
# by the Python docker SDK when authenticating against Docker Hub on Windows.
export PATH="/c/Program Files/Docker/Docker/resources/bin:$PATH"

# Check for .env file
if [ ! -f "src/backend/.env" ]; then
    echo "Error: src/backend/.env file not found."
    echo "Please copy src/backend/.env.example to src/backend/.env and fill in your API key."
    exit 1
fi

# Load environment variables, including the application port
set -a
source src/backend/.env
set +a

# --- Local-dev overrides (run.sh process only, never containers) ---
# These override values from src/backend/.env for processes launched by this script.
# Docker Compose reads src/backend/.env directly and does NOT source run.sh, so the
# on-disk value (BROWSER_HEADLESS=true) remains authoritative for containers.
export BROWSER_HEADLESS=false
export LOG_FORMAT=console          # human-readable colored logs
# export CREWAI_VERBOSE=true         # show agent reasoning in console

# Support both APP_PORT (new) and PORT (legacy) variables with a sane default
APP_PORT="${APP_PORT:-${PORT:-5000}}"
export APP_PORT
export PORT="$APP_PORT"

# When running locally (not inside Docker Compose), HOST_ROBOT_TESTS_DIR must be
# an absolute Windows path so the Docker daemon can resolve the bind mount.
# The .env value (./robot_tests) is relative and only valid inside Docker Compose.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -W 2>/dev/null || pwd)"
export HOST_ROBOT_TESTS_DIR="${SCRIPT_DIR}/robot_tests"

# Cross-platform venv activation and path handling
VENV_DIR="venv"
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
    # Windows (using Git Bash, Cygwin, etc.)
    VENV_ACTIVATE="$VENV_DIR/Scripts/activate"
else
    # Linux, macOS
    VENV_ACTIVATE="$VENV_DIR/bin/activate"
fi

# Check if venv exists and is valid
if [ -d "$VENV_DIR" ] && [ -f "$VENV_ACTIVATE" ]; then
    echo "Using existing virtual environment..."
    source "$VENV_ACTIVATE"
else
    echo "Creating new virtual environment..."
    # Remove invalid venv if it exists
    [ -d "$VENV_DIR" ] && rm -rf "$VENV_DIR"
    python -m venv "$VENV_DIR"
    source "$VENV_ACTIVATE"
    echo "Installing dependencies..."
    pip install uv
    uv pip install -r src/backend/requirements.txt
    playwright install chromium
    rfbrowser install chromium
fi

# --- Postgres (auth + learning stack live here as of Phase 4) ---
echo "Starting Postgres (nlrf-postgres)..."
docker compose up -d postgres || { echo "Error: failed to start the postgres container. Is Docker running?"; exit 1; }

echo -n "Waiting for Postgres to accept connections"
for i in $(seq 1 30); do
    if docker exec nlrf-postgres pg_isready -U "${POSTGRES_USER:-nlrf}" -d "${POSTGRES_DB:-nlrf}" > /dev/null 2>&1; then
        echo " ready."
        PG_READY=1
        break
    fi
    echo -n "."
    sleep 1
done
if [ -z "$PG_READY" ]; then
    echo
    echo "Error: Postgres did not become ready within 30s. Check 'docker logs nlrf-postgres'."
    exit 1
fi

# --- React SPA dev server (Vite, :5173) ---
if [ ! -d "src/frontend-react/node_modules" ]; then
    echo "Installing frontend dependencies (first run)..."
    (cd src/frontend-react && npm install)
fi

# Run the application
echo "Starting the application..."
python -m uvicorn src.backend.main:app --host 0.0.0.0 --port "${APP_PORT}" &
UVICORN_PID=$!

python tools/browser_use_service.py > bus.log 2>&1 &
BROWSER_SERVICE_PID=$!

# vite.js is run with node directly (not 'npm run dev') and exec'd so the PID
# we kill on exit is the actual dev-server process, not a wrapper around it.
(cd src/frontend-react && exec node node_modules/vite/bin/vite.js) > frontend.log 2>&1 &
FRONTEND_PID=$!

echo ""
echo "  React SPA (validate here):  http://localhost:5173"
echo "  FastAPI backend (API only):  http://localhost:${APP_PORT}"
echo "  BrowserUse service:          http://localhost:4999/health"
echo "  Postgres:                    localhost:5432 (container nlrf-postgres)"
echo ""
echo "  Logs: backend in this console; bus.log (BrowserUse); frontend.log (Vite)"
echo "  Press Ctrl+C to stop everything (Postgres container stays up)."
echo ""

cleanup() {
    kill "$UVICORN_PID" "$BROWSER_SERVICE_PID" "$FRONTEND_PID" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

wait -n "$UVICORN_PID" "$BROWSER_SERVICE_PID" "$FRONTEND_PID"
EXIT_CODE=$?
cleanup
wait "$UVICORN_PID" "$BROWSER_SERVICE_PID" "$FRONTEND_PID" 2>/dev/null || true
exit "$EXIT_CODE"