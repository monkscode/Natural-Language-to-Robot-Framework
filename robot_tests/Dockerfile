# Use a base image with Python
FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Install Google Chrome
RUN apt-get update && apt-get install -y wget gnupg
RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add -
RUN sh -c 'echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google-chrome.list'
RUN apt-get -y update
RUN apt-get install -y google-chrome-stable

# Install Robot Framework and SeleniumLibrary
RUN pip install robotframework robotframework-seleniumlibrary

# Copy the test file into the container
COPY . .

# The command to run the test will be provided by the docker run command
CMD ["robot", "test.robot"]
