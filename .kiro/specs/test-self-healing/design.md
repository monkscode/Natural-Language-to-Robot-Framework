# Test Self-Healing Feature Design

## Overview

The Test Self-Healing feature extends the existing Mark 1 system with intelligent failure recovery capabilities. When Robot Framework tests fail due to locator issues, the system automatically detects the failure, analyzes the root cause, generates alternative locators using AI agents, validates them against the live application, and updates the test code with working solutions.

The design integrates seamlessly with the existing multi-agent architecture and leverages the current Docker-based execution environment while adding new specialized agents for failure analysis and locator healing.

## Architecture

### High-Level Flow

```
Test Execution → Failure Detection → Failure Analysis → Locator Generation → Validation → Test Update → Re-execution
```

### Integration Points

The self-healing system integrates with existing components:

- **Robot Framework Executor**: Enhanced to capture detailed failure information
- **Agent System**: New healing agents added alongside existing planning/validation agents  
- **Docker Environment**: Extended to support persistent Chrome sessions for validation
- **API Layer**: New endpoints for healing status and reports
- **Frontend**: Enhanced to display healing progress and results

## Components and Interfaces

### 1. Failure Detection Service

**Purpose**: Monitors test execution and identifies healable failures

**Key Methods**:
- `analyze_execution_result(output_xml, logs)` → `FailureAnalysis`
- `is_locator_failure(exception_details)` → `bool`
- `extract_failure_context(logs)` → `FailureContext`

**Failure Classification Logic**:
- Parse Robot Framework output.xml for test status and error messages
- Identify Selenium exceptions: NoSuchElementException, ElementNotInteractableException, TimeoutException
- Extract failing locator, target URL, and test step context
- Filter out non-healable failures (network issues, assertion failures, etc.)

### 2. Element Fingerprinting Service

**Purpose**: Creates and manages element signatures for better matching

**Key Methods**:
- `create_fingerprint(element_info)` → `ElementFingerprint`
- `match_fingerprint(current_dom, fingerprint)` → `MatchResult`
- `store_fingerprint(test_id, step_id, fingerprint)` → `void`

**Fingerprint Components**:
- Element tag name, attributes, text content
- Parent/sibling element context
- Visual properties (if available)
- Relative position in DOM tree

### 3. Healing Agent System

**Purpose**: AI-powered locator generation and validation

#### 3.1 Failure Analysis Agent
- **Role**: Diagnostic specialist for test failures
- **Goal**: Accurately classify failure types and extract healing context
- **Capabilities**: Parse logs, identify root causes, determine healing feasibility

#### 3.2 Locator Generation Agent  
- **Role**: Web element locator specialist (enhanced from existing)
- **Goal**: Generate multiple alternative locators using DOM analysis
- **Capabilities**: Create diverse locator strategies, leverage element fingerprints

#### 3.3 Locator Validation Agent
- **Role**: Live validation specialist
- **Goal**: Test locator effectiveness against running application
- **Capabilities**: Execute locator tests, verify element properties, rank alternatives

### 4. Chrome Session Manager

**Purpose**: Manages persistent browser sessions for efficient validation

**Key Methods**:
- `get_session(url)` → `ChromeSession`
- `validate_locator(session, locator, expected_properties)` → `ValidationResult`
- `cleanup_sessions()` → `void`

**Session Management**:
- Pool of reusable headless Chrome instances
- Session timeout and cleanup policies
- Resource usage monitoring and limits

### 5. Test Code Updater

**Purpose**: Safely modifies Robot Framework test files with healed locators

**Key Methods**:
- `backup_test_file(file_path)` → `backup_path`
- `update_locator(file_path, old_locator, new_locator)` → `UpdateResult`
- `validate_syntax(updated_content)` → `bool`

**Update Strategy**:
- Create timestamped backups before modifications
- Use AST parsing for precise locator replacement
- Validate Robot Framework syntax after changes
- Atomic file operations to prevent corruption

### 6. Healing Orchestrator

**Purpose**: Coordinates the entire healing workflow

**Key Methods**:
- `initiate_healing(failure_context)` → `HealingSession`
- `execute_healing_workflow(session)` → `HealingResult`
- `generate_healing_report(session)` → `HealingReport`

**Workflow Management**:
- Manages healing session state and progress
- Coordinates agent interactions
- Handles retries and fallback strategies
- Generates comprehensive reports

## Data Models

### FailureContext
```python
@dataclass
class FailureContext:
    test_file: str
    test_case: str
    failing_step: str
    original_locator: str
    target_url: str
    exception_type: str
    exception_message: str
    timestamp: datetime
    run_id: str
```

### ElementFingerprint
```python
@dataclass
class ElementFingerprint:
    tag_name: str
    attributes: Dict[str, str]
    text_content: str
    parent_context: List[str]
    sibling_context: List[str]
    dom_path: str
    visual_hash: Optional[str]
```

### HealingResult
```python
@dataclass
class HealingResult:
    success: bool
    original_locator: str
    healed_locator: Optional[str]
    attempts: List[LocatorAttempt]
    execution_time: float
    confidence_score: float
    backup_file_path: Optional[str]
```

## Error Handling

### Healing Failure Scenarios
1. **No Alternative Locators Found**: Log failure, preserve original test
2. **All Alternatives Invalid**: Report comprehensive failure analysis
3. **Chrome Session Timeout**: Retry with fresh session, fallback to basic healing
4. **Test Update Failure**: Restore from backup, log error details
5. **Syntax Validation Failure**: Rollback changes, report parsing issues

### Fallback Strategies
- If AI agents fail, use rule-based locator generation
- If Chrome validation fails, use static DOM analysis
- If healing repeatedly fails for same element, temporarily disable for that locator

### Resource Management
- Limit concurrent Chrome sessions (default: 3)
- Set healing timeout per test (default: 5 minutes)
- Implement circuit breaker for repeated failures

## Testing Strategy

### Unit Testing
- Mock Chrome sessions for locator validation testing
- Test failure detection with various Robot Framework output formats
- Validate element fingerprinting accuracy with DOM samples
- Test agent prompt engineering with known failure scenarios

### Integration Testing
- End-to-end healing workflow with real failing tests
- Chrome session management under load
- Test file backup and restoration procedures
- Agent coordination and error propagation

### Performance Testing
- Healing latency under various failure scenarios
- Chrome session pool efficiency
- Memory usage during extended healing sessions
- Concurrent healing request handling

### Validation Testing
- Accuracy of failure classification
- Quality of generated alternative locators
- Effectiveness of element fingerprinting
- Success rate of healed tests in subsequent runs

## Configuration

### Healing Settings
```yaml
self_healing:
  enabled: true
  max_attempts_per_locator: 3
  chrome_session_timeout: 30s
  healing_timeout: 5m
  max_concurrent_sessions: 3
  backup_retention_days: 7
  
  failure_detection:
    enable_fingerprinting: true
    confidence_threshold: 0.7
    
  locator_generation:
    strategies: ["id", "name", "css", "xpath", "link_text"]
    max_alternatives: 5
    
  validation:
    element_wait_timeout: 10s
    interaction_test: true
```

### Agent Configuration
- Enhanced existing agents with healing capabilities
- New specialized healing agents with focused prompts
- Model selection for healing vs. generation tasks
- Retry policies for agent failures

This design maintains compatibility with your existing architecture while adding robust self-healing capabilities that go beyond simple locator replacement to include intelligent failure analysis and validation.