# Configuration Guide

This guide covers all configuration options for Mark 1.

## Environment Variables

Mark 1 is configured using environment variables in the `.env` file located at `src/backend/.env`.

### Quick Setup

```bash
# Copy the example file
cp src/backend/.env.example src/backend/.env

# Edit with your settings
nano src/backend/.env  # or use your preferred editor
```

## Core Settings

### MODEL_PROVIDER

Controls which AI provider to use.

```env
MODEL_PROVIDER=online  # or 'local'
```

**Options:**
- `online` - Use cloud-based models (Google Gemini)
- `local` - Use locally hosted models (Ollama)

**Recommendation:** Use `online` for best results and performance.

### GEMINI_API_KEY

Your Google Gemini API key (required for online mode).

```env
GEMINI_API_KEY=your-actual-api-key-here
```

**How to get:**
1. Visit [Google AI Studio](https://aistudio.google.com/app/apikey)
2. Sign in with Google account
3. Click "Create API Key"
4. Copy and paste into `.env`

**Free Tier:** 1,500 requests per day

### ONLINE_MODEL

Which Gemini model to use.

```env
ONLINE_MODEL=gemini-2.5-flash
```

**Available Models:**
- `gemini-2.5-flash` - Fast, accurate (recommended)
- `gemini-1.5-pro-latest` - More powerful, slower
- `gemini-1.5-flash` - Balanced performance

**Recommendation:** Use `gemini-2.5-flash` for best speed/accuracy balance.

### LOCAL_MODEL

Which Ollama model to use (for local mode).

```env
LOCAL_MODEL=llama3.1
```

**Available Models:**
- `llama3.1` - Good balance
- `llama3` - Faster, less accurate
- `mistral` - Alternative option

**Note:** Model must be pulled first:
```bash
ollama pull llama3.1
```

## Application Settings

### APP_PORT

Port for the FastAPI backend.

```env
APP_PORT=5000
```

**Default:** 5000

**Change if:** Port 5000 is already in use on your system.

### APP_HOST

Host address for the backend.

```env
APP_HOST=0.0.0.0
```

**Default:** 0.0.0.0 (all interfaces)

**Options:**
- `0.0.0.0` - Accessible from network
- `127.0.0.1` - Localhost only

## Browser Automation Settings

### BROWSER_USE_SERVICE_URL

URL for the BrowserUse AI service.

```env
BROWSER_USE_SERVICE_URL=http://localhost:4999
```

**Default:** http://localhost:4999

**Change if:** Running service on different host/port.

### BROWSER_USE_TIMEOUT

Maximum time (seconds) for element detection.

```env
BROWSER_USE_TIMEOUT=900
```

**Default:** 900 seconds (15 minutes)

**Adjust based on:**
- Website complexity
- Network speed
- Test complexity

### ROBOT_LIBRARY

Which Robot Framework library to use for test execution.

```env
ROBOT_LIBRARY=browser  # Recommended
```

**Options:**
- `browser` - Browser Library (Playwright-based) - **Recommended** ⭐
- `selenium` - SeleniumLibrary (legacy support)

## Knowledge Base Settings

### Overview

Mark 1 uses a **RAG (Retrieval-Augmented Generation)** knowledge base system to provide Robot Framework keyword information to agents. This dramatically reduces token usage while maintaining accuracy.

**How it works:**
1. Robot Framework keywords are dynamically extracted from installed libraries
2. Keywords are chunked and embedded using your chosen embedding provider
3. During test generation, agents semantically retrieve only relevant keywords
4. Result: **~85% reduction in token usage** (from 6-8k to 500-1k tokens per query)

### EMBEDDER_PROVIDER

Which embedding provider to use for the knowledge base.

```env
EMBEDDER_PROVIDER=google  # Default if GOOGLE_API_KEY is set
```

**Options:**
- `google` - Google text-embedding-004 (recommended, requires GOOGLE_API_KEY) ⭐
- `ollama` - Local Ollama embeddings (privacy-first, requires Ollama running)
- `sentence_transformers` - Local SentenceTransformers (future support)

**Auto-selection:** If not specified:
1. Uses `google` if `GOOGLE_API_KEY` is available
2. Falls back to `ollama` if running locally

**Google Embeddings (Recommended):**
```env
EMBEDDER_PROVIDER=google
GOOGLE_API_KEY=your-api-key  # Reuses same key as Gemini
```

✅ **Advantages:**
- High quality embeddings
- Works seamlessly with Gemini LLMs
- Reliable API with good rate limits
- No local setup required
- Same API key as your Gemini LLM

❌ **Considerations:**
- Requires internet connection
- API calls for embedding (minimal cost)

**Ollama Embeddings (Privacy-First):**
```env
EMBEDDER_PROVIDER=ollama
```

✅ **Advantages:**
- Fully local (no API calls)
- Zero costs
- Privacy-preserving
- Works offline

❌ **Considerations:**
- Requires Ollama installation
- Need to pull embedding model: `ollama pull mxbai-embed-large`
- Slower than Google embeddings
- Requires ~400MB disk space for model

**Setup for Ollama embeddings:**
```bash
# 1. Install Ollama (if not already)
curl https://ollama.ai/install.sh | sh

# 2. Pull embedding model
ollama pull mxbai-embed-large

# 3. Ensure Ollama is running
ollama serve  # Starts on http://localhost:11434

# 4. Set environment variable
EMBEDDER_PROVIDER=ollama
```

### Knowledge Storage Location

The knowledge base stores embedded keywords in ChromaDB, a local vector database.

**Storage locations by platform:**

**Windows:**
```
C:\Users\{username}\AppData\Local\CrewAI\Natural-Language-to-Robot-Framework\knowledge\
```

**Linux:**
```
~/.local/share/CrewAI/Natural-Language-to-Robot-Framework/knowledge/
```

**macOS:**
```
~/Library/Application Support/CrewAI/Natural-Language-to-Robot-Framework/knowledge/
```

**Contents:**
- `chroma.sqlite3` - ChromaDB metadata database
- `Robot Framework Code Generator (Output ONLY Code)/` - Code Assembler agent's knowledge
- `Robot Framework Linter and Quality Assurance Engineer/` - Code Validator agent's knowledge

### CREWAI_STORAGE_DIR (Advanced)

Customize knowledge base storage location.

```env
CREWAI_STORAGE_DIR=/path/to/custom/storage
```

**Default:** Platform-specific (see above)

**Use cases:**
- Shared storage across team
- Network-attached storage
- Custom backup location
- Docker volume mounts

**Example:**
```env
# Store in project directory for version control backup
CREWAI_STORAGE_DIR=./knowledge_storage

# Store on network drive (team sharing)
CREWAI_STORAGE_DIR=/mnt/shared/mark1_knowledge
```

### Knowledge Base Management

**Viewing storage size:**
```bash
# Windows
dir "%LOCALAPPDATA%\CrewAI\Natural-Language-to-Robot-Framework\knowledge" /s

# Linux/macOS
du -sh ~/.local/share/CrewAI/Natural-Language-to-Robot-Framework/knowledge/
```

**Expected size:** ~5-10 MB (depends on library keyword count)

**Clearing knowledge cache:**

Use this when:
- Robot Framework library version changes
- Switching between libraries (Browser ↔ Selenium)
- Troubleshooting embedding issues

```bash
# Method 1: CrewAI CLI (recommended)
crewai reset-memories --knowledge

# Method 2: Manual deletion
# Windows
rmdir /s "%LOCALAPPDATA%\CrewAI\Natural-Language-to-Robot-Framework\knowledge"

# Linux/macOS
rm -rf ~/.local/share/CrewAI/Natural-Language-to-Robot-Framework/knowledge/
```

**When knowledge is regenerated:**
- First run after clearing cache
- First run after library version update
- After changing `ROBOT_LIBRARY` setting

### Knowledge Retrieval Configuration

These settings control how agents retrieve keywords (advanced tuning).

**Built-in defaults** (optimized, no configuration needed):
```python
# Code Assembler & Validator agents
results_limit = 10        # Return top 10 relevant keyword chunks
score_threshold = 0.35    # Minimum relevance score
chunk_size = 4000        # Optimal embedding size
chunk_overlap = 200      # Context preservation
```

**How retrieval works:**
1. Agent needs keyword information (e.g., "How to click a button?")
2. Query is semantically compared to all keyword chunks
3. Top 10 most relevant chunks are returned (e.g., Click, Click Element, Click Button)
4. Agent uses only relevant keywords instead of all 142+ keywords

**Token savings example:**
```
Before (String Concatenation):
- All 142 SeleniumLibrary keywords injected: ~6,000 tokens
- Every query, every time

After (RAG Knowledge Base):
- Only 10 relevant keywords retrieved: ~800 tokens
- 85% reduction in token usage!
- More focused, better code quality
```

### Knowledge Base Workflow

**First Run (Knowledge Creation):**
```
1. User starts Mark 1
2. Library context loaded (SeleniumLibrary or Browser)
3. Keywords extracted dynamically via libdoc
4. Keywords chunked (4000 chars, 200 overlap)
5. Chunks embedded using configured provider
6. Stored in ChromaDB (platform-specific location)
7. Ready for semantic retrieval!
```

**Subsequent Runs (Knowledge Retrieval):**
```
1. User submits query
2. Code Assembler agent needs keyword info
3. Query semantically compared to stored chunks
4. Top 10 relevant chunks retrieved
5. Agent generates code using only relevant keywords
6. Code Validator validates using relevant rules
7. Fast and token-efficient!
```

### Troubleshooting Knowledge Base

**"Embedding dimension mismatch" error:**
```bash
# You switched embedding providers
# Clear cache and restart:
crewai reset-memories --knowledge
```

**"GOOGLE_API_KEY not found" warning:**
```env
# Add to .env:
GOOGLE_API_KEY=your-key

# Or switch to Ollama:
EMBEDDER_PROVIDER=ollama
```

**"ChromaDB permission denied":**
```bash
# Fix permissions (Linux/macOS)
chmod -R 755 ~/.local/share/CrewAI/

# Windows: Run as administrator or check folder permissions
```

**Knowledge not persisting:**
```env
# Ensure consistent storage location
# Add to .env if using custom location:
CREWAI_STORAGE_DIR=./knowledge_storage
```

**Browser Library (Recommended):**
- ✅ **2-3x faster** execution than Selenium
- ✅ **Better AI compatibility** - LLMs understand JavaScript/Playwright better
- ✅ **Modern web support** - Shadow DOM, iframes, SPAs work seamlessly
- ✅ **Auto-waiting built-in** - No explicit waits needed
- ✅ **Powerful locators** - Text-based (`text=Login`), role-based (`role=button[name="Submit"]`), and traditional selectors
- ✅ **Consistent validation** - Same Playwright engine for locator generation and execution
- ✅ **Better error messages** - More detailed diagnostics

**SeleniumLibrary (Legacy):**
- ✅ **Mature and stable** - Battle-tested over many years
- ✅ **Wide compatibility** - Works with older websites
- ✅ **Familiar syntax** - Traditional Selenium WebDriver approach
- ⚠️ Slower execution
- ⚠️ Manual waits often needed
- ⚠️ Limited modern web support

**When to use Browser Library:**
- New projects
- Modern websites (React, Vue, Angular)
- Performance-critical tests
- Sites with Shadow DOM or complex iframes
- When you want faster test execution

**When to use SeleniumLibrary:**
- Existing projects with Selenium tests
- Legacy websites
- Team has Selenium expertise
- Compatibility with older browsers

**Switching libraries:**
1. Change `ROBOT_LIBRARY` in `.env`
2. Restart Mark 1
3. Generate new tests - they'll use the selected library automatically!

**Example outputs:**

*Browser Library:*
```robot
*** Settings ***
Library    Browser

*** Test Cases ***
Example Test
    New Browser    chromium    headless=False
    New Context    viewport=None
    New Page    https://example.com
    Fill Text    name=q    search term
    Click    text=Search
    Close Browser
```

*SeleniumLibrary:*
```robot
*** Settings ***
Library    SeleniumLibrary

*** Test Cases ***
Example Test
    Open Browser    https://example.com    chrome
    Input Text    name=q    search term
    Click Element    xpath=//button[text()='Search']
    Close Browser
```

## Logging Settings

### LOG_LEVEL

Application logging verbosity.

```env
LOG_LEVEL=INFO
```

**Options:**
- `DEBUG` - Verbose logging (for troubleshooting)
- `INFO` - Standard logging (recommended)
- `WARNING` - Only warnings and errors
- `ERROR` - Only errors

### LOG_DIR

Directory for application logs.

```env
LOG_DIR=logs
```

**Default:** `logs/` in project root

## Docker Settings

### DOCKER_IMAGE

Docker image for test execution.

```env
DOCKER_IMAGE=python:3.9-slim
```

**Default:** python:3.9-slim

**Custom images:** You can build and use custom images with pre-installed dependencies.

## Advanced Settings

### CREWAI_VERBOSE

Enable verbose output from CrewAI agents.

```env
CREWAI_VERBOSE=true
```

**Options:**
- `true` - Show detailed agent workflow
- `false` - Minimal output

### MAX_ITERATIONS

Maximum iterations for agent tasks.

```env
MAX_ITERATIONS=10
```

**Default:** 10

**Increase if:** Complex queries need more processing steps.

### TEMPERATURE

AI model temperature (creativity vs consistency).

```env
TEMPERATURE=0.1
```

**Range:** 0.0 to 1.0
- `0.0` - Deterministic, consistent
- `0.5` - Balanced
- `1.0` - Creative, varied

**Recommendation:** Keep low (0.1-0.3) for test generation.

## Example Configurations

### Production Setup (Cloud) - Recommended

```env
# AI Provider
MODEL_PROVIDER=online
GEMINI_API_KEY=your-production-key
ONLINE_MODEL=gemini-2.5-flash

# Application
APP_PORT=5000
APP_HOST=0.0.0.0

# Browser Automation
BROWSER_USE_SERVICE_URL=http://localhost:4999
BROWSER_USE_TIMEOUT=900
ROBOT_LIBRARY=browser  # Use Browser Library for best performance

# Logging
LOG_LEVEL=INFO
```

### Development Setup (Local)

```env
# AI Provider
MODEL_PROVIDER=local
LOCAL_MODEL=llama3.1

# Application
APP_PORT=5000
APP_HOST=127.0.0.1

# Browser Automation
BROWSER_USE_SERVICE_URL=http://localhost:4999
BROWSER_USE_TIMEOUT=600
ROBOT_LIBRARY=browser  # Use Browser Library for faster development

# Logging
LOG_LEVEL=DEBUG
CREWAI_VERBOSE=true
```

### Privacy-First Setup

```env
# AI Provider (fully local)
MODEL_PROVIDER=local
LOCAL_MODEL=llama3.1

# Application (localhost only)
APP_HOST=127.0.0.1
APP_PORT=5000

# Browser Automation
BROWSER_USE_SERVICE_URL=http://localhost:4999
ROBOT_LIBRARY=browser  # Browser Library works great locally too

# Logging (local only)
LOG_LEVEL=INFO
```

### Legacy/Compatibility Setup

```env
# AI Provider
MODEL_PROVIDER=online
GEMINI_API_KEY=your-key
ONLINE_MODEL=gemini-2.5-flash

# Application
APP_PORT=5000
APP_HOST=0.0.0.0

# Browser Automation
BROWSER_USE_SERVICE_URL=http://localhost:4999
BROWSER_USE_TIMEOUT=900
ROBOT_LIBRARY=selenium  # Use SeleniumLibrary for legacy compatibility

# Logging
LOG_LEVEL=INFO
```

## Configuration Validation

Mark 1 validates configuration on startup. Common validation errors:

### "GEMINI_API_KEY not found"
- Add key to `.env` file
- Ensure no extra spaces

### "Invalid ROBOT_LIBRARY"
- Must be 'selenium' or 'browser' (lowercase)
- Check spelling
- Recommended: Use 'browser' for best performance

### "Invalid MODEL_PROVIDER"
- Must be 'online' or 'local'
- Check spelling

## Environment-Specific Configuration

### Using Multiple Environments

Create separate `.env` files:

```bash
.env.development
.env.production
.env.testing
```

Load the appropriate one:

```bash
# Development
cp .env.development src/backend/.env

# Production
cp .env.production src/backend/.env
```

### CI/CD Configuration

Set environment variables in your CI/CD platform:

**GitHub Actions:**
```yaml
env:
  MODEL_PROVIDER: online
  GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
  ONLINE_MODEL: gemini-2.5-flash
```

**GitLab CI:**
```yaml
variables:
  MODEL_PROVIDER: "online"
  ONLINE_MODEL: "gemini-2.5-flash"
```

## Security Best Practices

1. **Never commit `.env` files** - Already in `.gitignore`
2. **Use secrets management** - For production deployments
3. **Rotate API keys** - Regularly update keys
4. **Limit key permissions** - Use least privilege
5. **Use local models** - For sensitive data

## Troubleshooting Configuration

### Configuration not loading

1. Check file location: `src/backend/.env`
2. Verify file name (not `.env.txt`)
3. Check file permissions
4. Restart the application

### Values not taking effect

1. Restart all services
2. Check for typos in variable names
3. Verify no extra spaces or quotes
4. Check logs for validation errors

## Getting Help

- Configuration issues? Check [Troubleshooting Guide](TROUBLESHOOTING.md)
- Questions? Open a [GitHub Discussion](https://github.com/monkscode/Natural-Language-to-Robot-Framework/discussions)
- Found a bug? Report on [GitHub Issues](https://github.com/monkscode/Natural-Language-to-Robot-Framework/issues)
