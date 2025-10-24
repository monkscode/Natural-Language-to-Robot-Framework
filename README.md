# 🤖 Mark 1 - Natural Language to Robot Framework

![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)
![Python](https://img.shields.io/badge/Python-3.9%2B-green.svg)
![Robot Framework](https://img.shields.io/badge/Robot%20Framework-6.0%2B-orange.svg)
![Docker](https://img.shields.io/badge/Docker-Required-blue.svg)
![AI Powered](https://img.shields.io/badge/AI-Powered-purple.svg)

**Transform plain English into production-ready test automation.** Mark 1 is an intelligent test generation platform that converts natural language descriptions into executable Robot Framework code using a sophisticated multi-agent AI system. No coding required—just describe what you want to test.

```
"Open Flipkart and search for shoes and then get the first product name"
                            ↓
        [4 AI Agents Working Together]
                            ↓
    ✅ Working Robot Framework Test
```

## Why Mark 1?

- 🎯 **95%+ Success Rate** - Vision-based element detection that actually works
- ⚡ **3-5x Faster** - Batch processing finds all elements in one session
- 🧠 **Context-Aware** - AI understands your workflow, not just individual steps
- 🔒 **Privacy-First** - Run locally with Ollama or use cloud models
- 📦 **Zero Setup** - One command to start, works out of the box
- 🎨 **Beautiful Reports** - Detailed HTML logs for easy debugging

## Quick Comparison

| Feature | Mark 1 | Selenium IDE | Playwright Codegen | Manual Coding |
|---------|--------|--------------|-------------------|---------------|
| **Input Method** | Natural language | Record actions | Record actions | Write code |
| **Output Format** | Robot Framework | Selenium code | Python/JS/Java | Any framework |
| **Element Detection** | AI (95%+) | Record only | Record only | Manual |
| **Learning Curve** | None | Low | Medium | High |
| **Maintenance** | Simply Rerun | Re-record | Re-record | Manual updates |

## 🚀 Quick Start

### Prerequisites

- ✅ **Python 3.9+** - [Download](https://python.org/downloads/)
- ✅ **Docker Desktop** - [Install](https://docs.docker.com/get-docker/) (must be running!)
- ✅ **Git** - [Install](https://git-scm.com/downloads)
- ✅ **Google Gemini API Key** - [Get Free Key](https://aistudio.google.com/app/apikey)

### Installation

```bash
# 1. Clone the repository
git clone https://github.com/your-repo/mark-1.git
cd mark-1

# 2. Configure your API key
cp src/backend/.env.example src/backend/.env
# Edit src/backend/.env and add your GEMINI_API_KEY

# 3. Start Mark 1
chmod +x run.sh
./run.sh

# 4. Start BrowserUse service (in another terminal)
python tools/browser_use_service.py
```

### Generate Your First Test

1. Open `http://localhost:5000` in your browser
2. Enter a test description:
   ```
   Open Flipkart and search for shoes and then get the first product name
   ```
3. Click **"Generate & Run"**
4. Watch the magic happen! ✨

## 💡 Usage Examples

### Example 1: E-commerce Search
```
"Open Flipkart and search for shoes and then get the first product name"
```

### Example 2: GitHub Navigation
```
"Navigate to GitHub using url https://github.com/monkscode, and then get the name of the Pinned project"
```

### Example 3: Google Search
```
"Go to google.com and search for python tutorials"
```

**Pro Tip:** Be specific about what you want. Mention exact elements like "first product name" or "search button in header".

## 🏗️ How It Works

Mark 1 uses a **multi-agent AI system** to transform your natural language into working tests:

```
Your Query → [AI Processing] → Robot Framework Code → Execution → Results
```

**The Process:**
1. **Intelligent Planning** - Query analyzed and broken into precise steps
2. **Smart Element Detection** - AI finds web elements with 95%+ accuracy
3. **Code Generation** - Transforms steps into production-ready Robot Framework code
4. **Quality Assurance** - Validates code before execution
5. **Isolated Execution** - Runs in clean Docker containers

**What You Get:**
- ✅ Working test code in Robot Framework format
- ✅ Detailed HTML reports with step-by-step execution logs
- ✅ Real-time progress updates
- ✅ Validated locators that work on dynamic websites

## 📁 Project Structure

```
mark-1/
├── src/backend/          # FastAPI backend with AI agents
├── tools/                # Browser automation utilities
├── robot_tests/          # Generated tests & reports (auto-created)
│   └── {run-id}/
│       ├── test.robot    # Your generated test
│       ├── log.html      # Detailed execution log
│       └── report.html   # Test summary
├── docs/                 # Documentation
├── run.sh                # One-command startup
└── README.md             # You are here!
```

## 📚 Documentation

- **[Configuration Guide](docs/CONFIGURATION.md)** - Environment variables and settings
- **[Troubleshooting](docs/TROUBLESHOOTING.md)** - Fix common issues
- **[FAQ](docs/FAQ.md)** - Frequently asked questions
- **[Best Practices](docs/BEST_PRACTICES.md)** - Get the most out of Mark 1
- **[Architecture](docs/ARCHITECTURE.md)** - How Mark 1 works under the hood
- **[Contributing](CONTRIBUTING.md)** - Help improve Mark 1

## 🎬 Example Output

**Your Input:**
```
Open Flipkart and search for shoes and then get the first product name
```

**Generated Code:**
```robot
*** Settings ***
Library    SeleniumLibrary

*** Test Cases ***
Search Shoes On Flipkart
    Open Browser    https://www.flipkart.com    chrome
    Input Text    name=q    shoes
    Press Keys    name=q    RETURN
    ${product_name}=    Get Text    xpath=(//div[@class='_4rR01T'])[1]
    Log    First product name: ${product_name}
    Close Browser
```

**Result:** Working test + detailed HTML report in ~20 seconds.

## 🛠️ Configuration

Create a `.env` file in `src/backend/`:

```env
# AI Provider
MODEL_PROVIDER=online
GEMINI_API_KEY=your-actual-api-key-here
ONLINE_MODEL=gemini-2.5-flash

# Application
APP_PORT=5000

# Browser Automation
BROWSER_USE_SERVICE_URL=http://localhost:4999
BROWSER_USE_TIMEOUT=900
ROBOT_LIBRARY=selenium
```

**Get your free Gemini API key:** [Google AI Studio](https://aistudio.google.com/app/apikey)

For detailed configuration options, see the [Configuration Guide](docs/CONFIGURATION.md).

## 🧪 Testing

Test the API directly:

```bash
chmod +x test.sh
./test.sh
```

Or use curl:

```bash
curl -X POST http://localhost:5000/generate-and-run \
  -H "Content-Type: application/json" \
  -d '{"query": "go to google.com and search for python tutorials"}'
```

## 🐛 Troubleshooting

**Common Issues:**

- **"Docker is not available"** - Make sure Docker Desktop is running
- **"GEMINI_API_KEY not found"** - Check your `.env` file in `src/backend/`
- **"Port 5000 already in use"** - Change `APP_PORT` in your `.env` file
- **Tests fail with "Element not found"** - Try being more specific in your query

For detailed troubleshooting, see the [Troubleshooting Guide](docs/TROUBLESHOOTING.md).

## 🤝 Contributing

We welcome contributions! Whether it's bug fixes, new features, or documentation improvements, your help makes Mark 1 better for everyone.

Please see our [Contributing Guide](CONTRIBUTING.md) for detailed instructions on:
- Setting up your development environment
- Making and testing changes
- Submitting pull requests
- Code guidelines and best practices

By submitting a pull request, you agree to our [Contributor License Agreement](CLA.md).

## 📄 License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

## 🆘 Support

- 📚 **Documentation**: Check the [docs](docs/) folder
- 🐛 **Bug Reports**: [GitHub Issues](https://github.com/monkscode/Natural-Language-to-Robot-Framework/issues)
- 💬 **Discussions**: [GitHub Discussions](https://github.com/monkscode/Natural-Language-to-Robot-Framework/discussions)
- 💡 **Feature Requests**: Open an issue with the `enhancement` label

## ⭐ Show Your Support

If Mark 1 helps streamline your testing workflow:
- ⭐ Star this repository
- 🐛 Report issues and help us improve
- 💡 Suggest features
- 🤝 Contribute code
- 📢 Share your experience

---

**Built with ❤️ for the test automation community**

*Mark 1 is not affiliated with or endorsed by Google, Robot Framework, or any mentioned websites. All trademarks belong to their respective owners.*
