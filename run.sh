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

# --- Mode: dev (default) or bench ("./run.sh bench") ---
# Both modes are process-env overlays on src/backend/.env — the file is never
# edited. Docker Compose reads src/backend/.env directly and does NOT source
# run.sh, so the on-disk values remain authoritative for containers.
MODE="${1:-dev}"
if [ "$MODE" != "dev" ] && [ "$MODE" != "bench" ]; then
    echo "Usage: ./run.sh [bench]"
    exit 1
fi

if [ "$MODE" = "bench" ]; then
    # Bench pins (bench/README.md): a baseline is only comparable to runs
    # pinned the same way.
    export OPTIMIZATION_ENABLED=false
    export BROWSER_HEADLESS=true
    export LOG_FORMAT=json
    # Refuse to start over a live stack — otherwise the address-in-use error
    # is buried in a child process log and the bench hits mixed pins.
    # netstat portability: Windows prints "LISTENING", Linux/macOS "LISTEN";
    # macOS separates the port with "." not ":"; -o is Windows/Linux-only.
    for port in 5000 4999 4998; do
        if netstat -an | grep "LISTEN" | grep -Eq "[:.]${port}[[:space:]]"; then
            echo "Error: port ${port} already in use — is the dev stack still running? Stop it first."
            exit 1
        fi
    done
    echo "=============================================================="
    echo "  BENCH MODE: learning OFF, headless browser, JSON logs,"
    echo "  frontend skipped. src/backend/.env untouched."
    echo "=============================================================="
else
    # Local-dev overrides (run.sh process only, never containers)
    export BROWSER_HEADLESS=false
    export LOG_FORMAT=console          # human-readable colored logs
    # export CREWAI_VERBOSE=true         # show agent reasoning in console
fi

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

# A venv is only as fresh as the manifest it was built from. Both blocks below used to
# install ONLY when the venv was missing, so editing a requirements file left an existing
# venv silently stale — and the bench then measures package versions the manifest does
# not claim. That is how the playwright bench-vs-container fork could reappear after
# being closed. Each venv now stores a hash of its manifest and reinstalls in place when
# it changes. In place, never renamed: uv console-script trampolines hardcode an absolute
# interpreter path.
_req_hash() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

BACKEND_REQ="src/backend/requirements.txt"
VENV_STAMP="$VENV_DIR/.requirements-hash"
BACKEND_HASH="$(_req_hash "$BACKEND_REQ")"

# Check if venv exists and is valid
if [ -d "$VENV_DIR" ] && [ -f "$VENV_ACTIVATE" ]; then
    echo "Using existing virtual environment..."
    source "$VENV_ACTIVATE"
    if [ "$(cat "$VENV_STAMP" 2>/dev/null)" != "$BACKEND_HASH" ]; then
        echo "$BACKEND_REQ changed — updating virtual environment..."
        command -v uv >/dev/null 2>&1 || pip install uv
        uv pip install -r "$BACKEND_REQ"
        echo "$BACKEND_HASH" > "$VENV_STAMP"
    fi
else
    echo "Creating new virtual environment..."
    # Remove invalid venv if it exists
    [ -d "$VENV_DIR" ] && rm -rf "$VENV_DIR"
    python -m venv "$VENV_DIR"
    source "$VENV_ACTIVATE"
    echo "Installing dependencies..."
    pip install uv
    uv pip install -r "$BACKEND_REQ"
    uv pip install pytest pytest-asyncio pytest-cov
    echo "$BACKEND_HASH" > "$VENV_STAMP"
fi

# --- Browser-use service venv (isolated — see requirements-bus.txt header) ---
BUS_VENV_DIR="venv-bus"
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
    BUS_PY="$BUS_VENV_DIR/Scripts/python.exe"
else
    BUS_PY="$BUS_VENV_DIR/bin/python"
fi
BUS_STAMP="$BUS_VENV_DIR/.requirements-hash"
BUS_HASH="$(_req_hash requirements-bus.txt)"
if [ ! -f "$BUS_PY" ]; then
    echo "Creating browser-service virtual environment..."
    [ -d "$BUS_VENV_DIR" ] && rm -rf "$BUS_VENV_DIR"
    python -m venv "$BUS_VENV_DIR"
    "$BUS_PY" -m pip install -r requirements-bus.txt
    "$BUS_PY" -m playwright install chromium
    echo "$BUS_HASH" > "$BUS_STAMP"
elif [ "$(cat "$BUS_STAMP" 2>/dev/null)" != "$BUS_HASH" ]; then
    # Reinstall the browser too: the pin that changed may be playwright itself, and the
    # engine is what the locator stack resolves against.
    echo "requirements-bus.txt changed — updating browser-service virtual environment..."
    "$BUS_PY" -m pip install -r requirements-bus.txt
    "$BUS_PY" -m playwright install chromium
    echo "$BUS_HASH" > "$BUS_STAMP"
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

# --- React SPA dev server (Vite, :5173) — dev mode only ---
if [ "$MODE" != "bench" ] && [ ! -d "src/frontend-react/node_modules" ]; then
    echo "Installing frontend dependencies (first run)..."
    (cd src/frontend-react && npm install)
fi

# Phase 4: the socket-holding executor (dev/prod parity — FastAPI no longer
# touches Docker directly).
uvicorn src.backend.runner_exec.app:app --host 127.0.0.1 --port 4998 &
RUNNER_EXEC_PID=$!

# Run the application
echo "Starting the application..."
python -m uvicorn src.backend.main:app --host 0.0.0.0 --port "${APP_PORT}" &
UVICORN_PID=$!

"$BUS_PY" tools/browser_use_service.py > bus.log 2>&1 &
BROWSER_SERVICE_PID=$!

PIDS=("$RUNNER_EXEC_PID" "$UVICORN_PID" "$BROWSER_SERVICE_PID")

if [ "$MODE" != "bench" ]; then
    # vite.js is run with node directly (not 'npm run dev') and exec'd so the PID
    # we kill on exit is the actual dev-server process, not a wrapper around it.
    (cd src/frontend-react && exec node node_modules/vite/bin/vite.js) > frontend.log 2>&1 &
    PIDS+=("$!")
fi

echo ""
if [ "$MODE" != "bench" ]; then
    echo "  React SPA (validate here):  http://localhost:5173"
fi
echo "  FastAPI backend (API only):  http://localhost:${APP_PORT}"
echo "  BrowserUse service:          http://localhost:4999/health"
echo "  Postgres:                    localhost:5432 (container nlrf-postgres)"
echo ""
echo "  Logs: backend in this console; bus.log (BrowserUse); frontend.log (Vite)"
echo "  Press Ctrl+C to stop everything (Postgres container stays up)."
echo ""

cleanup() {
    kill "${PIDS[@]}" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

if ((BASH_VERSINFO[0] >= 4)); then
    # Bash 4+: block until the first background service exits.
    wait -n "${PIDS[@]}"
    EXIT_CODE=$?
else
    # macOS ships Bash 3.2, which lacks `wait -n`: block until all exit.
    wait "${PIDS[@]}"
    EXIT_CODE=$?
fi
cleanup
wait "${PIDS[@]}" 2>/dev/null || true
exit "$EXIT_CODE"