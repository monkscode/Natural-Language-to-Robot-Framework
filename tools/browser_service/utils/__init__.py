"""
Utilities Module

Shared utility functions for the browser service including:
- JSON extraction and parsing
- Metrics recording
- Logging configuration
"""

from .json_parser import extract_json_for_element, extract_workflow_json
from .metrics import record_workflow_metrics
from .logging_setup import setup_logging

__all__ = [
    'extract_json_for_element',
    'extract_workflow_json',
    'record_workflow_metrics',
    'setup_logging',
]
