# Docker Build Guide

## Quick Start

### Build the Image

```bash
# Standard build
docker build -t robot-test-runner:latest .

# Build with BuildKit (recommended - faster with caching)
DOCKER_BUILDKIT=1 docker build -t robot-test-runner:latest .

# Build with progress output
docker build --progress=plain -t robot-test-runner:latest .
```

## What's Included

### Browsers
- ✅ **Google Chrome Stable** - For SeleniumLibrary tests
- ✅ **Chromium (Playwright)** - For Browser Library tests

### Robot Framework Libraries
- ✅ **robotframework** - Core framework
- ✅ **robotframework-seleniumlibrary** - Selenium-based web testing
- ✅ **robotframework-browser** - Playwright-based web testing

### Python Packages
- ✅ **playwright** - Modern browser automation (via Browser Library)

### System Tools
- ✅ **Xvfb** - Virtual display for headless browser execution
- ✅ **Python 3.12** - Latest stable Python

## Build Time

### First Build
- **Without cache:** ~6-8 minutes
- **With BuildKit cache:** ~4-6 minutes

### Subsequent Builds
- **With layer cache:** ~30-60 seconds
- **With --no-cache:** ~3-4 minutes (cache mounts still help)

## Testing the Image

### Test Chrome
```bash
docker run --rm robot-test-runner:latest google-chrome --version
```

### Test Robot Framework
```bash
docker run --rm robot-test-runner:latest robot --version
```

### Test SeleniumLibrary
```bash
docker run --rm robot-test-runner:latest python -c "import SeleniumLibrary; print(SeleniumLibrary.__version__)"
```

### Test Browser Library
```bash
docker run --rm robot-test-runner:latest python -c "from Browser import Browser; print('Browser Library OK')"
```

### Run a Test
```bash
docker run --rm \
  -v $(pwd)/robot_tests:/app/robot_tests \
  robot-test-runner:latest \
  robot --outputdir /app/robot_tests/results /app/robot_tests/your_test.robot
```

## Troubleshooting

### Build Fails with "No space left on device"
```bash
# Clean up Docker
docker system prune -a
docker builder prune -a

# Check available space
docker system df
```

### Build is Slow
```bash
# Use BuildKit for faster builds
export DOCKER_BUILDKIT=1
docker build -t robot-test-runner:latest .

# Or on Windows PowerShell
$env:DOCKER_BUILDKIT=1
docker build -t robot-test-runner:latest .
```

### Chrome/Chromium Not Working
```bash
# Verify installations
docker run --rm robot-test-runner:latest google-chrome --version
docker run --rm robot-test-runner:latest python -c "import os; print(os.path.exists('/root/.cache/ms-playwright'))"
```

### Image Size Too Large
```bash
# Check layer sizes
docker history robot-test-runner:latest

# Use dive for detailed analysis
docker run --rm -it \
  -v /var/run/docker.sock:/var/run/docker.sock \
  wagoodman/dive robot-test-runner:latest
```

## Optimization Tips

### 1. Use BuildKit
BuildKit provides better caching and parallel builds:
```bash
export DOCKER_BUILDKIT=1
```

### 2. Don't Rebuild Unnecessarily
The image includes everything needed for test execution. Only rebuild when:
- Updating Robot Framework versions
- Adding new libraries
- Updating browser versions

### 3. Use Multi-Stage Builds (Advanced)
For even smaller images, consider separating build and runtime stages.

### 4. Layer Caching
Docker caches each layer. Order matters:
1. System dependencies (changes rarely)
2. Python packages (changes occasionally)
3. Browser installations (changes rarely)
4. Application code (changes frequently)

## Environment Variables

The image supports these environment variables:

```bash
# Enable healing session with virtual display
HEALING_SESSION=true

# Chrome options (already set)
CHROME_OPTIONS="--headless=new --no-sandbox --disable-dev-shm-usage --disable-gpu"

# Playwright browser path (already set)
PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright
```

## Maintenance

### Update Base Image
```bash
docker pull python:3.12-slim
docker build --no-cache -t robot-test-runner:latest .
```

### Update Browsers
Rebuild the image to get latest Chrome and Chromium versions:
```bash
docker build --no-cache -t robot-test-runner:latest .
```

### Clean Up Old Images
```bash
# Remove old versions
docker images | grep robot-test-runner

# Remove specific version
docker rmi robot-test-runner:old-version

# Remove all unused images
docker image prune -a
```

## Support

If you encounter issues:
1. Check Docker logs: `docker logs <container-id>`
2. Inspect the image: `docker run --rm -it robot-test-runner:latest /bin/sh`
3. Review build logs with `--progress=plain`
