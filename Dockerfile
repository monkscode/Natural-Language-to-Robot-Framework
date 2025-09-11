# Use a lightweight Debian-based image with Python
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install Google Chrome and other dependencies in a single layer for optimization
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        wget \
        gnupg \
        ca-certificates && \
    wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome-keyring.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome-keyring.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends google-chrome-stable && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/*

# Install Robot Framework and SeleniumLibrary
RUN pip install --no-cache-dir robotframework robotframework-seleniumlibrary
