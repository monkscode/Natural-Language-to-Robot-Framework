#!/bin/bash

# Check for .env file
if [ ! -f "backend/.env" ]; then
    echo "Error: backend/.env file not found."
    echo "Please copy backend/.env.example to backend/.env and fill in your API key."
    exit 1
fi

# Create and activate virtual environment
VENV_DIR=".venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment in $VENV_DIR..."
    python3 -m venv $VENV_DIR
fi

echo "Activating virtual environment..."
source $VENV_DIR/bin/activate

# Install dependencies
echo "Installing dependencies into virtual environment..."
pip install -r backend/requirements.txt

# Run the application
echo "Starting the application..."
echo "You can access it at http://localhost:5000"
python backend/app.py

# Deactivate the virtual environment on exit (e.g., when server is stopped with Ctrl+C)
deactivate
