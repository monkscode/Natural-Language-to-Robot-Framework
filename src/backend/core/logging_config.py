"""
Logging configuration for the test self-healing system.

This module provides structured logging configuration with different loggers
for various components of the healing system.
"""

import logging
import logging.handlers
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import asdict, is_dataclass


class StructuredFormatter(logging.Formatter):
    """Custom formatter for structured JSON logging."""
    
    def format(self, record: logging.LogRecord) -> str:
        """Format log record as structured JSON."""
        log_data = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno
        }
        
        # Add extra fields if present
        if hasattr(record, 'session_id'):
            log_data['session_id'] = record.session_id
        if hasattr(record, 'test_case'):
            log_data['test_case'] = record.test_case
        if hasattr(record, 'operation'):
            log_data['operation'] = record.operation
        if hasattr(record, 'duration'):
            log_data['duration'] = record.duration
        if hasattr(record, 'success'):
            log_data['success'] = record.success
        if hasattr(record, 'error_code'):
            log_data['error_code'] = record.error_code
        if hasattr(record, 'metadata'):
            log_data['metadata'] = record.metadata
        
        # Add exception info if present
        if record.exc_info:
            log_data['exception'] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]) if record.exc_info[1] else None,
                "traceback": self.formatException(record.exc_info) if record.exc_info else None
            }
        
        return json.dumps(log_data, default=self._json_serializer)
    
    def _json_serializer(self, obj):
        """Custom JSON serializer for complex objects."""
        if is_dataclass(obj):
            return asdict(obj)
        elif hasattr(obj, 'isoformat'):  # datetime objects
            return obj.isoformat()
        elif hasattr(obj, '__dict__'):
            return obj.__dict__
        else:
            return str(obj)


class HealingLoggerAdapter(logging.LoggerAdapter):
    """Logger adapter for healing operations with contextual information."""
    
    def __init__(self, logger: logging.Logger, extra: Dict[str, Any]):
        super().__init__(logger, extra)
    
    def process(self, msg: str, kwargs: Dict[str, Any]) -> tuple:
        """Process log message and add contextual information."""
        # Merge extra context
        if 'extra' not in kwargs:
            kwargs['extra'] = {}
        kwargs['extra'].update(self.extra)
        return msg, kwargs
    
    def log_operation_start(self, operation: str, **metadata):
        """Log the start of a healing operation."""
        self.info(f"Starting {operation}", extra={
            'operation': operation,
            'phase': 'start',
            'metadata': metadata
        })
    
    def log_operation_success(self, operation: str, duration: float, **metadata):
        """Log successful completion of a healing operation."""
        self.info(f"Completed {operation} successfully", extra={
            'operation': operation,
            'phase': 'complete',
            'success': True,
            'duration': duration,
            'metadata': metadata
        })
    
    def log_operation_failure(self, operation: str, duration: float, error: str, error_code: str = None, **metadata):
        """Log failure of a healing operation."""
        self.error(f"Failed {operation}: {error}", extra={
            'operation': operation,
            'phase': 'complete',
            'success': False,
            'duration': duration,
            'error_code': error_code,
            'metadata': metadata
        })
    
    def log_progress(self, operation: str, progress: float, message: str, **metadata):
        """Log progress of a healing operation."""
        self.info(f"{operation} progress: {message}", extra={
            'operation': operation,
            'phase': 'progress',
            'progress': progress,
            'metadata': metadata
        })


def setup_healing_logging(log_level: str = "INFO", log_dir: str = "logs") -> Dict[str, logging.Logger]:
    """
    Set up structured logging for the healing system.
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_dir: Directory to store log files
        
    Returns:
        Dictionary of configured loggers
    """
    import os
    
    # Create log directory
    log_path = Path(log_dir)
    log_path.mkdir(exist_ok=True)
    
    # Get external library log levels from environment
    crewai_level = getattr(logging, os.getenv("CREWAI_LOG_LEVEL", "DEBUG").upper())
    litellm_level = getattr(logging, os.getenv("LITELLM_LOG_LEVEL", "DEBUG").upper())
    langchain_level = getattr(logging, os.getenv("LANGCHAIN_LOG_LEVEL", "INFO").upper())
    openai_level = getattr(logging, os.getenv("OPENAI_LOG_LEVEL", "DEBUG").upper())
    requests_level = getattr(logging, os.getenv("REQUESTS_LOG_LEVEL", "WARNING").upper())
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper()))
    
    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Create formatters
    structured_formatter = StructuredFormatter()
    console_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Console handler for development
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(getattr(logging, log_level.upper()))
    
    # File handler for all logs
    all_logs_handler = logging.handlers.RotatingFileHandler(
        log_path / "healing_all.log",
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5
    )
    all_logs_handler.setFormatter(structured_formatter)
    all_logs_handler.setLevel(logging.DEBUG)
    
    # File handler for healing operations only
    healing_handler = logging.handlers.RotatingFileHandler(
        log_path / "healing_operations.log",
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=10
    )
    healing_handler.setFormatter(structured_formatter)
    healing_handler.setLevel(logging.INFO)
    
    # File handler for errors only
    error_handler = logging.handlers.RotatingFileHandler(
        log_path / "healing_errors.log",
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=10
    )
    error_handler.setFormatter(structured_formatter)
    error_handler.setLevel(logging.ERROR)
    
    # Add handlers to root logger
    root_logger.addHandler(console_handler)
    root_logger.addHandler(all_logs_handler)
    
    # Create specific loggers
    loggers = {}
    
    # Healing orchestrator logger
    orchestrator_logger = logging.getLogger("healing.orchestrator")
    orchestrator_logger.addHandler(healing_handler)
    orchestrator_logger.addHandler(error_handler)
    loggers["orchestrator"] = orchestrator_logger
    
    # Failure detection logger
    failure_logger = logging.getLogger("healing.failure_detection")
    failure_logger.addHandler(healing_handler)
    failure_logger.addHandler(error_handler)
    loggers["failure_detection"] = failure_logger
    
    # Chrome session manager logger
    chrome_logger = logging.getLogger("healing.chrome_manager")
    chrome_logger.addHandler(healing_handler)
    chrome_logger.addHandler(error_handler)
    loggers["chrome_manager"] = chrome_logger
    
    # Agent system logger
    agents_logger = logging.getLogger("healing.agents")
    agents_logger.addHandler(healing_handler)
    agents_logger.addHandler(error_handler)
    loggers["agents"] = agents_logger
    
    # Test code updater logger
    updater_logger = logging.getLogger("healing.code_updater")
    updater_logger.addHandler(healing_handler)
    updater_logger.addHandler(error_handler)
    loggers["code_updater"] = updater_logger
    
    # Metrics logger
    metrics_logger = logging.getLogger("healing.metrics")
    metrics_logger.addHandler(healing_handler)
    loggers["metrics"] = metrics_logger
    
    # Audit logger
    audit_logger = logging.getLogger("healing.audit")
    audit_handler = logging.handlers.RotatingFileHandler(
        log_path / "healing_audit.log",
        maxBytes=20 * 1024 * 1024,  # 20MB
        backupCount=20
    )
    audit_handler.setFormatter(structured_formatter)
    audit_logger.addHandler(audit_handler)
    loggers["audit"] = audit_logger
    
    # CrewAI logger
    crewai_logger = logging.getLogger("crewai")
    crewai_logger.setLevel(crewai_level)
    crewai_handler = logging.handlers.RotatingFileHandler(
        log_path / "crewai.log",
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5
    )
    crewai_handler.setFormatter(structured_formatter)
    crewai_logger.addHandler(crewai_handler)
    crewai_logger.addHandler(console_handler)  # Also show in console
    loggers["crewai"] = crewai_logger
    
    # LiteLLM logger
    litellm_logger = logging.getLogger("litellm")
    litellm_logger.setLevel(litellm_level)
    litellm_handler = logging.handlers.RotatingFileHandler(
        log_path / "litellm.log",
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5
    )
    litellm_handler.setFormatter(structured_formatter)
    litellm_logger.addHandler(litellm_handler)
    litellm_logger.addHandler(console_handler)  # Also show in console
    loggers["litellm"] = litellm_logger
    
    # LangChain logger (used by CrewAI)
    langchain_logger = logging.getLogger("langchain")
    langchain_logger.setLevel(langchain_level)
    langchain_handler = logging.handlers.RotatingFileHandler(
        log_path / "langchain.log",
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5
    )
    langchain_handler.setFormatter(structured_formatter)
    langchain_logger.addHandler(langchain_handler)
    langchain_logger.addHandler(console_handler)  # Also show in console
    loggers["langchain"] = langchain_logger
    
    # OpenAI logger (for API calls)
    openai_logger = logging.getLogger("openai")
    openai_logger.setLevel(openai_level)
    openai_handler = logging.handlers.RotatingFileHandler(
        log_path / "openai.log",
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5
    )
    openai_handler.setFormatter(structured_formatter)
    openai_logger.addHandler(openai_handler)
    openai_logger.addHandler(console_handler)  # Also show in console
    loggers["openai"] = openai_logger
    
    # HTTP requests logger (for API debugging)
    requests_logger = logging.getLogger("requests")
    requests_logger.setLevel(requests_level)
    requests_handler = logging.handlers.RotatingFileHandler(
        log_path / "http_requests.log",
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5
    )
    requests_handler.setFormatter(structured_formatter)
    requests_logger.addHandler(requests_handler)
    loggers["requests"] = requests_logger
    
    # Configure external library logging
    _configure_external_library_logging()
    
    return loggers


def _configure_external_library_logging():
    """Configure logging for external libraries like LiteLLM and CrewAI."""
    import os
    
    # Set LiteLLM logging environment variables
    os.environ["LITELLM_LOG"] = os.getenv("LITELLM_LOG_LEVEL", "DEBUG")
    
    # Try to configure LiteLLM logging if available
    try:
        import litellm
        # Enable LiteLLM logging
        litellm.set_verbose = True
        litellm.log_level = os.getenv("LITELLM_LOG_LEVEL", "DEBUG")
    except ImportError:
        pass
    
    # Configure CrewAI logging if available
    try:
        import crewai
        # CrewAI uses standard Python logging, so our logger configuration should work
    except ImportError:
        pass
    
    # Configure LangChain logging
    try:
        import langchain
        # LangChain uses standard Python logging
        langchain_logger = logging.getLogger("langchain")
        langchain_logger.info("LangChain logging configured")
    except ImportError:
        pass


def get_healing_logger(component: str, session_id: str = None, test_case: str = None) -> HealingLoggerAdapter:
    """
    Get a healing logger adapter with contextual information.
    
    Args:
        component: Component name (orchestrator, failure_detection, etc.)
        session_id: Optional healing session ID
        test_case: Optional test case name
        
    Returns:
        HealingLoggerAdapter instance
    """
    logger = logging.getLogger(f"healing.{component}")
    
    extra = {}
    if session_id:
        extra['session_id'] = session_id
    if test_case:
        extra['test_case'] = test_case
    
    return HealingLoggerAdapter(logger, extra)