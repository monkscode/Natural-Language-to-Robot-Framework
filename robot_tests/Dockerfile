# Use a base image with Python
FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Install Google Chrome and other dependencies
RUN apt-get update && \
    apt-get install -y wget gnupg && \
    wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome-keyring.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome-keyring.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && \
    apt-get install -y google-chrome-stable && \
    # Clean up apt lists to reduce image size
    rm -rf /var/lib/apt/lists/*

# Install Robot Framework and SeleniumLibrary
RUN pip install robotframework robotframework-seleniumlibrary

# Copy the test file into the container
COPY . .

# The command to run the test will be provided by the docker run command
CMD ["robot", "test.robot"]
