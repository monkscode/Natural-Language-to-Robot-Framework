# Use a lightweight Debian-based image with Python
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install Google Chrome and other dependencies in a single layer for optimization
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    ca-certificates \
    xvfb \
    x11vnc \
    fluxbox \
    dbus-x11 \
    curl \
    unzip && \
    wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome-keyring.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome-keyring.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends google-chrome-stable && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/*

# Install ChromeDriver for Selenium using webdriver-manager (handles version automatically)
# Note: webdriver-manager will download ChromeDriver on first use

# Install Robot Framework with both SeleniumLibrary and Browser Library
RUN pip install --no-cache-dir \
    robotframework \
    robotframework-seleniumlibrary \
    selenium \
    webdriver-manager \
    robotframework-browser[bb]

# Install Chromium browser for Browser Library
RUN rfbrowser install chromium

# Create virtual display script for headless operation
RUN echo '#!/bin/bash\n\
    if [ "$HEALING_SESSION" = "true" ]; then\n\
    export DISPLAY=:99\n\
    Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset &\n\
    sleep 2\n\
    fi\n\
    exec "$@"' > /usr/local/bin/start-with-display.sh && \
    chmod +x /usr/local/bin/start-with-display.sh

# Set Chrome options for healing sessions
ENV CHROME_OPTIONS="--headless --no-sandbox --disable-dev-shm-usage --disable-gpu --window-size=1920,1080"

# Use the display script as entrypoint
ENTRYPOINT ["/usr/local/bin/start-with-display.sh"]
