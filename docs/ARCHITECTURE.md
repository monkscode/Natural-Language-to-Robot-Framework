# Architecture Overview

Mark 1 uses a sophisticated multi-agent AI system to transform natural language into working Robot Framework tests.

## System Architecture

```
┌─────────────────┐
│  User Query     │
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────┐
│     FastAPI Backend                 │
│  ┌───────────────────────────────┐  │
│  │   Multi-Agent AI System       │  │
│  │  ┌─────────────────────────┐  │  │
│  │  │ 1. Step Planner Agent   │  │  │
│  │  │ 2. Element Finder Agent │  │  │
│  │  │ 3. Code Assembly Agent  │  │  │
│  │  │ 4. Code Validator Agent │  │  │
│  │  └─────────────────────────┘  │  │
│  └───────────────────────────────┘  │
└─────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────┐
│   BrowserUse Service (AI Vision)    │
│   - Element Detection               │
│   - Context Understanding           │
└─────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────┐
│   Docker Container                  │
│   - Robot Framework Execution       │
│   - Isolated Environment            │
└─────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────┐
│   Test Results                      │
│   - .robot file                     │
│   - HTML reports                    │
│   - Execution logs                  │
└─────────────────────────────────────┘
```

## Core Components

### 1. Multi-Agent AI System

Four specialized agents work together:

**Step Planner Agent**
- Analyzes natural language query
- Breaks down into structured test steps
- Only includes explicitly mentioned actions

**Element Finder Agent**
- Coordinates with BrowserUse service
- Finds web elements using AI vision
- Generates stable locators

**Code Assembly Agent**
- Transforms steps into Robot Framework code
- Applies library-specific syntax
- Adds proper error handling

**Code Validator Agent**
- Validates generated code syntax
- Checks for common errors
- Ensures best practices

### 2. BrowserUse Service

AI-powered browser automation:
- Vision-based element detection
- Context-aware navigation
- Batch processing (finds all elements in one session)
- 95%+ accuracy rate

### 3. Library Context System

Dynamic code generation for different Robot Framework libraries:

**Supported Libraries:**
- **Browser Library (Playwright)** - Recommended for modern websites
- **SeleniumLibrary** - Legacy support for compatibility

**Key Features:**
- Dynamic keyword extraction from installed libraries
- Library-specific best practices and templates
- Automatic syntax adaptation
- Extensible architecture for future libraries

**How it works:**
1. Configuration specifies library (`ROBOT_LIBRARY=browser` or `selenium`)
2. Library context loaded at startup
3. AI agents receive library-specific instructions
4. Generated code uses correct keywords and syntax
5. Validation ensures library-specific correctness

**Example - Same query, different libraries:**

*Browser Library output:*
```robot
New Browser    chromium    headless=False
New Context    viewport=None
New Page    https://example.com
Fill Text    name=q    search term
```

*SeleniumLibrary output:*
```robot
Open Browser    https://example.com    chrome
Input Text    name=q    search term
```

### 4. Docker Execution

Isolated test execution:
- Clean environment per test
- No interference between runs
- Consistent, reproducible results

## Data Flow

1. **User submits query** via web interface or API
2. **Step Planner** analyzes and structures the query
3. **Element Finder** uses BrowserUse to locate elements
4. **Code Assembly** generates Robot Framework code
5. **Code Validator** checks and validates code
6. **Docker** executes test in isolated container
7. **Results** returned with logs and reports

## Technology Stack

- **Backend**: FastAPI (Python)
- **AI Framework**: CrewAI (multi-agent orchestration)
- **LLM**: Google Gemini or Ollama (local)
- **Browser Automation**: BrowserUse (AI vision with Playwright)
- **Test Framework**: Robot Framework
- **Containerization**: Docker
- **Test Libraries**: 
  - Browser Library (Playwright) - Recommended
  - SeleniumLibrary - Legacy support
- **Locator Validation**: Playwright (for Browser Library) or JavaScript (for SeleniumLibrary)

## Key Design Decisions

### Why Multi-Agent?

Specialized agents handle specific tasks better than a single monolithic system:
- Better accuracy per task
- Easier to debug and improve
- Modular and maintainable

### Why AI Vision?

Traditional element detection (record-and-playback) fails on dynamic websites. AI vision:
- Understands context and intent
- Adapts to website changes
- Generates stable locators

### Why Docker?

Isolated execution ensures:
- No dependency conflicts
- Clean state per test
- Reproducible results
- Easy CI/CD integration

## Performance Characteristics

- **Test Generation**: 15-30 seconds
- **Element Detection**: 5-15 seconds (batch processing)
- **Code Validation**: 1-2 seconds
- **Test Execution**: Varies by website

## Scalability

Current limitations:
- One test at a time (sequential processing)
- Single browser session per test

Future improvements:
- Parallel test generation
- Distributed execution
- Caching and optimization

## Security Model

- API keys stored locally only
- No data persistence (stateless)
- Docker isolation for execution
- Optional local AI models (Ollama)

## Library Context Architecture

Mark 1 uses a flexible library context system to support multiple Robot Framework libraries:

```
src/backend/crew_ai/library_context/
├── base.py                    # Abstract base class
├── browser_context.py         # Browser Library (Playwright)
├── selenium_context.py        # SeleniumLibrary
├── dynamic_context.py         # Dynamic keyword extraction
└── __init__.py               # Factory function
```

**Adding a new library:**

1. Create new context class:
```python
# my_library_context.py
from .base import LibraryContext

class MyLibraryContext(LibraryContext):
    @property
    def library_name(self) -> str:
        return "MyLibrary"
    
    @property
    def planning_context(self) -> str:
        return "Keywords and best practices..."
    
    # Implement other required methods
```

2. Register in factory:
```python
# __init__.py
def get_library_context(library_type: str):
    if library_type == "mylibrary":
        return MyLibraryContext()
```

3. Update configuration:
```python
# config.py
@validator('ROBOT_LIBRARY')
def validate_robot_library(cls, v):
    if v.lower() not in ['selenium', 'browser', 'mylibrary']:
        raise ValueError(...)
```

## Extension Points

Mark 1 is designed to be extensible:

1. **New Robot Framework Libraries**
   - Add library context in `library_context/`
   - Implement LibraryContext interface
   - Update configuration validator
   - Example: AppiumLibrary for mobile testing

2. **New AI Models**
   - Configure in `.env`
   - Supported via LiteLLM
   - Example: Claude, GPT-4, local models

3. **Custom Agents**
   - Add to `agents.py`
   - Integrate in workflow
   - Example: Performance testing agent

4. **New Test Types**
   - Extend agent capabilities
   - Add new task definitions
   - Example: API testing, mobile testing

## Learn More

- [Configuration Guide](CONFIGURATION.md)
- [Best Practices](BEST_PRACTICES.md)
- [Contributing Guide](../CONTRIBUTING.md)
