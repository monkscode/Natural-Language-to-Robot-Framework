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
PORT=5000
echo "Attempting to kill any process running on port $PORT..."
if command -v lsof &> /dev/null
then
    PID=$(lsof -t -i:$PORT)
    if [ -n "$PID" ]
    then
        kill $PID
    fi
else
    echo "lsof command not found, skipping process kill. Please ensure no process is running on port $PORT."
fi

echo "Starting the application..."
echo "You can access it at http://localhost:$PORT"
uvicorn backend.main:app --host 0.0.0.0 --port $PORT
