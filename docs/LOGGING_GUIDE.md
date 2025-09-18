# Logging Guide - CrewAI & LiteLLM

## Overview
This guide explains how to enable and monitor logging for CrewAI and LiteLLM in the Natural Language to Robot Framework project.

## Problem Solved
You were not seeing CrewAI and LiteLLM logs because the logging configuration didn't include specific loggers for these external libraries.

## Solution Implemented

### 1. Enhanced Logging Configuration
Added dedicated loggers for:
- **CrewAI** - AI agent workflows and task execution
- **LiteLLM** - LLM API calls and responses
- **LangChain** - Language model operations (used by CrewAI)
- **OpenAI** - OpenAI API interactions
- **HTTP Requests** - Network requests for debugging

### 2. Environment Variables Added
In `src/backend/.env`:
```bash
# --- Logging Configuration ---
# Set logging levels for external libraries (DEBUG, INFO, WARNING, ERROR)
CREWAI_LOG_LEVEL=DEBUG
LITELLM_LOG_LEVEL=DEBUG
LANGCHAIN_LOG_LEVEL=INFO
OPENAI_LOG_LEVEL=DEBUG
REQUESTS_LOG_LEVEL=WARNING

# Enable detailed LiteLLM logging
LITELLM_LOG=DEBUG
```

### 3. New Log Files Created
The system now creates these log files in the `logs/` directory:
- `crewai.log` - CrewAI agent activities and workflows
- `litellm.log` - LiteLLM API calls and responses
- `langchain.log` - LangChain operations
- `openai.log` - OpenAI API interactions
- `http_requests.log` - HTTP request/response details

## How to Monitor Logs

### Option 1: Real-time Monitoring (Windows)
```bash
# Monitor specific log
tools\monitor_ai_logs.bat crewai
tools\monitor_ai_logs.bat litellm

# Show available logs
tools\monitor_ai_logs.bat
```

### Option 2: Real-time Monitoring (Python)
```bash
# Monitor multiple logs simultaneously
python tools/monitor_ai_logs.py

# Monitor specific logs
python tools/monitor_ai_logs.py --monitor crewai litellm

# Show log status
python tools/monitor_ai_logs.py --status

# Show recent entries
python tools/monitor_ai_logs.py --recent crewai --lines 50
```

### Option 3: Manual Log Viewing
```bash
# View recent entries
tail -f logs/crewai.log
tail -f logs/litellm.log

# On Windows with PowerShell
Get-Content logs\crewai.log -Wait -Tail 10
Get-Content logs\litellm.log -Wait -Tail 10
```

## Log Levels Explained

### DEBUG Level
- Shows detailed internal operations
- API request/response details
- Step-by-step execution flow
- Best for troubleshooting

### INFO Level
- Shows major operations and results
- High-level workflow progress
- Success/failure notifications
- Good for monitoring

### WARNING Level
- Shows potential issues
- Recoverable errors
- Performance warnings
- Minimal noise

### ERROR Level
- Shows only critical errors
- Failed operations
- System failures
- Cleanest output

## What You'll See in Each Log

### CrewAI Logs (`crewai.log`)
```json
{
  "timestamp": "2025-01-01T12:00:00.000000",
  "level": "INFO",
  "logger": "crewai",
  "message": "Starting agent workflow",
  "agent": "failure_analysis_agent",
  "task": "analyze_failure"
}
```

### LiteLLM Logs (`litellm.log`)
```json
{
  "timestamp": "2025-01-01T12:00:01.000000",
  "level": "DEBUG",
  "logger": "litellm",
  "message": "Making API call to gemini",
  "model": "gemini-2.5-flash",
  "tokens": 150,
  "response_time": 1.2
}
```

### LangChain Logs (`langchain.log`)
```json
{
  "timestamp": "2025-01-01T12:00:02.000000",
  "level": "INFO",
  "logger": "langchain",
  "message": "LLM chain execution completed",
  "chain_type": "sequential",
  "execution_time": 2.5
}
```

## Troubleshooting

### No Log Files Created
**Problem**: Log files don't exist after starting the application.

**Solution**:
1. Check that the application is running
2. Verify the `logs/` directory exists
3. Ensure environment variables are set correctly
4. Check file permissions

### Empty Log Files
**Problem**: Log files exist but are empty.

**Solution**:
1. Verify log levels are set to DEBUG
2. Check that CrewAI/LiteLLM operations are actually running
3. Ensure the logging configuration is loaded

### Too Much Logging
**Problem**: Log files are too verbose.

**Solution**:
1. Change log levels to INFO or WARNING
2. Update environment variables in `.env`
3. Restart the application

### Missing Logs for Specific Operations
**Problem**: Some operations don't appear in logs.

**Solution**:
1. Set log level to DEBUG for more detail
2. Check if the operation is using a different logger
3. Look in `healing_all.log` for general application logs

## Configuration Examples

### Minimal Logging (Production)
```bash
CREWAI_LOG_LEVEL=WARNING
LITELLM_LOG_LEVEL=ERROR
LANGCHAIN_LOG_LEVEL=ERROR
OPENAI_LOG_LEVEL=ERROR
REQUESTS_LOG_LEVEL=ERROR
```

### Detailed Debugging (Development)
```bash
CREWAI_LOG_LEVEL=DEBUG
LITELLM_LOG_LEVEL=DEBUG
LANGCHAIN_LOG_LEVEL=DEBUG
OPENAI_LOG_LEVEL=DEBUG
REQUESTS_LOG_LEVEL=DEBUG
```

### Balanced Monitoring (Default)
```bash
CREWAI_LOG_LEVEL=DEBUG
LITELLM_LOG_LEVEL=DEBUG
LANGCHAIN_LOG_LEVEL=INFO
OPENAI_LOG_LEVEL=DEBUG
REQUESTS_LOG_LEVEL=WARNING
```

## Integration with Existing Logs

The new AI logs work alongside existing healing system logs:
- `healing_all.log` - All healing system operations
- `healing_operations.log` - Healing workflow operations
- `healing_errors.log` - Healing system errors
- `healing_audit.log` - Audit trail

## Performance Considerations

### Log File Rotation
- All log files use rotating file handlers
- Maximum size: 10MB per file
- Backup count: 5 files
- Old files are automatically compressed

### Performance Impact
- DEBUG level logging may slow down operations slightly
- INFO level has minimal performance impact
- WARNING/ERROR levels have negligible impact

## Next Steps

1. **Start the application** with the new logging configuration
2. **Monitor logs** using the provided tools
3. **Adjust log levels** based on your needs
4. **Check specific logs** when debugging issues

The enhanced logging will now show you exactly what CrewAI and LiteLLM are doing, making it much easier to debug AI-related issues!