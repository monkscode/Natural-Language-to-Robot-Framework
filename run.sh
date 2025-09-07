#!/bin/bash

# Check for .env file
if [ ! -f "backend/.env" ]; then
    echo "Error: backend/.env file not found."
    echo "Please copy backend/.env.example to backend/.env and fill in your API key."
    exit 1
fi

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
    uv pip install -r backend/requirements.txt
fi

# Run the application
echo "Starting the application..."
echo "You can access it at http://localhost:5000"
uvicorn backend.main:app --host 0.0.0.0 --port 5000
