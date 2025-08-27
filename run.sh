#!/bin/bash

# Check for .env file
if [ ! -f "backend/.env" ]; then
    echo "Error: backend/.env file not found."
    echo "Please copy backend/.env.example to backend/.env and fill in your API key."
    exit 1
fi

# Check if venv exists and is valid
if [ -d "venv" ] && [ -f "venv/bin/activate" ]; then
    echo "Using existing virtual environment..."
    source venv/bin/activate
else
    echo "Creating new virtual environment..."
    # Remove invalid venv if it exists
    [ -d "venv" ] && rm -rf venv
    python -m venv venv
    source venv/bin/activate
    echo "Installing dependencies..."
    pip install -r backend/requirements.txt
fi

# Run the application
echo "Starting the application..."
echo "You can access it at http://localhost:5000"
uvicorn backend.main:app --host 0.0.0.0 --port 5000
