#!/bin/bash

# Force UTF-8 encoding for all Python operations (fixes emoji/Unicode issues on Windows)
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

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
IS_WINDOWS=false
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
    # Windows (using Git Bash, Cygwin, etc.)
    VENV_ACTIVATE="$VENV_DIR/Scripts/activate"
    IS_WINDOWS=true
    # Fix Windows long path issue for Playwright browsers
    # Use a short path to avoid ENOENT errors on Windows
    export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-C:/pw-browsers}"
else
    # Linux, macOS
    VENV_ACTIVATE="$VENV_DIR/bin/activate"
fi

# Function to install browser dependencies
install_browser_deps() {
    echo "Installing Playwright browsers..."
    python -m playwright install chromium || {
        echo "Warning: Failed to install Playwright via python module, trying alternative..."
        pip install playwright && python -m playwright install chromium
    }
    
    echo "Initializing Robot Framework Browser library..."
    # rfbrowser init can fail on Windows due to path issues, so we handle errors gracefully
    if rfbrowser init 2>/dev/null; then
        echo "rfbrowser initialized successfully."
    else
        echo "Warning: rfbrowser init encountered issues. Trying with skip-browsers flag..."
        rfbrowser init --skip-browsers 2>/dev/null || {
            echo "Warning: rfbrowser init failed. Browser-based locator finding may not work."
            echo "You can try running 'rfbrowser init' manually after installation."
        }
    fi
}

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
    
    echo "Installing uv package manager..."
    pip install --upgrade pip
    pip install uv
    
    echo "Installing dependencies..."
    # Use uv for faster installation, with fallback to pip
    if uv pip install -r src/backend/requirements.txt; then
        echo "Dependencies installed successfully with uv."
    else
        echo "uv installation failed, falling back to pip..."
        pip install -r src/backend/requirements.txt
    fi
    
    # Install browser dependencies
    install_browser_deps
fi


# Run the application
echo "Starting the application..."
echo "You can access it at http://localhost:${APP_PORT}"
python -m uvicorn src.backend.main:app --host 0.0.0.0 --port "${APP_PORT}"
