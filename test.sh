#!/bin/bash

echo "Testing the /generate-and-run endpoint..."

curl -X POST \
     -H "Content-Type: application/json" \
     -d '{"query": "Open browser to google.com, search for '\''Robot Framework'\'', and then close the browser."}' \
     http://localhost:5000/generate-and-run

echo -e "\n\nTest complete."
