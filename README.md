# Mark 1 - Natural Language to Robot Framework

![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)
![Python](https://img.shields.io/badge/Python-3.9%2B-green.svg)
![Robot Framework](https://img.shields.io/badge/Robot%20Framework-6.0%2B-orange.svg)
![Docker](https://img.shields.io/badge/Docker-Required-blue.svg)

Transform natural language test descriptions into executable Robot Framework code using advanced AI agents. Mark 1 is an intelligent test automation platform that bridges the gap between human language and automated testing.

## ✨ Key Features

- 🤖 **Multi-Agent AI System**: Sophisticated pipeline with specialized agents for planning, identification, validation, and self-correction
- 🔄 **Self-Healing Tests**: Automatic error detection and correction through intelligent validation loops
- 🐳 **Containerized Execution**: Clean, isolated test runs in Docker containers for consistent results
- 🌐 **Flexible Model Support**: Works with both local LLMs (via Ollama) and cloud models (Google Gemini)
- 📊 **Detailed Reporting**: Comprehensive HTML logs and reports for easy debugging
- ⚡ **Real-time Streaming**: Live progress updates and instant feedback
- 🎯 **Smart Element Detection**: AI-powered web element locator generation

## 🏗️ Architecture

Mark 1 employs a sophisticated multi-agent workflow:

1. **Step Planner Agent** - Decomposes natural language into structured test plans
2. **Element Identifier Agent** - Generates optimal web element locators
3. **Code Assembler Agent** - Creates syntactically correct Robot Framework code
4. **Validator Agent** - Ensures code quality and correctness
5. **Self-Correction Orchestrator** - Automatically fixes validation errors

## 🚀 Quick Start

### Prerequisites

- **Python 3.9+** - [Download Python](https://python.org/downloads/)
- **Docker** - [Install Docker](https://docs.docker.com/get-docker/)
- **Git** - [Install Git](https://git-scm.com/downloads)
- **(Optional) Ollama** - For local AI models [Install Ollama](https://ollama.com/)

### Installation

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd natural-language-robot-framework
   ```

2. **Configure your AI model**
   ```bash
   cp src/backend/.env.example src/backend/.env
   ```

   **For Google Gemini (Recommended)**:
   ```env
   MODEL_PROVIDER=online
   GEMINI_API_KEY="your-gemini-api-key-here"
   ONLINE_MODEL=gemini-1.5-pro-latest
   ```

   **For Local Models (Ollama)**:
   ```env
   MODEL_PROVIDER=local
   LOCAL_MODEL=llama3
   ```

3. **Start the application**
   ```bash
   # Linux/macOS
   chmod +x run.sh
   ./run.sh

   # Windows (Git Bash)
   bash run.sh
   ```

4. **Access the web interface**
   Open your browser to `http://localhost:5000`

## 💡 Usage Examples

Simply describe your test in natural language:

### Basic Examples
- *"Open Google, search for 'Robot Framework tutorials', and click the first result"*
- *"Navigate to GitHub, search for 'selenium automation', and star the top repository"*
- *"Go to YouTube, search for 'Python automation', and play the first video"*

### Advanced Examples
- *"Visit Amazon, search for 'wireless headphones', filter by rating above 4 stars, and add the top result to cart"*
- *"Open LinkedIn, search for 'QA Engineer' jobs in San Francisco, and apply filters for remote work"*
- *"Navigate to Stack Overflow, search for 'pytest fixtures', and upvote the most helpful answer"*

## 🛠️ Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `MODEL_PROVIDER` | AI model provider (`online` or `local`) | `online` |
| `GEMINI_API_KEY` | Google Gemini API key | Required for online |
| `ONLINE_MODEL` | Gemini model name | `gemini-1.5-pro-latest` |
| `LOCAL_MODEL` | Ollama model name | `llama3` |
| `SECONDS_BETWEEN_API_CALLS` | Rate limiting delay | `0` |

### Getting API Keys

**Google Gemini API Key**:
1. Visit [Google AI Studio](https://aistudio.google.com/app/apikey)
2. Create a new API key
3. Copy the key to your `.env` file

## 🧪 Testing

Test the API endpoint directly:

```bash
chmod +x test.sh
./test.sh
```

Or use curl:

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{"query": "go to google.com and search for python tutorials", "model": "gemini-1.5-pro-latest"}' \
  http://localhost:5000/generate-and-run
```

## 📁 Project Structure

```
├── src/
│   ├── backend/
│   │   ├── api/
│   │   │   └── endpoints.py       # API endpoints
│   │   ├── core/
│   │   │   └── config.py          # Configuration management
│   │   ├── services/
│   │   │   ├── docker_service.py  # Docker-related services
│   │   │   └── workflow_service.py# Agentic workflow services
│   │   ├── crew_ai/               # CrewAI agents and tasks
│   │   ├── main.py                # FastAPI application entry point
│   │   └── requirements.txt       # Python dependencies
│   └── frontend/
│       ├── index.html             # Web interface
│       ├── script.js              # Frontend logic
│       └── style.css              # Styling
├── robot_tests/                   # Generated test files and reports
├── tests/                         # Backend unit tests
├── run.sh                         # Startup script
└── test.sh                        # Testing script
```

## 🐛 Debugging

When tests fail, detailed logs are automatically saved:

- `robot_tests/{run_id}/log.html` - Step-by-step execution log
- `robot_tests/{run_id}/report.html` - High-level test report
- `robot_tests/{run_id}/output.xml` - Machine-readable results

Open `log.html` in your browser for comprehensive failure analysis.

## 🤝 Contributing

We welcome contributions! Please see our [Contributing Guide](CONTRIBUTING.md) for details.

### Development Setup

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Make your changes
4. Run tests: `./test.sh`
5. Commit: `git commit -m 'Add amazing feature'`
6. Push: `git push origin feature/amazing-feature`
7. Open a Pull Request

Before contributing, please read our [Contributor License Agreement](CLA.md).

## 📄 License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

## 🆘 Support

- 📚 **Documentation**: Check this README and code comments
- 🐛 **Issues**: Report bugs via GitHub Issues
- 💬 **Discussions**: Join our GitHub Discussions
- 📧 **Email**: Contact the maintainers

## 🎯 Roadmap

- [ ] Support for mobile app testing
- [ ] Integration with CI/CD pipelines
- [ ] Visual test result dashboards
- [ ] Multi-language test generation
- [ ] Advanced AI model fine-tuning
- [ ] Cloud deployment options

## ⭐ Show Your Support

If Mark 1 helps streamline your testing workflow, please consider:
- ⭐ Starring this repository
- 🐛 Reporting issues
- 💡 Suggesting new features
- 🤝 Contributing code

---

**Built with ❤️ for the test automation community**
