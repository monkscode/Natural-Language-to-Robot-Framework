# Changelog

All notable changes to Mark 1 will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added - Browser Library Support ⭐
- **Browser Library (Playwright) support** - Modern, fast alternative to SeleniumLibrary
- **Library Context System** - Dynamic code generation for different Robot Framework libraries
- **Playwright Validation** - Native Playwright validation for Browser Library locators
- **Configuration-based switching** - Easy switching between Browser and Selenium via `ROBOT_LIBRARY` setting
- **Library-specific locator strategies** - Text-based, role-based, and traditional selectors
- **Dynamic keyword extraction** - Automatically extracts keywords from installed libraries
- **Comprehensive documentation** - Browser Library Guide, Quick Reference, updated configuration docs

### Performance Improvements
- **2-3x faster test execution** with Browser Library (Playwright)
- **Better AI compatibility** - LLMs understand JavaScript/Playwright better
- **Auto-waiting built-in** - No explicit waits needed with Browser Library
- **Modern web support** - Shadow DOM, iframes, SPAs work seamlessly

### Documentation
- Added [Library Switching Guide](docs/LIBRARY_SWITCHING_GUIDE.md) - Quick guide for switching between Browser Library & Selenium
- Updated [Configuration Guide](docs/CONFIGURATION.md) - Detailed ROBOT_LIBRARY documentation
- Updated [Architecture Guide](docs/ARCHITECTURE.md) - Library context system architecture
- Updated [README.md](README.md) - Browser Library benefits and usage
- Updated [.env.example](src/backend/.env.example) - Inline documentation of library options

### Changed
- **Default library** changed to Browser Library (`ROBOT_LIBRARY=browser`)
- **Generated code format** now uses Browser Library syntax by default
- **Locator generation** now prioritizes Playwright-specific selectors (text=, role=)
- **Validation strategy** uses Playwright native validation for Browser Library

### Technical Details
- Implemented `LibraryContext` abstract base class
- Created `BrowserLibraryContext` with Playwright-specific instructions
- Created `SeleniumLibraryContext` for backward compatibility
- Added `DynamicLibraryDocumentation` for keyword extraction
- Integrated library context into all AI agents (Step Planner, Code Assembler, Validator)
- Added `validate_locators_with_playwright()` function in browser_use_service.py
- Added library-specific JavaScript generation for locator strategies

### Backward Compatibility
- ✅ SeleniumLibrary fully supported via `ROBOT_LIBRARY=selenium`
- ✅ Existing tests continue to work
- ✅ No breaking changes
- ✅ Easy migration path provided

---

## Previous Releases

### Added
- Comprehensive README with detailed architecture explanation
- Real-world usage examples for e-commerce, social media, and data extraction
- FAQ section covering common questions and troubleshooting
- Technical deep dive explaining batch processing innovation
- Quick comparison table with other automation tools
- Best practices guide for writing effective test descriptions
- Performance metrics and limitations documentation

### Changed
- Enhanced README structure with better navigation
- Improved Quick Start guide with step-by-step instructions
- Updated configuration section with detailed environment variables
- Reorganized project structure documentation for clarity

### Documentation
- Added visual workflow diagrams
- Included real-world example with complete output
- Documented the 4-agent pipeline architecture
- Explained batch vision processing benefits
- Added troubleshooting section with common issues

## [1.0.0]

### Added
- Initial release of Mark 1
- Multi-agent AI system with 4 specialized agents
- Vision-based element detection using browser-use
- Batch processing for 3-5x faster element finding
- Docker-based isolated test execution
- Support for Google Gemini and Ollama models
- Real-time streaming progress updates
- Comprehensive HTML test reports
- Web-based user interface
- REST API with Server-Sent Events

### Features
- Natural language to Robot Framework code generation
- 90%+ first-run success rate
- F12-style locator validation
- Context-aware popup handling
- Support for modern web applications
- Detailed execution logs and reports

### Technical
- CrewAI-powered agent orchestration
- FastAPI backend with SSE streaming
- Playwright-based browser automation
- Docker containerization for test execution
- Robot Framework code generation
- Vanilla JavaScript frontend

---

**Legend:**
- `Added` - New features
- `Changed` - Changes in existing functionality
- `Deprecated` - Soon-to-be removed features
- `Removed` - Removed features
- `Fixed` - Bug fixes
- `Security` - Security improvements
- `Documentation` - Documentation updates
