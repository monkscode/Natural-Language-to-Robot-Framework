"""
Optimization module for CrewAI-based Robot Framework code generation.

Public API — symbols used by crew.py and workflow_service.py:
- KeywordVectorStore: ChromaDB wrapper for keyword embeddings
- KeywordSearchTool: CrewAI BaseTool for semantic keyword search
- QueryPatternMatcher: Query-to-keyword association from past successes
- SmartKeywordProvider: 4-tier context assembly for agent prompts
- AgentContextResult: Return type of SmartKeywordProvider.get_context()
- ContextPruner: Category-based context classification

Internal symbols (ExecutionMemory, FailureAnalyzer, engines, etc.) are
not re-exported here. Import them directly from their modules if needed:
    from src.backend.crew_ai.optimization.execution_memory import ExecutionMemory
"""

from .chroma_store import KeywordVectorStore
from .keyword_search_tool import KeywordSearchTool
from .pattern_learning import QueryPatternMatcher
from .smart_keyword_provider import SmartKeywordProvider, AgentContextResult
from .context_pruner import ContextPruner

__all__ = [
    "KeywordVectorStore",
    "KeywordSearchTool",
    "QueryPatternMatcher",
    "SmartKeywordProvider",
    "AgentContextResult",
    "ContextPruner",
]
