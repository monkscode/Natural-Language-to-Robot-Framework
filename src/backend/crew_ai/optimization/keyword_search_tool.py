"""
Keyword search tool for CrewAI agents.

Provides semantic search over Robot Framework keywords, allowing agents
to find relevant keywords on-demand without having all keywords in context.
"""

import json
import logging
import time
from typing import Optional
from crewai.tools import BaseTool
from .chroma_store import KeywordVectorStore

logger = logging.getLogger(__name__)


class KeywordSearchTool(BaseTool):
    """
    CrewAI tool for semantic keyword search using ChromaDB.
    
    Agents call this tool when they need to find relevant keywords for an action.
    Returns top K matching keywords with descriptions and examples.
    """
    
    name: str = "keyword_search"
    description: str = """
Search for Robot Framework keywords by describing what you want to do.
Use this when you need to find the right keyword for an action.

Input: Natural language query (e.g., "click a button", "wait for element", "fill text input")
Output: Top 3 matching keywords with descriptions, arguments, and examples

Example usage:
- To find click keywords: "click a button"
- To find input keywords: "type text into field"
- To find wait keywords: "wait for element to be visible"
"""
    
    # Use Pydantic's PrivateAttr for internal state
    _library_name: str
    _vector_store: KeywordVectorStore
    _cache: dict
    _metrics: Optional[object]
    
    def __init__(self, library_name: str, vector_store: KeywordVectorStore, metrics: Optional[object] = None):
        """
        Initialize with library name and ChromaDB vector store.
        
        Args:
            library_name: "Browser" or "SeleniumLibrary"
            vector_store: KeywordVectorStore instance
            metrics: Optional WorkflowMetrics instance for tracking
        """
        super().__init__()
        object.__setattr__(self, '_library_name', library_name)
        object.__setattr__(self, '_vector_store', vector_store)
        object.__setattr__(self, '_cache', {})
        object.__setattr__(self, '_metrics', metrics)
    
    def _run(self, query: str, top_k: int = 3) -> str:
        """
        Search for keywords matching the query.
        
        Args:
            query: Natural language description of what you want to do
            top_k: Number of results to return (default: 3)
            
        Returns:
            JSON string with top matching keywords
        """
        # Start timing for metrics
        start_time = time.time()
        
        # Check cache
        cache_key = f"{query}:{top_k}"
        if cache_key in self._cache:
            logger.debug(f"Cache hit for query: {query}")
            return self._cache[cache_key]
        
        try:
            # Search ChromaDB
            keywords = self._vector_store.search(
                library_name=self._library_name,
                query=query,
                top_k=top_k
            )
            
            if not keywords:
                return json.dumps({
                    "message": "No keywords found for your query. Try rephrasing or use a more general term.",
                    "results": []
                })
            
            # Format results for agent consumption
            results = []
            for kw in keywords:
                # Generate usage example
                example = self._get_example(kw['name'], kw['args'])
                
                results.append({
                    "name": kw['name'],
                    "args": kw['args'],
                    "description": kw['description'][:200] if kw['description'] else "No description available",
                    "example": example,
                    "similarity": round(kw['similarity'], 3)
                })
            
            result_json = json.dumps({
                "query": query,
                "library": self._library_name,
                "results": results
            }, indent=2)
            
            # Cache result (limit cache size to 100 entries)
            if len(self._cache) >= 100:
                # Remove oldest entry (simple FIFO)
                self._cache.pop(next(iter(self._cache)))
            self._cache[cache_key] = result_json
            
            # Track metrics if available
            if self._metrics:
                latency_ms = (time.time() - start_time) * 1000
                returned_keyword_names = [r['name'] for r in results]
                self._metrics.track_keyword_search(latency_ms, returned_keyword_names)
            
            logger.info(f"Keyword search for '{query}' returned {len(results)} results")
            return result_json
            
        except Exception as e:
            logger.error(f"Keyword search failed: {e}")
            return json.dumps({
                "error": "Search failed. Please try again or use keywords you already know.",
                "results": []
            })
    
    def _get_example(self, keyword_name: str, args: list) -> str:
        """
        Generate usage example for keyword with clear argument structure.
        
        Args:
            keyword_name: Name of the keyword
            args: List of arguments
            
        Returns:
            Example usage string with argument explanations
        """
        # Format arguments with explanations
        if args:
            # Handle both string and dict formats
            arg_parts = []
            arg_explanations = []
            for i, arg in enumerate(args):
                if isinstance(arg, dict):
                    arg_name = arg.get('name', '')
                    # Check if 'default' key exists (not just if value is None)
                    arg_required = 'default' not in arg
                else:
                    arg_str_val = str(arg)
                    arg_name = arg_str_val.split('=')[0].split(':')[0].strip()
                    arg_required = '=' not in arg_str_val
                
                if arg_name and arg_name not in ['self', 'cls']:
                    if arg_required:
                        arg_parts.append(f"<{arg_name}>")
                        arg_explanations.append(f"arg{i+1}: {arg_name} (required)")
                    else:
                        arg_parts.append(f"[{arg_name}]")
                        arg_explanations.append(f"arg{i+1}: {arg_name} (optional)")
            
            arg_str = '    '.join(arg_parts) if arg_parts else ""
            explanation_str = ", ".join(arg_explanations) if arg_explanations else ""
        else:
            arg_str = ""
            explanation_str = ""
        
        # Generate example with syntax note
        if arg_str:
            example = f"{keyword_name}    {arg_str}"
            if explanation_str:
                example += f"\n  # Syntax: {explanation_str}"
                example += "\n  # Note: Each argument is SEPARATE (use 4 spaces between args)"
            return example
        else:
            return f"{keyword_name}"
