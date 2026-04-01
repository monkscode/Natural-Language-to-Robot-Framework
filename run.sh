#!/bin/bash

# Force UTF-8 encoding for all Python operations (fixes emoji/Unicode issues on Windows)
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

# Fix protobuf compatibility with Python 3.12 and robotframework-browser
# This uses pure Python protobuf parsing (slower but compatible)
export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

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

# Support both APP_PORT (new) and PORT (legacy) variables with a sane default
APP_PORT="${APP_PORT:-${PORT:-5000}}"
export APP_PORT
export PORT="$APP_PORT"

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

# Run the application
echo "Starting the application..."
echo "You can access it at http://localhost:${APP_PORT}"
python -m uvicorn src.backend.main:app --host 0.0.0.0 --port "${APP_PORT}" &
UVICORN_PID=$!

python tools/browser_use_service.py > bus.log 2>&1 &
BROWSER_SERVICE_PID=$!

cleanup() {
    kill "$UVICORN_PID" "$BROWSER_SERVICE_PID" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

wait -n "$UVICORN_PID" "$BROWSER_SERVICE_PID"
EXIT_CODE=$?
cleanup
wait "$UVICORN_PID" "$BROWSER_SERVICE_PID" 2>/dev/null || true
exit "$EXIT_CODE"