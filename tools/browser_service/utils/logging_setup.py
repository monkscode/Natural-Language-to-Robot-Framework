"""
Logging Setup Utility

This module provides logging configuration with UTF-8 support for Windows compatibility.
It ensures that all log output is properly encoded, preventing Unicode errors when logging
special characters or emojis.

Functions:
    - setup_logging: Configure logging with UTF-8 support and return configured logger
"""

import sys
import os
import io
import logging
from typing import Optional


def setup_logging(
    log_level: int = logging.INFO,
    logger_name: Optional[str] = None
) -> logging.Logger:
    """
    Configure logging with UTF-8 support for Windows compatibility.

    This function sets up UTF-8 encoding for stdout/stderr on Windows systems and
    configures the logging system with a custom handler that properly handles Unicode
    characters. This is critical for preventing encoding errors when logging special
    characters or emojis.

    Args:
        log_level: Logging level (default: logging.INFO)
        logger_name: Name of the logger to return (default: root logger)

    Returns:
        Configured logger instance

    Example:
        >>> logger = setup_logging(logging.DEBUG, __name__)
        >>> logger.info("✅ Service started successfully")

    Note:
        This function should be called BEFORE importing any libraries that use logging
        (e.g., browser-use) to ensure all loggers inherit the UTF-8 configuration.
    """
    # ========================================
    # UNICODE FIX - Force UTF-8 encoding on Windows
    # ========================================
    if sys.platform.startswith('win'):
        # Set environment variables for UTF-8
        os.environ['PYTHONIOENCODING'] = 'utf-8'
        os.environ['PYTHONUTF8'] = '1'

        # Reconfigure stdout/stderr with UTF-8 (only if not already wrapped)
        try:
            if hasattr(sys.stdout, 'buffer') and not isinstance(sys.stdout, io.TextIOWrapper):
                sys.stdout = io.TextIOWrapper(
                    sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
            if hasattr(sys.stderr, 'buffer') and not isinstance(sys.stderr, io.TextIOWrapper):
                sys.stderr = io.TextIOWrapper(
                    sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)
        except (ValueError, AttributeError):
            # If reconfiguration fails, just continue with default encoding
            pass

    # ========================================
    # LOGGING CONFIGURATION
    # ========================================
    # Create a custom handler with UTF-8 encoding
    # CRITICAL: Must explicitly set encoding='utf-8' and errors='replace' for Windows compatibility
    if sys.platform.startswith('win'):
        # For Windows, create a StreamHandler with explicit UTF-8 encoding
        log_handler = logging.StreamHandler(sys.stdout)
        log_handler.stream = io.TextIOWrapper(
            sys.stdout.buffer,
            encoding='utf-8',
            errors='replace',  # Replace unencodable characters instead of crashing
            line_buffering=True
        )
    else:
        # For Unix-like systems, default encoding is usually UTF-8
        log_handler = logging.StreamHandler(sys.stdout)

    log_handler.setFormatter(logging.Formatter(
        "%(levelname)-8s [%(name)s] %(message)s"
    ))

    # Configure root logger with our UTF-8 handler
    # This ensures all child loggers inherit the UTF-8 encoding
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()  # Remove any existing handlers
    root_logger.addHandler(log_handler)

    # Get the requested logger (or root logger if none specified)
    if logger_name:
        logger = logging.getLogger(logger_name)
    else:
        logger = root_logger

    return logger
