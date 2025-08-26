#!/bin/bash

echo "Testing the /generate-and-run endpoint..."

curl -X POST \
     -H "Content-Type: application/json" \
     -d '{"query": "Go to https://www.google.com/search?q=Robot+Framework and then close the browser."}' \
     http://localhost:5000/generate-and-run-test

echo -e "\n\nTest complete."
