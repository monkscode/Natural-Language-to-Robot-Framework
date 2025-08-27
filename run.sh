#!/bin/bash

# Check for .env file
if [ ! -f "backend/.env" ]; then
    echo "Error: backend/.env file not found."
    echo "Please copy backend/.env.example to backend/.env and fill in your API key."
    exit 1
fi

# Create a virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate the virtual environment and install dependencies
echo "Activating virtual environment and installing dependencies..."
source venv/bin/activate
pip install -r backend/requirements.txt

# Run the application
echo "Starting the application..."
echo "You can access it at http://localhost:5000"
uvicorn backend.main:app --host 0.0.0.0 --port 5000
