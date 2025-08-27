#!/bin/bash

echo "Testing the /generate-and-run endpoint with gemini-2.5-pro..."

curl -X POST \
     -H "Content-Type: application/json" \
     -d '{"query": "go to youtube.com, search for \"funny cat videos\", and click the first video", "model": "gemini-2.5-pro"}' \
     http://localhost:5000/generate-and-run

echo -e "\n\nTest complete."
