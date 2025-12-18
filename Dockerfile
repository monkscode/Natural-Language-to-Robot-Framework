# Optimized Dockerfile - Aggressive Size Reduction
# Python 3.12 + UV package manager + BuildKit caching + System Chrome Only
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Prevent Python from writing pyc files and enable unbuffered output
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright \
    CHROME_BIN=/usr/bin/google-chrome

# Install UV package manager (10-100x faster than pip)
RUN pip install --no-cache-dir uv

# Install system dependencies with BuildKit cache support for faster rebuilds
# Combined layer for install + cleanup to reduce image size
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    ca-certificates \
    xvfb \
    dbus-x11 \
    curl \
    # Required system libraries for Chrome/Playwright
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libwayland-client0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    nodejs \
    npm && \
    # Install Google Chrome
    wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome-keyring.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome-keyring.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends google-chrome-stable && \
    # Cleanup build tools and lists to reduce layer size
    apt-get purge -y --auto-remove wget gnupg curl && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/* /usr/share/doc/* /usr/share/man/* /tmp/*

# Install Robot Framework packages using UV (much faster than pip)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --no-cache \
    robotframework \
    robotframework-seleniumlibrary \
    robotframework-browser[bb]

# Initialize rfbrowser node dependencies but SKIP browser binary download
# We use the system-installed google-chrome-stable instead
RUN rfbrowser init --skip-browsers

# Create optimized virtual display script using sh (smaller than bash)
RUN printf '#!/bin/sh\n\
    if [ "$HEALING_SESSION" = "true" ]; then\n\
    export DISPLAY=:99\n\
    Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset &\n\
    sleep 1\n\
    fi\n\
    exec "$@"\n' > /usr/local/bin/start-with-display.sh && \
    chmod +x /usr/local/bin/start-with-display.sh

# Set Chrome options for healing sessions
ENV CHROME_OPTIONS="--headless=new --no-sandbox --disable-dev-shm-usage --disable-gpu --window-size=1920,1080"

# Use the display script as entrypoint
ENTRYPOINT ["/usr/local/bin/start-with-display.sh"]

# Build with: DOCKER_BUILDKIT=1 docker build -t robot-test-runner:latest .
