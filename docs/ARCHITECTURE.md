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
- SeleniumLibrary support
- Browser Library support
- Extensible for future libraries

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
- **LLM**: Google Gemini or Ollama
- **Browser Automation**: BrowserUse (AI vision)
- **Test Framework**: Robot Framework
- **Containerization**: Docker
- **Libraries**: SeleniumLibrary, Browser Library

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

## Extension Points

Mark 1 is designed to be extensible:

1. **New Robot Framework Libraries**
   - Add library context in `library_context/`
   - Implement LibraryContext interface

2. **New AI Models**
   - Configure in `.env`
   - Supported via LiteLLM

3. **Custom Agents**
   - Add to `agents.py`
   - Integrate in workflow

4. **New Test Types**
   - Extend agent capabilities
   - Add new task definitions

## Learn More

- [Configuration Guide](CONFIGURATION.md)
- [Best Practices](BEST_PRACTICES.md)
- [Contributing Guide](../CONTRIBUTING.md)
