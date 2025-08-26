#!/bin/bash

# Check for .env file
if [ ! -f "backend/.env" ]; then
    echo "Error: backend/.env file not found."
    echo "Please copy backend/.env.example to backend/.env and fill in your API key."
    exit 1
fi

# Install dependencies
echo "Installing dependencies..."
pip install -r backend/requirements.txt

# Run the application
echo "Starting the application..."
echo "You can access it at http://localhost:5000"
uvicorn backend.main:app --host 0.0.0.0 --port 5000
