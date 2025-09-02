# Natural Language to Robot Framework

This project is a web application that converts natural language commands into Robot Framework code. The application uses a local Large Language Model (LLM) served by Ollama to understand the user's query and generate the corresponding Robot Framework steps. The generated code is then executed in a Docker container, and the results are displayed to the user.

## Features

-   Convert natural language to Robot Framework code.
-   Execute the generated code in a Docker container.
-   User-friendly web interface.
-   Downloadable Robot Framework files.

## Setup

### 1. Install Ollama

This project uses a local LLM served by [Ollama](https://ollama.ai/).

1.  Download and install Ollama from the official website.
2.  Pull the model you want to use. We recommend `llama3`.
    ```bash
    ollama pull llama3
    ```
3.  Ensure the Ollama server is running.

### 2. Clone the repository

```bash
git clone <repository-url>
cd <repository-directory>
```

### 3. Configure the environment

Copy the `backend/.env.example` file to `backend/.env`:
```bash
cp backend/.env.example backend/.env
```
The `.env` file is used to configure the Ollama model and base URL. The default values are:
```
OLLAMA_MODEL=llama3
OLLAMA_BASE_URL=http://localhost:11434
```
You can change these values if needed.

### 4. Make the scripts executable
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
3.  The backend enhances the query using a local LLM (via Ollama) to create a detailed, step-by-step plan.
4.  The backend uses the `browser-use` library, powered by the same local LLM, to convert the enhanced query into a structured list of Robot Framework steps.
5.  The backend uses a Jinja2 template to generate a `.robot` file from the structured steps. This is a deterministic process that ensures the generated code is always valid.
6.  The backend builds a dedicated Docker image for the test environment and runs the generated `.robot` file in a container.
7.  The backend returns the generated Robot Framework code and the execution logs to the frontend.
8.  The frontend displays the results to the user.
