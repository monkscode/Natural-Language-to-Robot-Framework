"""
Logging configuration for the CrewAI optimization system.

This module provides a centralized logging configuration for all optimization
components, ensuring consistent logging behavior across the system.

Logging Levels:
- INFO: Normal operation (predictions used, search calls, successful operations)
- WARNING: Fallback triggered (component failed, using baseline behavior)
- ERROR: Critical failure (optimization disabled entirely, unrecoverable errors)
- DEBUG: Detailed diagnostic information (for development/troubleshooting)
"""

import logging
import sys
from typing import Optional


# Optimization-specific logger name
OPTIMIZATION_LOGGER_NAME = "crew_ai.optimization"


def get_optimization_logger(name: str) -> logging.Logger:
    """
    Get a logger for optimization components.
    
    Args:
        name: Module name (typically __name__)
        
    Returns:
        Configured logger instance
        
    Example:
        >>> logger = get_optimization_logger(__name__)
        >>> logger.info("Pattern learning predicted 8 keywords")
    """
    # Create logger with optimization namespace
    if not name.startswith(OPTIMIZATION_LOGGER_NAME):
        # If called from optimization module, use the module name
        if "optimization" in name:
            logger_name = name
        else:
            logger_name = f"{OPTIMIZATION_LOGGER_NAME}.{name}"
    else:
        logger_name = name
    
    return logging.getLogger(logger_name)


def configure_optimization_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    format_string: Optional[str] = None
) -> None:
    """
    Configure logging for the optimization system.
    
    This function sets up the optimization logger with appropriate handlers
    and formatters. It should be called once during system initialization.
    
    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_file: Optional file path for logging output
        format_string: Optional custom format string
        
    Example:
        >>> configure_optimization_logging(level="INFO", log_file="logs/optimization.log")
    """
    # Get the root optimization logger
    logger = logging.getLogger(OPTIMIZATION_LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper()))
    
    # Remove existing handlers to avoid duplicates
    logger.handlers.clear()
    
    # Default format string
    if format_string is None:
        format_string = (
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
    
    formatter = logging.Formatter(format_string)
    
    # Console handler (always enabled)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, level.upper()))
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # File handler (optional)
    if log_file:
        try:
            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(getattr(logging, level.upper()))
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
            logger.info(f"Optimization logging configured with file output: {log_file}")
        except Exception as e:
            logger.warning(f"Failed to configure file logging: {e}")
    
    # Prevent propagation to root logger to avoid duplicate logs
    logger.propagate = False
    
    logger.info(f"Optimization logging configured at {level} level")


# Standard log messages for common scenarios
class LogMessages:
    """
    Standard log messages for optimization system.
    
    This class provides consistent log messages across all optimization
    components, making it easier to monitor and troubleshoot the system.
    """
    
    # INFO level messages (normal operation)
    PATTERN_LEARNING_PREDICTION = "Pattern learning predicted {count} keywords with confidence {confidence:.3f}"
    PATTERN_LEARNING_LEARNED = "Learned pattern from query: {query}"
    KEYWORD_SEARCH_SUCCESS = "Keyword search for '{query}' returned {count} results in {latency:.1f}ms"
    CONTEXT_PRUNING_SUCCESS = "Context pruning: {original} -> {pruned} keywords ({reduction:.1f}% reduction)"
    CHROMA_INITIALIZED = "ChromaDB initialized at {path}"
    COLLECTION_READY = "Collection '{name}' ready with {count} keywords"
    
    # WARNING level messages (fallback triggered)
    PATTERN_LEARNING_FALLBACK = "Pattern learning failed: {error}, falling back to zero-context"
    KEYWORD_SEARCH_FALLBACK = "Keyword search failed: {error}, providing full context to agent"
    CONTEXT_PRUNING_FALLBACK = "Context pruning failed: {error}, using all predicted keywords"
    EMBEDDING_FALLBACK = "Embedding generation failed: {error}, disabling semantic search"
    FULL_CONTEXT_FALLBACK = "Using full context as fallback - optimization failed"
    NO_PREDICTIONS = "No predictions from pattern learning, using zero-context + tool"
    LOW_CONFIDENCE = "Top similarity {similarity:.3f} below threshold {threshold:.3f}"
    NO_CATEGORIES = "No categories met threshold {threshold:.3f}, max similarity: {max_sim:.3f}"
    
    # ERROR level messages (critical failures)
    CHROMA_INIT_FAILED = "Failed to initialize ChromaDB: {error}"
    COLLECTION_CREATE_FAILED = "Failed to create/get collection '{name}': {error}"
    KEYWORD_INGESTION_FAILED = "Failed to ingest keywords from {library}: {error}"
    PATTERN_LEARNING_ERROR = "Failed to learn from execution: {error}"
    KEYWORD_SEARCH_ERROR = "Keyword search failed: {error}"
    ZERO_CONTEXT_ERROR = "Zero-context formatting failed: {error}, falling back to full context"
    
    # DEBUG level messages (detailed diagnostics)
    CACHE_HIT = "Cache hit for query: {query}"
    EXTRACTED_KEYWORDS = "Extracted {count} keywords from code: {keywords}"
    CATEGORY_SIMILARITY = "Category '{category}' similarity: {similarity:.3f}"
    QUERY_CLASSIFICATION = "Query classified into {count} categories: {categories}"


# Convenience functions for common log patterns
def log_fallback(logger: logging.Logger, component: str, error: Exception, fallback_action: str) -> None:
    """
    Log a fallback event with consistent formatting.
    
    Args:
        logger: Logger instance
        component: Component name (e.g., "Pattern Learning", "Keyword Search")
        error: Exception that triggered the fallback
        fallback_action: Description of fallback action taken
        
    Example:
        >>> log_fallback(logger, "Pattern Learning", e, "using zero-context + tool")
    """
    logger.warning(
        f"{component} failed: {error}, falling back to {fallback_action}",
        exc_info=False  # Don't include stack trace for fallbacks
    )


def log_critical_failure(logger: logging.Logger, component: str, error: Exception) -> None:
    """
    Log a critical failure with full stack trace.
    
    Args:
        logger: Logger instance
        component: Component name
        error: Exception that caused the failure
        
    Example:
        >>> log_critical_failure(logger, "ChromaDB Initialization", e)
    """
    logger.error(
        f"{component} failed critically: {error}",
        exc_info=True  # Include full stack trace for critical errors
    )


def log_performance_metric(logger: logging.Logger, operation: str, duration_ms: float, threshold_ms: float) -> None:
    """
    Log a performance metric with threshold checking.
    
    Args:
        logger: Logger instance
        operation: Operation name
        duration_ms: Duration in milliseconds
        threshold_ms: Performance threshold in milliseconds
        
    Example:
        >>> log_performance_metric(logger, "Keyword Search", 75.5, 100.0)
    """
    if duration_ms > threshold_ms:
        logger.warning(
            f"{operation} took {duration_ms:.1f}ms (threshold: {threshold_ms:.1f}ms)"
        )
    else:
        logger.info(
            f"{operation} completed in {duration_ms:.1f}ms"
        )
