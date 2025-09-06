# Natural Language to Robot Framework

This project is a sophisticated, AI-driven framework that translates natural language test cases into executable Robot Framework code. It leverages a multi-agent system to interpret, plan, validate, and self-correct test automation scripts, providing a seamless experience for testers and developers.

---

## Key Features

-   **Natural Language Conversion:** Simply describe a test case in plain English, and Mark 1 will generate the corresponding Robot Framework script.
-   **Multi-Agent System:** Utilizes a robust pipeline of specialized AI agents (Planner, Identifier, Validator) to ensure high-quality code generation.
-   **Self-Correction Loop:** If the generated code is syntactically incorrect, the system automatically attempts to fix its own mistakes, significantly improving reliability.
-   **Containerized Execution:** Tests are executed in a clean, isolated Docker container, guaranteeing consistency and eliminating the "it works on my machine" problem.
-   **Flexible Model Support:** Seamlessly switch between local LLMs (via Ollama) and powerful online models like Google's Gemini for maximum flexibility.
-   **Detailed Debugging:** Automatically saves detailed HTML logs and reports from every test run, allowing for easy inspection and debugging of test failures.

---

## Architecture Overview

Mark 1 employs a sophisticated multi-agent workflow to ensure the generated code is both accurate and robust.

1.  **Step Planner Agent:** The initial agent receives the natural language query. Its job is to decompose the query into a high-level, structured plan, identifying each distinct action required for the test case.
2.  **Element Identifier Agent:** This agent takes the plan and, for each step involving a UI element, uses an AI model to determine the best and most stable web locator (e.g., CSS selector, XPath).
3.  **Code Assembler Agent:** A deterministic agent that assembles the planned and located steps into a syntactically-structured `.robot` file.
4.  **Validator Agent:** Before execution, this crucial agent inspects the generated code against a set of rules (e.g., ensuring keywords have the correct arguments).
5.  **Self-Correction Orchestrator:** If the Validator Agent finds a flaw, the orchestrator feeds the error back to the Step Planner Agent, which then attempts to generate a corrected plan. This loop can run multiple times to resolve errors automatically.

---

## Getting Started

### Prerequisites

-   **Docker:** Required for running the containerized test execution environment. [Install Docker](https://docs.docker.com/get-docker/).
-   **Python 3.9+**
-   **(Optional) Ollama:** If you wish to use a local LLM, you must have Ollama installed and running. [Install Ollama](https://ollama.com/).

### Installation & Setup

1.  **Clone the Repository**
    ```bash
    git clone <repository-url>
    cd <repository-directory>
    ```

2.  **Configure Environment Variables**
    Copy the example `.env` file:
    ```bash
    cp backend/.env.example backend/.env
    ```
    Now, edit `backend/.env` to configure your desired model provider.

    **For Local Models (via Ollama):**
    ```dotenv
    MODEL_PROVIDER=local
    LOCAL_MODEL=llama3 # The name of the model you have pulled with Ollama
    ```

    **For Online Models (Google Gemini):**
    ```dotenv
    MODEL_PROVIDER=online
    GEMINI_API_KEY="YOUR_GEMINI_API_KEY"
    # ONLINE_MODEL=gemini-1.5-pro-latest # (Optional) Specify a different Gemini model
    ```

3.  **Make Scripts Executable** (for Linux/macOS)
    ```bash
    chmod +x run.sh test.sh
    ```

### Running the Application

1.  **Start the Server**
    ```bash
    ./run.sh
    ```
    This script automatically creates a Python virtual environment, installs all dependencies, and starts the FastAPI server on `http://localhost:5000`.

2.  **Access the Web Interface**
    Open your browser and navigate to `http://localhost:5000`.

---

## Usage

1.  Enter your test case in plain English in the text area (e.g., "search for the latest news on google and verify the title").
2.  Click "Generate and Run".
3.  The backend will process the query, generate the Robot Framework code, and execute it in a Docker container.
4.  The results, including the generated code and execution logs, will be displayed on the page.

### Debugging Failed Tests

If a test fails, you can find detailed information in the `robot_tests` directory. The framework automatically saves the following files from the container to your local machine:
-   `log.html`: A detailed, step-by-step log of the test execution with expandable views.
-   `report.html`: A high-level report of the test run.
-   `output.xml`: The machine-readable XML output from Robot Framework.

Simply open `log.html` in your browser to get a complete picture of the failure.

---

## License

This project is licensed under the Apache License, Version 2.0. See the [LICENSE](LICENSE) file for details.

## Contributing

We welcome contributions to this project! Please see the [CONTRIBUTING.md](CONTRIBUTING.md) file for details on how to get started and to review our contributor license agreement.
