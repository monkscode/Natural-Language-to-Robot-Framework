# Natural Language to Robot Framework

This project is a web application that converts natural language commands into Robot Framework code. The application uses the `browser-use` library and Google's Gemini API to understand the user's query and generate the corresponding Robot Framework steps. The generated code is then executed in a Docker container, and the results are displayed to the user.

## Features

-   Convert natural language to Robot Framework code.
-   Execute the generated code in a Docker container.
-   User-friendly web interface.
-   Downloadable Robot Framework files.

## Setup

1.  **Clone the repository:**
    ```bash
    git clone <repository-url>
    cd <repository-directory>
    ```

2.  **Create the environment file:**
    Copy the `backend/.env.example` file to `backend/.env`:
    ```bash
    cp backend/.env.example backend/.env
    ```
    Open `backend/.env` and add your Google API key.

3.  **Make the scripts executable:**
    ```bash
    chmod +x run.sh
    chmod +x test.sh
    ```

## Usage

1.  **Run the application:**
    ```bash
    ./run.sh
    ```
    This script will automatically create a Python virtual environment, install the required dependencies into it, and then start the web application on `http://localhost:5000`.

2.  **Test the application:**
    Open a new terminal and run the test script:
    ```bash
    ./test.sh
    ```
    This will send a sample request to the running application and print the response.

## How it Works

1.  The user enters a natural language query in the web interface.
2.  The frontend sends the query to the backend.
3.  The backend uses the `browser-use` library and the Google Gemini API to convert the query into a sequence of Robot Framework steps.
4.  The backend generates a `.robot` file with the generated steps.
5.  The backend builds a Docker image for the Robot Framework environment and runs the generated test file in a container.
6.  The backend returns the generated Robot Framework code and the execution logs to the frontend.
7.  The frontend displays the results to the user.
