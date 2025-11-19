"""
Optimization module for CrewAI-based Robot Framework code generation.

This module provides:
- ChromaDB vector store for keyword embeddings
- Semantic keyword search tool for agents
- Pattern learning from successful executions
- Smart keyword provider with hybrid architecture
- Centralized logging configuration
"""

from .chroma_store import KeywordVectorStore
from .keyword_search_tool import KeywordSearchTool
from .pattern_learning import QueryPatternMatcher
from .smart_keyword_provider import SmartKeywordProvider
from .context_pruner import ContextPruner
from .logging_config import (
    get_optimization_logger,
    configure_optimization_logging,
    LogMessages,
    log_fallback,
    log_critical_failure,
    log_performance_metric,
)

__all__ = [
    "KeywordVectorStore",
    "KeywordSearchTool",
    "QueryPatternMatcher",
    "SmartKeywordProvider",
    "ContextPruner",
    "get_optimization_logger",
    "configure_optimization_logging",
    "LogMessages",
    "log_fallback",
    "log_critical_failure",
    "log_performance_metric",
]
