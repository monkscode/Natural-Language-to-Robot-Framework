# Project Structure

## Overview
This document describes the professional code structure of the Natural Language to Robot Framework project with self-healing capabilities.

## Directory Structure

```
Natural-Language-to-Robot-Framework/
├── .git/                           # Git repository
├── .kiro/                          # Kiro IDE configuration
│   └── specs/                      # Feature specifications
├── config/                         # Configuration files
│   └── self_healing.yaml          # Self-healing configuration
├── data/                          # Data files and samples
├── docs/                          # Documentation
│   ├── examples/                  # Demo scripts and examples
│   ├── healing_api.md             # Healing API documentation
│   └── PROJECT_STRUCTURE.md       # This file
├── logs/                          # Application logs
├── robot_tests/                   # Robot Framework test outputs
├── src/                           # Source code
│   ├── backend/                   # Backend application
│   │   ├── api/                   # API endpoints
│   │   ├── core/                  # Core functionality
│   │   ├── crew_ai/               # CrewAI integration
│   │   └── services/              # Business logic services
│   └── frontend/                  # Frontend application (if applicable)
├── tests/                         # Test suite
│   ├── backend/                   # Backend unit tests
│   ├── integration/               # Integration tests
│   └── utils/                     # Test utilities
├── tools/                         # Development tools and scripts
├── venv/                          # Python virtual environment
├── .gitignore                     # Git ignore rules
├── Dockerfile                     # Docker configuration
├── LICENSE                        # License file
├── README.md                      # Project documentation
├── requirements-dev.txt           # Development dependencies
├── run.sh                         # Application startup script
└── setup_venv.sh                  # Environment setup script
```

## Source Code Structure (`src/backend/`)

### API Layer (`api/`)
- `endpoints.py` - Main API endpoints
- `healing_endpoints.py` - Self-healing specific endpoints
- `monitoring_endpoints.py` - Monitoring and metrics endpoints
- `auth.py` - Authentication and authorization

### Core Layer (`core/`)
- `models/` - Data models and schemas
- `config.py` - Configuration management
- `config_loader.py` - Configuration loading utilities
- `logging_config.py` - Logging configuration
- `metrics.py` - Metrics collection
- `audit_trail.py` - Audit logging
- `alerting.py` - Alert system

### Services Layer (`services/`)
- `workflow_service.py` - Main workflow orchestration
- `docker_service.py` - Docker container management
- `healing_orchestrator.py` - Self-healing coordination
- `failure_detection_service.py` - Test failure analysis
- `chrome_session_manager.py` - Browser session management
- `test_code_updater.py` - Test code modification
- `fingerprinting_service.py` - Element fingerprinting

### CrewAI Integration (`crew_ai/`)
- `crew.py` - CrewAI workflow setup
- `healing_agents.py` - AI agents for healing
- `healing_tasks.py` - AI tasks for healing

## Test Structure (`tests/`)

### Backend Tests (`backend/`)
- Unit tests for all backend services
- Mocked dependencies for isolated testing
- Comprehensive coverage of business logic

### Integration Tests (`integration/`)
- End-to-end workflow testing
- API integration testing
- Self-healing system integration
- Natural language to Robot Framework workflow

### Test Utilities (`utils/`)
- `docker_test_helpers.py` - Docker testing utilities
- Common test fixtures and helpers
- Mock data generators

## Tools and Scripts (`tools/`)

### Development Tools
- `cleanup_docker_containers.py` - Docker container cleanup utility

## Configuration Files

### Application Configuration
- `config/self_healing.yaml` - Self-healing system configuration
- `src/backend/.env` - Environment variables
- `requirements-dev.txt` - Development dependencies

### Development Configuration
- `.gitignore` - Git ignore patterns
- `Dockerfile` - Container configuration
- `setup_venv.sh` - Environment setup
- `run.sh` - Application startup

## Key Design Principles

### 1. Separation of Concerns
- Clear separation between API, business logic, and data layers
- Each service has a single responsibility
- Minimal coupling between components

### 2. Professional Structure
- Consistent naming conventions
- Proper package organization
- Clear module boundaries

### 3. Testability
- Comprehensive test coverage
- Proper test organization
- Isolated unit tests with mocked dependencies

### 4. Maintainability
- Clear documentation
- Consistent code structure
- Proper error handling and logging

### 5. Scalability
- Modular architecture
- Service-oriented design
- Configuration-driven behavior

## Development Workflow

### 1. Environment Setup
```bash
./setup_venv.sh
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements-dev.txt
```

### 2. Running Tests
```bash
# Run all tests
pytest tests/

# Run specific test categories
pytest tests/backend/
pytest tests/integration/

# Run with coverage
pytest tests/ --cov=src/backend/
```

### 3. Running the Application
```bash
./run.sh
```

### 4. Docker Management
```bash
# Clean up Docker containers
python tools/cleanup_docker_containers.py
```

## File Naming Conventions

### Python Files
- `snake_case.py` for all Python files
- `test_*.py` for test files
- `*_service.py` for service classes
- `*_endpoints.py` for API endpoints

### Configuration Files
- `*.yaml` for YAML configuration
- `*.json` for JSON configuration
- `.env` for environment variables

### Documentation
- `*.md` for Markdown documentation
- `UPPERCASE.md` for important project documents

## Import Structure

### Relative Imports
- Use relative imports within the same package
- Use absolute imports for cross-package dependencies

### Example Import Patterns
```python
# Within the same service package
from .failure_detection_service import FailureDetectionService

# Cross-package imports
from src.backend.core.models import FailureContext
from src.backend.services.docker_service import get_docker_client
```

This structure ensures maintainability, testability, and professional organization of the codebase.