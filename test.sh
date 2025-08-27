#!/bin/bash

echo "Testing the /generate-and-run endpoint..."

curl -X POST \
     -H "Content-Type: application/json" \
     -d '{"query": "search for cats on google"}' \
     http://localhost:5000/generate-and-run-test

echo -e "\n\nTest complete."
