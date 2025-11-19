"""
Smart Keyword Provider with Hybrid Architecture

This module orchestrates the 3-tier keyword retrieval system:
1. Core Rules (always included, ~300 tokens)
2. Predicted Keywords (from pattern learning) OR Zero-Context + Tool
3. Full Context Fallback (if both fail)
"""

import logging
from typing import Optional, List, Dict
from .pattern_learning import QueryPatternMatcher
from .chroma_store import KeywordVectorStore
from .keyword_search_tool import KeywordSearchTool
from .context_pruner import ContextPruner

logger = logging.getLogger(__name__)


class SmartKeywordProvider:
    """
    Intelligent keyword provider with hybrid approach:
    - Tier 1: Core Rules (always included)
    - Tier 2: Predicted Keywords OR Zero-Context + Tool
    - Tier 3: Full Context Fallback
    """
    
    def __init__(self,
                 library_context,
                 pattern_matcher: QueryPatternMatcher,
                 vector_store: KeywordVectorStore,
                 context_pruner: Optional['ContextPruner'] = None,
                 pruning_enabled: bool = False,
                 pruning_threshold: float = 0.8,
                 metrics: Optional[object] = None):
        """
        Initialize with library context and optimization components.
        
        Args:
            library_context: LibraryContext instance (e.g., BrowserLibraryContext)
            pattern_matcher: QueryPatternMatcher for pattern learning
            vector_store: KeywordVectorStore for semantic search
            context_pruner: Optional ContextPruner for smart keyword filtering
            pruning_enabled: Whether to enable context pruning
            pruning_threshold: Confidence threshold for category classification (0.0-1.0)
            metrics: Optional WorkflowMetrics instance for tracking
        """
        self.library_context = library_context
        self.pattern_matcher = pattern_matcher
        self.vector_store = vector_store
        self.context_pruner = context_pruner
        self.pruning_enabled = pruning_enabled and context_pruner is not None
        self.pruning_threshold = pruning_threshold
        self.metrics = metrics
        
        logger.info(f"SmartKeywordProvider initialized for {library_context.library_name}")
        if self.pruning_enabled:
            logger.info(f"Context pruning enabled with threshold {pruning_threshold}")
    
    def _get_core_rules(self) -> str:
        """
        Get core library rules that are always included.
        
        Returns:
            Core rules string (~300 tokens)
        """
        return self.library_context.core_rules
    
    def _format_zero_context_with_tool(self, agent_role: str) -> str:
        """
        Format minimal context with keyword search tool instructions.
        
        Used when no predictions are available from pattern learning.
        Target: core rules (300) + tool instructions (200) = 500 tokens
        
        Args:
            agent_role: "planner", "assembler", or "validator"
            
        Returns:
            Formatted context string with core rules + tool usage instructions
        """
        core_rules = self._get_core_rules()
        
        return f"""
You are an expert Robot Framework developer using {self.library_context.library_name}.

{core_rules}

**KEYWORD SEARCH TOOL AVAILABLE:**

You have access to a keyword_search tool to find relevant keywords on-demand.
When you need a keyword, search for it by describing what you want to do.

**How to use the tool:**
- Need to click? Search: "click button element"
- Need to input text? Search: "type text input field"
- Need to wait? Search: "wait element visible"
- Need to get text? Search: "get text from element"

The tool will return the top 3 matching keywords with documentation and examples.
Use the exact keyword names and syntax from the tool results.

**Examples:**
```
Action: keyword_search
Action Input: "click button"

Result: Click, Click Element, Click Button (with docs and examples)
```

Use this tool whenever you need to find the right keyword for an action.
"""
    
    def _format_predicted_context(self, predicted_keywords: List[str], agent_role: str, user_query: str = "") -> str:
        """
        Format context with predicted keywords from pattern learning.
        
        Gets full documentation for predicted keywords from ChromaDB.
        Optionally applies context pruning to filter keywords by category.
        Target: core rules (300) + predicted keywords (500) = 800 tokens
        
        Args:
            predicted_keywords: List of keyword names predicted by pattern learning
            agent_role: "planner", "assembler", or "validator"
            user_query: User's query (used for pruning if enabled)
            
        Returns:
            Formatted context string with core rules + predicted keyword docs
        """
        # Get core rules
        core_rules = self._get_core_rules()
        
        # Apply context pruning if enabled
        keywords_to_fetch = predicted_keywords[:5]  # Limit to top 5 for efficiency
        
        if self.pruning_enabled and user_query:
            try:
                # Classify query into categories
                relevant_categories = self.context_pruner.classify_query(
                    user_query, 
                    confidence_threshold=self.pruning_threshold
                )
                
                # Create keyword dicts for pruning
                keyword_dicts = [{'name': kw} for kw in keywords_to_fetch]
                
                # Prune keywords to relevant categories
                pruned_keyword_dicts = self.context_pruner.prune_keywords(
                    keyword_dicts, 
                    relevant_categories
                )
                
                # Extract pruned keyword names
                keywords_to_fetch = [kw['name'] for kw in pruned_keyword_dicts]
                
                # Log pruning stats
                stats = self.context_pruner.get_pruning_stats(
                    len(keyword_dicts), 
                    len(pruned_keyword_dicts)
                )
                logger.info(
                    f"Context pruning: {stats['original_count']} -> {stats['pruned_count']} keywords "
                    f"({stats['reduction_percentage']:.1f}% reduction)"
                )
            except Exception as e:
                logger.warning(f"Context pruning failed: {e}, using all predicted keywords")
        
        # Get full documentation for keywords from ChromaDB
        logger.info(f"Fetching documentation for {len(keywords_to_fetch)} keywords")
        keyword_docs = []
        for keyword_name in keywords_to_fetch:
            # Search for exact keyword in ChromaDB
            results = self.vector_store.search(
                library_name=self.library_context.library_name,
                query=keyword_name,
                top_k=1
            )
            
            if results and results[0]['name'] == keyword_name:
                kw = results[0]
                # Format keyword documentation - MINIMAL format to reduce tokens
                # Only include essential info: name and first 2 args
                args_list = kw['args'][:2] if kw['args'] else []
                args_str = ', '.join([str(arg) for arg in args_list])
                if len(kw['args']) > 2:
                    args_str += ', ...'
                
                # Very short description (50 chars max)
                doc_str = kw['description'][:50] if kw['description'] else ''
                
                # Compact format: one line per keyword
                keyword_docs.append(f"• {kw['name']}({args_str}): {doc_str}")
        
        logger.info(f"Formatted {len(keyword_docs)} keyword docs in compact format")
        
        # Combine core rules + predicted keywords
        predicted_docs = '\n'.join(keyword_docs) if keyword_docs else 'No predicted keywords available'
        
        return f"""
You are an expert Robot Framework developer using {self.library_context.library_name}.

{core_rules}

**RELEVANT KEYWORDS (from similar queries):**
{predicted_docs}

Use keyword_search tool if you need additional keywords.
"""
    
    def get_agent_context(self, user_query: str, agent_role: str) -> str:
        """
        Get optimized context for an agent based on query and role.
        
        Implements 3-tier retrieval:
        1. Core Rules (always)
        2. Predicted Keywords OR Zero-Context + Tool
        3. Full Context Fallback
        
        Args:
            user_query: User's natural language query
            agent_role: "planner", "assembler", or "validator"
            
        Returns:
            Optimized context string with minimal, relevant keywords
        """
        # Tier 1: Always include core rules
        core_rules = self._get_core_rules()
        
        logger.info(f"Building context for {agent_role} agent")
        logger.debug(f"Core rules: {len(core_rules)} chars")
        
        # Tier 2: Try pattern learning for keyword prediction
        try:
            predicted_keywords = self.pattern_matcher.get_relevant_keywords(user_query)
            
            if predicted_keywords:
                logger.info(f"Pattern learning predicted {len(predicted_keywords)} keywords")
                
                # Track pattern learning metrics
                if self.metrics:
                    self.metrics.track_pattern_learning(
                        predicted=True,
                        keyword_count=len(predicted_keywords),
                        accuracy=0.0  # Accuracy will be calculated after execution
                    )
                
                try:
                    return self._format_predicted_context(predicted_keywords, agent_role, user_query)
                except Exception as e:
                    logger.warning(f"Failed to format predicted context: {e}, falling back to zero-context")
            else:
                logger.info("No predictions from pattern learning, using zero-context + tool")
                
                # Track that no prediction was used
                if self.metrics:
                    self.metrics.track_pattern_learning(
                        predicted=False,
                        keyword_count=0,
                        accuracy=0.0
                    )
        except Exception as e:
            logger.warning(f"Pattern learning failed: {e}, falling back to zero-context")
            
            # Track that prediction failed
            if self.metrics:
                self.metrics.track_pattern_learning(
                    predicted=False,
                    keyword_count=0,
                    accuracy=0.0
                )
        
        # Tier 2 Fallback: Zero-context + tool instructions
        try:
            return self._format_zero_context_with_tool(agent_role)
        except Exception as e:
            logger.error(f"Zero-context formatting failed: {e}, falling back to full context")
        
        # Tier 3: Full context fallback (baseline behavior)
        logger.warning("Using full context as fallback - optimization failed")
        return self._get_full_context_fallback(agent_role)
    
    def _get_full_context_fallback(self, agent_role: str) -> str:
        """
        Get full context as fallback when optimization fails.
        
        This ensures graceful degradation to baseline behavior.
        
        Args:
            agent_role: "planner", "assembler", or "validator"
            
        Returns:
            Full context string from library_context
        """
        logger.info(f"Fallback to full context for {agent_role} agent")
        
        if agent_role == "planner":
            return self.library_context.planning_context
        elif agent_role == "identifier":
            # Element identifier doesn't need keyword context, just minimal guidance
            return "Expert web element locator. Use batch_browser_automation tool to find all elements in one call."
        elif agent_role == "assembler":
            return self.library_context.code_assembly_context
        elif agent_role == "validator":
            return self.library_context.validation_context
        else:
            # Default to code assembly context
            logger.warning(f"Unknown agent role '{agent_role}', using code_assembly_context")
            return self.library_context.code_assembly_context
    
    def get_keyword_search_tool(self) -> KeywordSearchTool:
        """
        Get keyword search tool for agents.
        
        Returns:
            KeywordSearchTool instance configured for this library
        """
        return KeywordSearchTool(
            library_name=self.library_context.library_name,
            vector_store=self.vector_store,
            metrics=self.metrics
        )
    
    def learn_from_execution(self, user_query: str, generated_code: str):
        """
        Learn from successful execution.
        
        Args:
            user_query: Original user query
            generated_code: Successfully generated Robot Framework code
        """
        try:
            self.pattern_matcher.learn_from_execution(user_query, generated_code)
            logger.info(f"Learned pattern from query: {user_query[:50]}...")
        except Exception as e:
            logger.error(f"Failed to learn from execution: {e}")
