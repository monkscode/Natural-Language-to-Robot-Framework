FROM python:3.9-slim

# Install Robot Framework and SeleniumLibrary
RUN pip install robotframework robotframework-seleniumlibrary

# Install browser dependencies
RUN apt-get update && apt-get install -y \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    --no-install-recommends

# Install Chromium
RUN apt-get install -y chromium --no-install-recommends

# Create a non-root user
RUN useradd -ms /bin/bash robot
USER robot
WORKDIR /home/robot/tests
