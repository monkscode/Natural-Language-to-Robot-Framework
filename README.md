# 🤖 Mark 1 - Natural Language to Robot Framework

![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)
![Python](https://img.shields.io/badge/Python-3.9%2B-green.svg)
![Robot Framework](https://img.shields.io/badge/Robot%20Framework-6.0%2B-orange.svg)
![Docker](https://img.shields.io/badge/Docker-Required-blue.svg)
![AI Powered](https://img.shields.io/badge/AI-Powered-purple.svg)

**Transform plain English into production-ready test automation.** Mark 1 is your one-stop solution for writing automation tests without coding. Just describe what you want to test in plain English, and watch it generate working Robot Framework tests automatically. Write once, run repeatedly — and when your application changes, regenerate the test from the same plain-English description.

```
"Open Flipkart and search for shoes and then get the first product name"
                            ↓
   [AI planning + real-browser element detection + code generation,
        gated by a deterministic Robot Framework validation]
                            ↓
        ✅ Working, validated Robot Framework Test
```

## 🚀 What Can Mark 1 Do For You?

### ✅ Your One-Stop Solution for Automation Testing
- **No Coding Required** - Write tests in plain English, not Python or JavaScript
- **Works on Any Website** - E-commerce, SaaS, web apps—anything with a UI
- **Generates Professional Code** - Beautiful Robot Framework tests that even manual QAs can read
- **Fast Test Creation** - 20-30 seconds from idea to working test

### 📝 Write Once, Execute Infinitely
- **Reusable Tests** - Generate test code once, run it 1000 times
- **Environment Agnostic** - Same test works on dev, staging, and production
- **No Re-recording Needed** - Unlike traditional record-and-playback tools, when the UI changes you regenerate from the same plain-English description instead of re-recording
- **Cost Efficient** - Setup overhead paid once, then unlimited test runs

### 🧠 Gets Smarter Over Time
- **Learns Your Architecture** - Remembers common navigation patterns and workflows
- **Contextual Understanding** - AI understands your product's structure and layout
- **Fewer Tokens Over Time** - As it learns your system, it uses fewer AI tokens per test
- **Better Outputs** - More specific, stable, and efficient tests with each run

### 👥 Perfect for Manual QA Teams
- **Easy to Read** - Robot Framework syntax is plain English-like, no technical skills needed
- **Self-Documenting** - Test code IS the documentation
- **Low Learning Curve** - Manual QAs can understand and maintain tests immediately
- **Empowerment Without Complexity** - Keep your QA team without forcing them to become developers

---

## 📈 Quick Comparison

| Feature | Mark 1 | Selenium IDE | Playwright Codegen | Manual Coding |
|---------|--------|--------------|-------------------|---------------|
| **Input Method** | Natural language | Record actions | Record actions | Write code |
| **Output Format** | Robot Framework | Selenium code | Python/JS/Java | Any framework |
| **Element Detection** | AI (95%+) | Record only | Record only | Manual |
| **Learning Curve** | None | Low | Medium | High |
| **Maintenance** | Simply Rerun | Re-record | Re-record | Manual updates |

---

## 🎯 Why Choose Mark 1? (The Bottom Line)

| Your Situation | Mark 1 Solution | Time Saved |
|---|---|---|
| **You have manual QA team** | No coding needed, tests are readable English | ✅ 40-60% faster test creation |
| **UI changes frequently** | Tests auto-adapt via AI | ✅ No test maintenance time |
| **Need tests for new features** | Write tests before code exists | ✅ Test-driven development ready |
| **Legacy testing tools too slow** | Batch element detection | ✅ 3-5x faster than Selenium IDE |
| **Testing is expensive** | Reuse tests indefinitely | ✅ Lower total cost of ownership |
| **Hard to scale QA** | One engineer → 1000 tests | ✅ Enable non-technical QAs |

---

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
   Navigate to GitHub using url https://github.com/monkscode, and then get the name of the Pinned project
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
**Pro Tip:** Be specific about what you want. Mention exact elements like "first product name" or "search button in header".

### Example 3: Sites With a One-Time Popup After Login
```
"Go to https://yourapp.example.com, type admin in the username field, type admin in the password field, click the Sign In button, wait 5 seconds for the dashboard to load, go to the reports page, and click the Filter button"
```
**Why the wait step?** Some sites show an announcement or welcome popup exactly once per login, on the first page that finishes rendering. A short wait right after login lets that popup appear and expire on the dashboard — before your real steps run — instead of blocking a click later in the test. Two rules: put the wait immediately after login, and make sure a navigation to another page follows it. Persistent popups (cookie banners, consent dialogs) don't need this trick — just mention them as a step ("accept the cookie banner") and they are automated like any other click.

**In Technical Terms:**

Mark 1 uses **AI agents combined with deterministic validation** to transform your natural language into working tests:

```
Your Query → [AI Processing] → Robot Framework Code → Execution → Results
```

**The Process:**
1. **Intelligent Planning** - Query analyzed and broken into precise steps
2. **Smart Element Detection** - AI finds web elements with 95%+ accuracy (using computer vision)
3. **Code Generation** - Transforms steps into production-ready Robot Framework code
4. **Quality Assurance** - Validates code before execution
5. **Isolated Execution** - Runs in clean Docker containers

**What You Get:**
- ✅ Working test code in Robot Framework format
- ✅ Detailed HTML reports with step-by-step execution logs
- ✅ Real-time progress updates
- ✅ Validated locators that work on dynamic websites

**Want deeper details?** See the [Architecture Documentation](docs/ARCHITECTURE.md) for the full technical breakdown.

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
Navigate to GitHub using url https://github.com/monkscode, and then get the name of the Pinned project
```

**Generated Code (Browser Library):**
```robot
*** Settings ***
Library    Browser
Library    BuiltIn

*** Variables ***
${browser}    chromium
${headless}    True
${url}    https://github.com/monkscode
${pinned_project_name_locator}    id=892238219

*** Test Cases ***
Generated Test
    [Documentation]    Auto-generated test case
    New Browser    ${browser}    headless=${headless}
    New Context    viewport={'width': 1920, 'height': 1080}
    New Page    ${url}
    ${pinned_project_name}=    Get Text    ${pinned_project_name_locator}
    Log    Retrieved Pinned project name: ${pinned_project_name}
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

# Robot Framework Library (only 'browser' is supported)
ROBOT_LIBRARY=browser
```

**Get your free Gemini API key:** [Google AI Studio](https://aistudio.google.com/app/apikey)

For detailed configuration options, see the [Configuration Guide](docs/CONFIGURATION.md).

### 🎯 Robot Framework Library Support

Mark 1 generates **Browser Library (Playwright)** tests:

```env
ROBOT_LIBRARY=browser
```

**Why Browser Library:**
- ✅ **2-3x faster** test execution
- ✅ **Better AI compatibility** - LLMs understand JavaScript/Playwright better
- ✅ **Modern web support** - Shadow DOM, iframes, SPAs work seamlessly
- ✅ **Auto-waiting built-in** - No explicit waits needed
- ✅ **Powerful locators** - Text-based, role-based, and traditional selectors
- ✅ **Consistent validation** - Same engine (Playwright) for generation and execution

**SeleniumLibrary is not supported** — the locator pipeline emits Playwright-only
syntax, so `ROBOT_LIBRARY=selenium` fails fast at startup.

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
