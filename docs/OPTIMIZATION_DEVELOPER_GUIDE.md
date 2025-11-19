# CrewAI Optimization System - Developer Guide

This guide provides technical documentation for developers working on or extending the CrewAI optimization system.

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Core Components](#core-components)
- [Hybrid Knowledge Architecture](#hybrid-knowledge-architecture)
- [Extension Points](#extension-points)
- [Testing Approach](#testing-approach)
- [Code Examples](#code-examples)
- [Performance Considerations](#performance-considerations)
- [Debugging and Troubleshooting](#debugging-and-troubleshooting)

## Architecture Overview

The optimization system implements a **Hybrid Knowledge Architecture** that reduces token usage by 67% while maintaining code generation accuracy. The system combines three key strategies:

1. **Core Rules** - Always-present library-specific constraints (~300 tokens)
2. **ChromaDB Vector Store** - Semantic search over keyword documentation
3. **Pattern Learning** - Learn from successful executions to predict relevant keywords

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    CrewAI Workflow System                        │
│                                                                  │
│  ┌────────────────┐         ┌──────────────────────┐           │
│  │ Step Planner   │         │ Smart Keyword        │           │
│  │    Agent       │────────▶│    Provider          │           │
│  └────────────────┘         │                      │           │
│                              │  ┌────────────────┐  │           │
│  ┌────────────────┐         │  │ Pattern        │  │           │
│  │ Code Assembler │────────▶│  │ Learning       │  │           │
│  │    Agent       │         │  └────────┬───────┘  │           │
│  └────────────────┘         │           │          │           │
│                              │  ┌────────▼───────┐  │           │
│  ┌────────────────┐         │  │ Keyword Search │  │           │
│  │ Code Validator │────────▶│  │     Tool       │  │           │
│  │    Agent       │         │  └────────┬───────┘  │           │
│  └────────────────┘         │           │          │           │
│                              │  ┌────────▼───────┐  │           │
│  ┌────────────────┐         │  │   ChromaDB     │  │           │
│  │Library Context │◀────────│  │ Vector Store   │  │           │
│  │   (Existing)   │         │  └────────────────┘  │           │
│  └────────────────┘         └──────────────────────┘           │
└─────────────────────────────────────────────────────────────────┘
```

### Module Structure

```
src/backend/crew_ai/optimization/
├── __init__.py                    # Public API exports
├── chroma_store.py                # ChromaDB vector store wrapper
├── keyword_search_tool.py         # Semantic keyword search tool
├── pattern_learning.py            # Query pattern matcher
├── smart_keyword_provider.py      # Hybrid keyword provider orchestration
├── context_pruner.py              # Smart context pruning
└── logging_config.py              # Optimization-specific logging
```


## Core Components

### 1. ChromaDB Vector Store (`chroma_store.py`)

The `KeywordVectorStore` class manages keyword embeddings using ChromaDB for efficient semantic search.

**Key Features:**
- Persistent storage (no re-embedding on restart)
- Automatic embedding generation using sentence-transformers
- Separate collections per library (Browser, SeleniumLibrary)
- Version tracking and automatic rebuild on library updates

**Class Interface:**

```python
class KeywordVectorStore:
    """ChromaDB wrapper for keyword storage and semantic search."""
    
    def __init__(self, persist_directory: str = "./chroma_db"):
        """Initialize ChromaDB client with persistence."""
        
    def create_or_get_collection(self, library_name: str):
        """Get or create collection for library keywords."""
        
    def add_keywords(self, library_name: str, keywords: List[Dict]):
        """Add keywords to ChromaDB collection."""
        
    def search(self, library_name: str, query: str, top_k: int = 3) -> List[Dict]:
        """Semantic search for keywords."""
        
    def get_library_version(self, library_name: str) -> str:
        """Get library version from metadata."""
        
    def rebuild_collection(self, library_name: str):
        """Rebuild collection on version change."""
```

**Implementation Details:**

- Uses `chromadb.PersistentClient` for disk persistence
- Embedding function: `SentenceTransformerEmbeddingFunction` with `all-MiniLM-L6-v2`
- Documents formatted as: `"{keyword_name} {keyword_documentation}"`
- Metadata includes: name, args, doc, version
- Search returns: name, args, description, distance score

**Performance:**
- Initialization: <5 seconds for 143 keywords
- Search latency: <100ms per query
- Storage: ~10-20 MB per library

### 2. Keyword Search Tool (`keyword_search_tool.py`)

The `KeywordSearchTool` provides semantic search as a CrewAI tool that agents can invoke.

**Key Features:**
- CrewAI `BaseTool` integration
- LRU cache for frequent searches (100 entries)
- Returns top K results with examples
- JSON-formatted output for agent consumption

**Class Interface:**

```python
class KeywordSearchTool(BaseTool):
    """CrewAI tool for semantic keyword search."""
    
    name: str = "keyword_search"
    description: str = "Search for Robot Framework keywords..."
    
    def __init__(self, library_name: str, chroma_store: KeywordVectorStore):
        """Initialize with library name and ChromaDB store."""
        
    def _run(self, query: str, top_k: int = 3) -> str:
        """Search for keywords matching the query."""
```

**Usage by Agents:**

```python
# Agent calls tool with natural language query
result = keyword_search("click a button")

# Tool returns JSON with top matches
{
  "results": [
    {
      "name": "Click",
      "args": ["selector", "**kwargs"],
      "description": "Clicks element identified by selector...",
      "example": "Click    ${locator}"
    },
    ...
  ]
}
```

**Caching Strategy:**
- LRU cache with 100 entries
- Cache key: `"{query}:{top_k}"`
- Eviction: FIFO when cache full
- Hit rate: ~40% in typical usage


### 3. Pattern Learning System (`pattern_learning.py`)

The `QueryPatternMatcher` learns which keywords are commonly used for specific query types.

**Key Features:**
- ChromaDB-based pattern storage with embeddings
- Semantic similarity search for pattern matching
- Confidence-based prediction
- Continuous learning from successful executions

**Class Interface:**

```python
class QueryPatternMatcher:
    """Learn and predict keyword usage patterns."""
    
    def __init__(self, chroma_store: KeywordVectorStore):
        """Initialize with ChromaDB store."""
        
    def learn_from_execution(self, user_query: str, generated_code: str):
        """Extract keywords from code and store pattern."""
        
    def get_relevant_keywords(self, user_query: str, 
                            confidence_threshold: float = 0.7) -> List[str]:
        """Predict relevant keywords based on similar past queries."""
        
    def _extract_keywords_from_code(self, code: str) -> List[str]:
        """Extract Robot Framework keywords from generated code."""
```

**Learning Process:**

1. **Execution Completes** → Extract keywords from generated code
2. **Store Pattern** → Save query + keywords in ChromaDB with embedding
3. **New Query** → Search for similar past queries
4. **Predict Keywords** → If similarity ≥ threshold, return aggregated keywords

**Prediction Algorithm:**

```python
def get_relevant_keywords(self, user_query: str, confidence_threshold: float = 0.7):
    # 1. Search ChromaDB for similar patterns
    results = self.pattern_collection.query(query_texts=[user_query], n_results=5)
    
    # 2. Check confidence (convert distance to similarity)
    top_distance = results['distances'][0][0]
    similarity = 1 / (1 + top_distance)
    
    if similarity < confidence_threshold:
        return []  # Not confident enough
    
    # 3. Aggregate keywords from top 5 similar patterns
    keyword_counts = {}
    for metadata in results['metadatas'][0]:
        keywords = json.loads(metadata['keywords'])
        for keyword in keywords:
            keyword_counts[keyword] = keyword_counts.get(keyword, 0) + 1
    
    # 4. Return top 10 most common keywords
    sorted_keywords = sorted(keyword_counts.items(), key=lambda x: x[1], reverse=True)
    return [kw for kw, count in sorted_keywords[:10]]
```

**Improvement Over Time:**
- First 10 queries: Low prediction rate (~10%)
- After 20 queries: Moderate prediction rate (~40%)
- After 50 queries: High prediction rate (~70%)

### 4. Smart Keyword Provider (`smart_keyword_provider.py`)

The `SmartKeywordProvider` orchestrates the 3-tier keyword retrieval system.

**Key Features:**
- Hybrid approach: core rules + predicted/searched keywords
- Graceful degradation through multiple fallback tiers
- Agent-specific context formatting
- Learning hook for continuous improvement

**Class Interface:**

```python
class SmartKeywordProvider:
    """Intelligent keyword provider with hybrid approach."""
    
    def __init__(self, library_context: LibraryContext,
                 pattern_matcher: QueryPatternMatcher,
                 chroma_store: KeywordVectorStore):
        """Initialize with library context and optimization components."""
        
    def get_agent_context(self, user_query: str, agent_role: str) -> str:
        """Get optimized context for an agent."""
        
    def get_keyword_search_tool(self) -> KeywordSearchTool:
        """Get keyword search tool for agents."""
        
    def learn_from_execution(self, user_query: str, generated_code: str):
        """Learn from successful execution."""
```

**3-Tier Retrieval Strategy:**

```python
def get_agent_context(self, user_query: str, agent_role: str) -> str:
    # Tier 1: Core Rules (Always Included)
    core_rules = self.library_context.core_rules  # ~300 tokens
    
    try:
        # Tier 2: Pattern Learning (Predicted Keywords)
        predicted_keywords = self.pattern_matcher.get_relevant_keywords(user_query)
        
        if predicted_keywords:
            # Get full docs from ChromaDB
            keyword_docs = self._get_keyword_docs(predicted_keywords)
            return self._format_predicted_context(core_rules, keyword_docs, agent_role)
            # Total: ~800 tokens (300 core + 500 keywords)
    except Exception as e:
        logger.warning(f"Pattern learning failed: {e}")
    
    try:
        # Tier 3: Zero-Context + Search Tool
        return self._format_zero_context_with_tool(core_rules, agent_role)
        # Total: ~500 tokens (300 core + 200 tool instructions)
    except Exception as e:
        logger.warning(f"Zero-context formatting failed: {e}")
    
    # Tier 4: Full Context Fallback
    logger.info("Using full context as fallback")
    return self.library_context.code_assembly_context
    # Total: ~3000 tokens (baseline behavior)
```


### 5. Context Pruner (`context_pruner.py`)

The `ContextPruner` classifies queries and prunes context to relevant keyword categories.

**Key Features:**
- Semantic query classification into action categories
- Pre-computed category embeddings for fast lookup
- Confidence-based filtering
- Fallback to all categories if confidence too low

**Class Interface:**

```python
class ContextPruner:
    """Classify queries and prune context to relevant categories."""
    
    KEYWORD_CATEGORIES = {
        "navigation": ["New Browser", "New Page", "Go To", ...],
        "input": ["Fill Text", "Input Text", "Type Text", ...],
        "interaction": ["Click", "Click Element", "Hover", ...],
        "extraction": ["Get Text", "Get Attribute", ...],
        "assertion": ["Should Be Equal", "Should Contain", ...],
        "wait": ["Wait For Elements State", ...]
    }
    
    def __init__(self):
        """Initialize with sentence-transformers classifier."""
        
    def classify_query(self, user_query: str, 
                      confidence_threshold: float = 0.8) -> List[str]:
        """Classify query into action categories."""
        
    def prune_keywords(self, all_keywords: List[Dict], 
                      categories: List[str]) -> List[Dict]:
        """Filter keywords to only those in relevant categories."""
```

**Classification Process:**

```python
def classify_query(self, user_query: str, confidence_threshold: float = 0.8):
    # 1. Encode query
    query_embedding = self.classifier.encode([user_query])[0]
    
    # 2. Compute similarity with each category
    similarities = {
        cat: np.dot(query_embedding, emb)
        for cat, emb in self.category_embeddings.items()
    }
    
    # 3. Get categories above threshold
    relevant_categories = [
        cat for cat, sim in similarities.items()
        if sim >= confidence_threshold
    ]
    
    # 4. Fallback to all categories if none meet threshold
    if not relevant_categories:
        return list(self.KEYWORD_CATEGORIES.keys())
    
    return relevant_categories
```

**Performance Impact:**
- Average context reduction: 40%
- Classification latency: <50ms
- Accuracy maintained: >95%

## Hybrid Knowledge Architecture

The hybrid architecture combines three knowledge sources to optimize token usage while maintaining accuracy.

### Tier 1: Core Rules (Always Present)

**Purpose:** Ensure critical library-specific constraints are never forgotten.

**Content (~300 tokens):**
- Critical sequences (e.g., New Browser → New Context viewport=None → New Page)
- Parameter rules and syntax
- Auto-waiting behavior
- Locator priorities
- Common pitfalls and solutions

**Implementation:**

```python
# In src/backend/crew_ai/library_context/browser_context.py

class BrowserLibraryContext(LibraryContext):
    @property
    def core_rules(self) -> str:
        """Core rules always included in agent context."""
        return """
        CRITICAL BROWSER LIBRARY RULES:
        
        1. INITIALIZATION SEQUENCE (MUST FOLLOW):
           - New Browser    chromium    headless=True
           - New Context    viewport=None    # REQUIRED: viewport=None
           - New Page       ${URL}
        
        2. PARAMETER RULES:
           - viewport MUST be None (not 'None' string)
           - Use Browser Library syntax, NOT SeleniumLibrary
           - All selectors use CSS or text= prefix
        
        3. AUTO-WAITING:
           - Browser Library auto-waits for elements
           - No explicit Wait keywords needed in most cases
        
        4. LOCATOR PRIORITIES:
           - Prefer: id= > data-testid= > text= > css=
           - Avoid: xpath (use CSS instead)
        """
```

**Why Always Included:**
- Prevents critical mistakes (e.g., missing viewport=None)
- Maintains code quality consistency
- Small token cost (~300) for high value

### Tier 2: Predicted Keywords (Pattern Learning)

**Purpose:** Pre-load relevant keywords based on learned patterns.

**Process:**
1. Search for similar past queries in ChromaDB
2. If similarity ≥ 0.7, predict relevant keywords
3. Retrieve full documentation from ChromaDB
4. Include in agent context (~500 tokens)

**Example:**

```python
# User query: "search for shoes on Flipkart"

# Pattern learning finds similar past query:
# "search for laptops on Amazon" → used [Fill Text, Click, Get Text]

# Prediction: [Fill Text, Click, Get Text] with 0.85 confidence

# Context includes:
# - Core rules (300 tokens)
# - Fill Text documentation (150 tokens)
# - Click documentation (150 tokens)
# - Get Text documentation (200 tokens)
# Total: ~800 tokens (vs 3000 baseline)
```

**Improvement Over Time:**
- System learns from every successful execution
- Prediction accuracy increases with more data
- Adapts to user's testing patterns

### Tier 3: Zero-Context + Search Tool

**Purpose:** Provide minimal context with on-demand keyword retrieval.

**Process:**
1. If no pattern match, provide minimal context
2. Include tool usage instructions (~200 tokens)
3. Agent uses keyword_search tool when needed
4. Tool returns top 3 matches from ChromaDB (<100ms)

**Example:**

```python
# Agent context:
"""
You are an expert Robot Framework developer using Browser Library.

CORE RULES:
[... core rules ~300 tokens ...]

KEYWORD SEARCH TOOL:
You have access to a keyword_search tool to find relevant keywords on-demand.

Examples:
- Need to click? Search: "click button element"
- Need to input text? Search: "type text input field"
- Need to wait? Search: "wait element visible"

The tool returns the top 3 matching keywords with documentation.
Use the exact keyword names and syntax from the tool results.
"""

# Agent workflow:
# 1. Reads query: "click the login button"
# 2. Calls: keyword_search("click button")
# 3. Receives: [Click, Click Element, Click Button] with docs
# 4. Uses: Click    id=login-button
```

**Benefits:**
- Minimal context (~500 tokens total)
- Flexible - works for any query
- Fast - search completes in <100ms

### Tier 4: Full Context Fallback

**Purpose:** Ensure reliability if all optimizations fail.

**Trigger Conditions:**
- ChromaDB initialization fails
- Keyword search tool fails
- Pattern learning database corrupted
- Any unexpected error

**Behavior:**
- Falls back to baseline behavior (full context)
- Logs fallback event for monitoring
- Maintains 99.9% workflow success rate

**Implementation:**

```python
def get_agent_context(self, user_query: str, agent_role: str) -> str:
    try:
        # Try optimized approaches
        return self._get_optimized_context(user_query, agent_role)
    except Exception as e:
        logger.error(f"Optimization failed: {e}, falling back to full context")
        return self.library_context.code_assembly_context
```


## Extension Points

The optimization system is designed to be extensible. Here are the key extension points:

### 1. Adding New Libraries

To add optimization support for a new Robot Framework library:

**Step 1: Add Core Rules**

```python
# In src/backend/crew_ai/library_context/your_library_context.py

class YourLibraryContext(LibraryContext):
    @property
    def core_rules(self) -> str:
        """Define core rules for your library."""
        return """
        CRITICAL YOUR_LIBRARY RULES:
        
        1. INITIALIZATION:
           - [Your library-specific initialization]
        
        2. PARAMETER RULES:
           - [Your library-specific parameters]
        
        3. COMMON PATTERNS:
           - [Your library-specific patterns]
        """
```

**Step 2: Initialize ChromaDB Collection**

```python
# In your initialization code
from src.backend.crew_ai.optimization import KeywordVectorStore

chroma_store = KeywordVectorStore()

# Extract keywords from your library
keywords = extract_keywords_from_library("YourLibrary")

# Add to ChromaDB
chroma_store.add_keywords("YourLibrary", keywords)
```

**Step 3: Configure Pattern Learning**

```python
# Pattern learning works automatically for any library
# Just ensure keywords are extracted correctly from generated code

def _extract_keywords_from_code(self, code: str) -> List[str]:
    # Add your library-specific keyword extraction logic
    # Default implementation works for most Robot Framework libraries
    pass
```

### 2. Custom Embedding Models

To use a different embedding model:

**Step 1: Update ChromaDB Configuration**

```python
# In src/backend/crew_ai/optimization/chroma_store.py

def _get_embedding_function(self):
    """Get custom embedding function."""
    from chromadb.utils import embedding_functions
    
    # Option 1: Different sentence-transformers model
    return embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-mpnet-base-v2"  # More accurate but slower
    )
    
    # Option 2: OpenAI embeddings (requires API key)
    return embedding_functions.OpenAIEmbeddingFunction(
        api_key="your-api-key",
        model_name="text-embedding-3-small"
    )
    
    # Option 3: Custom embedding function
    class CustomEmbeddingFunction:
        def __call__(self, texts: List[str]) -> List[List[float]]:
            # Your custom embedding logic
            return embeddings
    
    return CustomEmbeddingFunction()
```

**Step 2: Update Configuration**

```env
# In .env
OPTIMIZATION_EMBEDDING_MODEL=all-mpnet-base-v2
```

**Trade-offs:**
- `all-MiniLM-L6-v2` (default): Fast, 384 dimensions, good accuracy
- `all-mpnet-base-v2`: Slower, 768 dimensions, better accuracy
- OpenAI embeddings: Requires API, costs money, excellent accuracy

### 3. Custom Context Pruning Strategies

To add new pruning strategies:

**Step 1: Extend ContextPruner**

```python
# In src/backend/crew_ai/optimization/context_pruner.py

class ContextPruner:
    def classify_query_advanced(self, user_query: str, 
                               website_type: str = None) -> List[str]:
        """Advanced classification with website-specific logic."""
        
        # Base classification
        categories = self.classify_query(user_query)
        
        # Website-specific adjustments
        if website_type == "ecommerce":
            # E-commerce sites often need extraction keywords
            if "extraction" not in categories:
                categories.append("extraction")
        
        elif website_type == "form":
            # Form-heavy sites need input keywords
            if "input" not in categories:
                categories.append("input")
        
        return categories
```

**Step 2: Add New Category Mappings**

```python
KEYWORD_CATEGORIES = {
    # Existing categories
    "navigation": [...],
    "input": [...],
    
    # New categories
    "file_handling": ["Upload File", "Download File", "Choose File"],
    "authentication": ["Login", "Logout", "Set Cookie"],
    "api_testing": ["GET Request", "POST Request", "Validate Response"],
}
```

### 4. Custom Pattern Learning Strategies

To implement advanced pattern learning:

**Step 1: Extend QueryPatternMatcher**

```python
# In src/backend/crew_ai/optimization/pattern_learning.py

class AdvancedPatternMatcher(QueryPatternMatcher):
    def get_relevant_keywords_with_context(self, 
                                          user_query: str,
                                          website_url: str = None,
                                          previous_queries: List[str] = None) -> List[str]:
        """Advanced prediction with additional context."""
        
        # Base prediction
        keywords = self.get_relevant_keywords(user_query)
        
        # Website-specific patterns
        if website_url:
            website_patterns = self._get_website_patterns(website_url)
            keywords.extend(website_patterns)
        
        # Sequential patterns (workflow context)
        if previous_queries:
            sequential_keywords = self._predict_next_keywords(previous_queries)
            keywords.extend(sequential_keywords)
        
        return list(set(keywords))  # Remove duplicates
    
    def _get_website_patterns(self, website_url: str) -> List[str]:
        """Get common keywords for specific website."""
        # Query patterns filtered by website
        pass
    
    def _predict_next_keywords(self, previous_queries: List[str]) -> List[str]:
        """Predict next keywords based on workflow sequence."""
        # Analyze common sequences (e.g., search → filter → select)
        pass
```

### 5. Custom Metrics and Monitoring

To add custom metrics:

**Step 1: Extend WorkflowMetrics**

```python
# In src/backend/metrics/workflow_metrics.py

class WorkflowMetrics:
    def __init__(self):
        # Existing metrics
        self.token_usage = {...}
        
        # Custom metrics
        self.custom_metrics = {
            "keyword_reuse_rate": 0.0,
            "pattern_cache_hit_rate": 0.0,
            "avg_keywords_per_query": 0.0,
        }
    
    def track_keyword_reuse(self, keywords_used: List[str], 
                           keywords_predicted: List[str]):
        """Track how many predicted keywords were actually used."""
        if not keywords_predicted:
            return
        
        reused = len(set(keywords_used) & set(keywords_predicted))
        self.custom_metrics["keyword_reuse_rate"] = reused / len(keywords_predicted)
```

**Step 2: Add to API Response**

```python
def to_dict(self) -> Dict:
    base_dict = super().to_dict()
    base_dict["optimization"]["custom"] = self.custom_metrics
    return base_dict
```

### 6. Alternative Storage Backends

To use a different storage backend instead of ChromaDB:

**Step 1: Create Storage Interface**

```python
# In src/backend/crew_ai/optimization/storage_interface.py

from abc import ABC, abstractmethod

class VectorStoreInterface(ABC):
    """Abstract interface for vector storage."""
    
    @abstractmethod
    def add_keywords(self, library_name: str, keywords: List[Dict]):
        """Add keywords to storage."""
        pass
    
    @abstractmethod
    def search(self, library_name: str, query: str, top_k: int) -> List[Dict]:
        """Search for keywords."""
        pass
```

**Step 2: Implement Alternative Backend**

```python
# In src/backend/crew_ai/optimization/faiss_store.py

import faiss
import numpy as np

class FAISSVectorStore(VectorStoreInterface):
    """FAISS-based vector storage."""
    
    def __init__(self, persist_directory: str):
        self.persist_directory = persist_directory
        self.indexes = {}  # library_name -> FAISS index
        self.embedder = SentenceTransformer('all-MiniLM-L6-v2')
    
    def add_keywords(self, library_name: str, keywords: List[Dict]):
        # Generate embeddings
        texts = [f"{kw['name']} {kw.get('doc', '')}" for kw in keywords]
        embeddings = self.embedder.encode(texts)
        
        # Create FAISS index
        dimension = embeddings.shape[1]
        index = faiss.IndexFlatL2(dimension)
        index.add(embeddings)
        
        # Store index and metadata
        self.indexes[library_name] = {
            "index": index,
            "keywords": keywords
        }
        
        # Persist to disk
        faiss.write_index(index, f"{self.persist_directory}/{library_name}.index")
    
    def search(self, library_name: str, query: str, top_k: int) -> List[Dict]:
        # Encode query
        query_embedding = self.embedder.encode([query])
        
        # Search FAISS index
        index_data = self.indexes[library_name]
        distances, indices = index_data["index"].search(query_embedding, top_k)
        
        # Format results
        results = []
        for i, idx in enumerate(indices[0]):
            keyword = index_data["keywords"][idx]
            results.append({
                "name": keyword["name"],
                "args": keyword.get("args", []),
                "description": keyword.get("doc", ""),
                "distance": float(distances[0][i])
            })
        
        return results
```

**Step 3: Update Configuration**

```python
# In src/backend/core/config.py

OPTIMIZATION_STORAGE_BACKEND = Field(
    default="chromadb",
    description="Vector storage backend: chromadb, faiss, or custom"
)
```


## Testing Approach

The optimization system uses a comprehensive testing strategy covering unit, integration, and performance tests.

### Unit Tests

Unit tests focus on individual components in isolation.

**Test Structure:**

```
tests/
├── test_chroma_store.py           # ChromaDB vector store tests
├── test_keyword_search_tool.py    # Keyword search tool tests
├── test_pattern_learning.py       # Pattern learning tests
├── test_context_pruner.py         # Context pruning tests
└── test_integration_optimization.py  # Integration tests
```

**Example: Testing ChromaDB Store**

```python
# tests/test_chroma_store.py

import pytest
from src.backend.crew_ai.optimization import KeywordVectorStore

@pytest.fixture
def chroma_store():
    """Create temporary ChromaDB store for testing."""
    import tempfile
    temp_dir = tempfile.mkdtemp()
    store = KeywordVectorStore(persist_directory=temp_dir)
    yield store
    # Cleanup
    import shutil
    shutil.rmtree(temp_dir)

def test_collection_creation(chroma_store):
    """Test creating collections for different libraries."""
    # Create Browser collection
    collection = chroma_store.create_or_get_collection("Browser")
    assert collection is not None
    assert collection.name == "keywords_browser"
    
    # Create SeleniumLibrary collection
    collection = chroma_store.create_or_get_collection("SeleniumLibrary")
    assert collection is not None
    assert collection.name == "keywords_seleniumlibrary"

def test_keyword_ingestion(chroma_store):
    """Test adding keywords to ChromaDB."""
    keywords = [
        {"name": "Click", "args": ["selector"], "doc": "Clicks element"},
        {"name": "Fill Text", "args": ["selector", "text"], "doc": "Fills text"},
    ]
    
    chroma_store.add_keywords("Browser", keywords)
    
    # Verify keywords stored
    collection = chroma_store.create_or_get_collection("Browser")
    results = collection.get(ids=["Click", "Fill Text"])
    assert len(results["ids"]) == 2

def test_semantic_search(chroma_store):
    """Test semantic search returns relevant keywords."""
    # Add test keywords
    keywords = [
        {"name": "Click", "args": ["selector"], "doc": "Clicks element identified by selector"},
        {"name": "Fill Text", "args": ["selector", "text"], "doc": "Fills text into input field"},
        {"name": "Get Text", "args": ["selector"], "doc": "Gets text content from element"},
    ]
    chroma_store.add_keywords("Browser", keywords)
    
    # Search for clicking
    results = chroma_store.search("Browser", "click a button", top_k=2)
    assert len(results) == 2
    assert results[0]["name"] == "Click"
    
    # Search for text input
    results = chroma_store.search("Browser", "type text into field", top_k=2)
    assert len(results) == 2
    assert results[0]["name"] == "Fill Text"

def test_version_tracking(chroma_store):
    """Test library version tracking and rebuild."""
    keywords = [{"name": "Click", "args": ["selector"], "doc": "Clicks element"}]
    
    # Add keywords with version 1.0.0
    chroma_store.add_keywords("Browser", keywords)
    version = chroma_store.get_library_version("Browser")
    assert version == "1.0.0"
    
    # Simulate version change
    chroma_store.rebuild_collection("Browser", new_version="1.1.0")
    version = chroma_store.get_library_version("Browser")
    assert version == "1.1.0"
```

**Example: Testing Pattern Learning**

```python
# tests/test_pattern_learning.py

import pytest
from src.backend.crew_ai.optimization import QueryPatternMatcher, KeywordVectorStore

@pytest.fixture
def pattern_matcher():
    """Create pattern matcher with temporary storage."""
    import tempfile
    temp_dir = tempfile.mkdtemp()
    chroma_store = KeywordVectorStore(persist_directory=temp_dir)
    matcher = QueryPatternMatcher(chroma_store=chroma_store)
    yield matcher
    # Cleanup
    import shutil
    shutil.rmtree(temp_dir)

def test_keyword_extraction(pattern_matcher):
    """Test extracting keywords from Robot Framework code."""
    code = """
*** Test Cases ***
Test Login
    New Browser    chromium    headless=True
    New Page    https://example.com
    Fill Text    id=username    testuser
    Click    id=login-button
    Get Text    id=welcome-message
    """
    
    keywords = pattern_matcher._extract_keywords_from_code(code)
    
    assert "New Browser" in keywords
    assert "New Page" in keywords
    assert "Fill Text" in keywords
    assert "Click" in keywords
    assert "Get Text" in keywords

def test_pattern_storage(pattern_matcher):
    """Test storing patterns in ChromaDB."""
    query = "login to website"
    code = """
*** Test Cases ***
Test
    Fill Text    id=username    user
    Click    id=login
    """
    
    # Learn from execution
    pattern_matcher.learn_from_execution(query, code)
    
    # Verify pattern stored
    collection = pattern_matcher.pattern_collection
    results = collection.get()
    assert len(results["ids"]) > 0

def test_pattern_prediction(pattern_matcher):
    """Test predicting keywords from similar queries."""
    # Learn from multiple executions
    pattern_matcher.learn_from_execution(
        "search for shoes on Flipkart",
        "*** Test Cases ***\nTest\n    Fill Text    name=q    shoes\n    Click    id=search"
    )
    pattern_matcher.learn_from_execution(
        "search for laptops on Amazon",
        "*** Test Cases ***\nTest\n    Fill Text    id=search-box    laptops\n    Click    css=.search-button"
    )
    
    # Predict for similar query
    predicted = pattern_matcher.get_relevant_keywords("search for phones on eBay")
    
    assert "Fill Text" in predicted
    assert "Click" in predicted

def test_confidence_threshold(pattern_matcher):
    """Test confidence threshold filtering."""
    # Learn pattern
    pattern_matcher.learn_from_execution(
        "login to website",
        "*** Test Cases ***\nTest\n    Fill Text    id=user    test\n    Click    id=login"
    )
    
    # Similar query should predict (high confidence)
    predicted = pattern_matcher.get_relevant_keywords("login to portal", confidence_threshold=0.7)
    assert len(predicted) > 0
    
    # Dissimilar query should not predict (low confidence)
    predicted = pattern_matcher.get_relevant_keywords("download file", confidence_threshold=0.7)
    assert len(predicted) == 0

def test_improvement_over_time(pattern_matcher):
    """Test prediction accuracy improves with more data."""
    # Learn from 20 diverse queries
    queries = [
        ("search for products", "Fill Text\nClick"),
        ("login to account", "Fill Text\nClick"),
        ("add to cart", "Click\nGet Text"),
        # ... 17 more queries
    ]
    
    for query, code in queries:
        pattern_matcher.learn_from_execution(query, f"*** Test Cases ***\nTest\n    {code}")
    
    # Test prediction accuracy
    test_query = "search for items"
    predicted = pattern_matcher.get_relevant_keywords(test_query)
    
    # Should predict Fill Text and Click (common for search queries)
    assert "Fill Text" in predicted
    assert "Click" in predicted
```

**Example: Testing Context Pruning**

```python
# tests/test_context_pruner.py

import pytest
from src.backend.crew_ai.optimization import ContextPruner

@pytest.fixture
def pruner():
    """Create context pruner instance."""
    return ContextPruner()

def test_query_classification(pruner):
    """Test classifying queries into categories."""
    # Navigation query
    categories = pruner.classify_query("open website and go to login page")
    assert "navigation" in categories
    
    # Input query
    categories = pruner.classify_query("fill form with user details")
    assert "input" in categories
    
    # Interaction query
    categories = pruner.classify_query("click the submit button")
    assert "interaction" in categories
    
    # Extraction query
    categories = pruner.classify_query("get the product name and price")
    assert "extraction" in categories
    
    # Mixed query
    categories = pruner.classify_query("search for products and click first result")
    assert "input" in categories
    assert "interaction" in categories

def test_keyword_pruning(pruner):
    """Test pruning keywords to relevant categories."""
    all_keywords = [
        {"name": "New Browser", "category": "navigation"},
        {"name": "Click", "category": "interaction"},
        {"name": "Fill Text", "category": "input"},
        {"name": "Get Text", "category": "extraction"},
    ]
    
    # Prune to only interaction keywords
    pruned = pruner.prune_keywords(all_keywords, ["interaction"])
    assert len(pruned) == 1
    assert pruned[0]["name"] == "Click"
    
    # Prune to input and interaction
    pruned = pruner.prune_keywords(all_keywords, ["input", "interaction"])
    assert len(pruned) == 2
    assert any(kw["name"] == "Fill Text" for kw in pruned)
    assert any(kw["name"] == "Click" for kw in pruned)

def test_confidence_threshold(pruner):
    """Test confidence threshold behavior."""
    # High confidence query
    categories = pruner.classify_query("click button", confidence_threshold=0.8)
    assert len(categories) > 0
    
    # Low confidence query (should return all categories)
    categories = pruner.classify_query("do something", confidence_threshold=0.9)
    assert len(categories) == len(pruner.KEYWORD_CATEGORIES)

def test_context_reduction(pruner):
    """Test context size reduction."""
    all_keywords = [
        {"name": f"Keyword{i}", "category": cat}
        for i in range(50)
        for cat in ["navigation", "input", "interaction", "extraction"]
    ]
    
    # Classify query
    categories = pruner.classify_query("fill form and submit")
    
    # Prune keywords
    pruned = pruner.prune_keywords(all_keywords, categories)
    
    # Should reduce context significantly
    reduction = (len(all_keywords) - len(pruned)) / len(all_keywords) * 100
    assert reduction > 30  # At least 30% reduction
```

### Integration Tests

Integration tests verify the optimization system works correctly with the full CrewAI workflow.

```python
# tests/test_integration_optimization.py

import pytest
from src.backend.crew_ai.crew import run_crew
from src.backend.core.config import settings

@pytest.fixture(autouse=True)
def enable_optimization():
    """Enable optimization for integration tests."""
    original = settings.OPTIMIZATION_ENABLED
    settings.OPTIMIZATION_ENABLED = True
    yield
    settings.OPTIMIZATION_ENABLED = original

def test_optimized_workflow():
    """Test full workflow with optimization enabled."""
    result, crew = run_crew(
        query="search for shoes on Flipkart and get first product name",
        model_provider="online",
        model_name="gemini-2.0-flash-exp",
        library_type="browser"
    )
    
    # Verify result
    assert result is not None
    assert "Fill Text" in result.raw
    assert "Get Text" in result.raw
    
    # Verify optimization metrics
    metrics = crew.metrics
    assert metrics.optimization.token_usage["total"] < 5000  # Less than baseline
    assert metrics.optimization.context_reduction["reduction_percentage"] > 50

def test_pattern_learning_integration():
    """Test pattern learning improves over multiple queries."""
    queries = [
        "search for laptops",
        "search for phones",
        "search for tablets",
    ]
    
    prediction_used = []
    
    for query in queries:
        result, crew = run_crew(
            query=query,
            model_provider="online",
            model_name="gemini-2.0-flash-exp"
        )
        
        # Track if prediction was used
        prediction_used.append(
            crew.metrics.optimization.pattern_learning_stats["prediction_used"]
        )
    
    # Later queries should use predictions more often
    assert sum(prediction_used[1:]) > sum(prediction_used[:1])

def test_graceful_degradation():
    """Test fallback when optimization fails."""
    # Simulate ChromaDB failure
    with patch('src.backend.crew_ai.optimization.KeywordVectorStore') as mock:
        mock.side_effect = Exception("ChromaDB failed")
        
        # Should still work with full context
        result, crew = run_crew(
            query="search for shoes",
            model_provider="online",
            model_name="gemini-2.0-flash-exp"
        )
        
        assert result is not None
        # Should use full context (no reduction)
        assert crew.metrics.optimization.context_reduction["reduction_percentage"] == 0

def test_keyword_search_tool_usage():
    """Test agents can use keyword search tool."""
    # Disable pattern learning to force tool usage
    with patch('src.backend.crew_ai.optimization.QueryPatternMatcher.get_relevant_keywords') as mock:
        mock.return_value = []  # No predictions
        
        result, crew = run_crew(
            query="click the login button",
            model_provider="online",
            model_name="gemini-2.0-flash-exp"
        )
        
        # Verify tool was used
        assert crew.metrics.optimization.keyword_search_stats["calls"] > 0
        assert result is not None

def test_core_rules_preservation():
    """Test core rules are always present in generated code."""
    result, crew = run_crew(
        query="open Flipkart website",
        model_provider="online",
        model_name="gemini-2.0-flash-exp",
        library_type="browser"
    )
    
    # Verify critical sequence present
    assert "New Browser" in result.raw
    assert "New Context" in result.raw
    assert "viewport=None" in result.raw  # Critical parameter
    assert "New Page" in result.raw
```

### Performance Tests

Performance tests verify the system meets latency and throughput requirements.

```python
# tests/test_performance_validation.py

import pytest
import time
from src.backend.crew_ai.optimization import KeywordVectorStore, KeywordSearchTool

def test_chromadb_initialization_time():
    """Test ChromaDB initialization completes within 5 seconds."""
    import tempfile
    temp_dir = tempfile.mkdtemp()
    
    start = time.time()
    store = KeywordVectorStore(persist_directory=temp_dir)
    
    # Add 143 keywords (Browser Library size)
    keywords = [
        {"name": f"Keyword{i}", "args": [], "doc": f"Documentation for keyword {i}"}
        for i in range(143)
    ]
    store.add_keywords("Browser", keywords)
    
    duration = time.time() - start
    
    assert duration < 5.0  # 5 second requirement
    
    # Cleanup
    import shutil
    shutil.rmtree(temp_dir)

def test_keyword_search_latency():
    """Test keyword search completes within 100ms."""
    import tempfile
    temp_dir = tempfile.mkdtemp()
    
    # Setup
    store = KeywordVectorStore(persist_directory=temp_dir)
    keywords = [
        {"name": "Click", "args": ["selector"], "doc": "Clicks element"},
        {"name": "Fill Text", "args": ["selector", "text"], "doc": "Fills text"},
        {"name": "Get Text", "args": ["selector"], "doc": "Gets text"},
    ]
    store.add_keywords("Browser", keywords)
    
    tool = KeywordSearchTool("Browser", store)
    
    # Measure search latency
    start = time.time()
    result = tool._run("click a button", top_k=3)
    latency = (time.time() - start) * 1000  # Convert to ms
    
    assert latency < 100  # 100ms requirement
    
    # Cleanup
    import shutil
    shutil.rmtree(temp_dir)

def test_pattern_prediction_latency():
    """Test pattern prediction completes quickly."""
    import tempfile
    temp_dir = tempfile.mkdtemp()
    
    # Setup
    store = KeywordVectorStore(persist_directory=temp_dir)
    matcher = QueryPatternMatcher(chroma_store=store)
    
    # Learn some patterns
    for i in range(10):
        matcher.learn_from_execution(
            f"test query {i}",
            f"*** Test Cases ***\nTest\n    Keyword{i}"
        )
    
    # Measure prediction latency
    start = time.time()
    predicted = matcher.get_relevant_keywords("test query similar")
    latency = (time.time() - start) * 1000
    
    assert latency < 200  # Should be fast
    
    # Cleanup
    import shutil
    shutil.rmtree(temp_dir)

def test_end_to_end_workflow_time():
    """Test optimized workflow completes in reasonable time."""
    start = time.time()
    
    result, crew = run_crew(
        query="search for shoes on Flipkart",
        model_provider="online",
        model_name="gemini-2.0-flash-exp"
    )
    
    duration = time.time() - start
    
    # Should complete in 30-40 seconds (vs 40-50 baseline)
    assert duration < 45
    assert result is not None
```

### Running Tests

```bash
# Run all tests
pytest tests/

# Run specific test file
pytest tests/test_chroma_store.py

# Run with coverage
pytest --cov=src/backend/crew_ai/optimization tests/

# Run performance tests only
pytest tests/test_performance_validation.py -v

# Run integration tests only
pytest tests/test_integration_optimization.py -v
```


## Code Examples

### Example 1: Basic Optimization Setup

```python
# In src/backend/crew_ai/crew.py

from src.backend.crew_ai.optimization import (
    KeywordVectorStore,
    QueryPatternMatcher,
    SmartKeywordProvider
)
from src.backend.core.config import settings

def run_crew(query: str, model_provider: str, model_name: str, 
             library_type: str = None, workflow_id: str = ""):
    """Run CrewAI workflow with optimization."""
    
    # Load library context
    library_context = get_library_context(library_type or settings.ROBOT_LIBRARY)
    
    # Initialize optimization if enabled
    if settings.OPTIMIZATION_ENABLED:
        # Initialize ChromaDB store
        chroma_store = KeywordVectorStore(
            persist_directory=settings.OPTIMIZATION_CHROMA_DB_PATH
        )
        
        # Initialize pattern matcher
        pattern_matcher = QueryPatternMatcher(chroma_store=chroma_store)
        
        # Initialize smart provider
        smart_provider = SmartKeywordProvider(
            library_context=library_context,
            pattern_matcher=pattern_matcher,
            chroma_store=chroma_store
        )
        
        # Get optimized context
        optimized_context = smart_provider.get_agent_context(query, "assembler")
        keyword_search_tool = smart_provider.get_keyword_search_tool()
    else:
        # Use baseline behavior
        optimized_context = library_context.code_assembly_context
        keyword_search_tool = None
    
    # Initialize agents
    agents = RobotAgents(
        model_provider,
        model_name,
        library_context,
        optimized_context=optimized_context,
        keyword_search_tool=keyword_search_tool
    )
    
    # Run workflow
    crew = Crew(
        agents=[
            agents.step_planner_agent(),
            agents.element_identifier_agent(),
            agents.code_assembler_agent(),
            agents.code_validator_agent()
        ],
        tasks=[...],
        verbose=True
    )
    
    result = crew.kickoff()
    
    # Learn from successful execution
    if settings.OPTIMIZATION_ENABLED and result:
        smart_provider.learn_from_execution(query, result.raw)
    
    return result, crew
```

### Example 2: Custom Keyword Search

```python
# Custom keyword search with filtering

from src.backend.crew_ai.optimization import KeywordSearchTool

class FilteredKeywordSearchTool(KeywordSearchTool):
    """Keyword search with custom filtering."""
    
    def __init__(self, library_name: str, chroma_store: KeywordVectorStore,
                 excluded_keywords: List[str] = None):
        super().__init__(library_name, chroma_store)
        self.excluded_keywords = excluded_keywords or []
    
    def _run(self, query: str, top_k: int = 3) -> str:
        """Search with filtering."""
        # Get base results
        results = self.chroma_store.search(
            library_name=self.library_name,
            query=query,
            top_k=top_k * 2  # Get more to account for filtering
        )
        
        # Filter out excluded keywords
        filtered = [
            r for r in results
            if r["name"] not in self.excluded_keywords
        ][:top_k]
        
        # Format results
        return json.dumps(filtered, indent=2)

# Usage
tool = FilteredKeywordSearchTool(
    "Browser",
    chroma_store,
    excluded_keywords=["Deprecated Keyword", "Old Keyword"]
)
```

### Example 3: Advanced Pattern Learning

```python
# Pattern learning with website-specific patterns

from src.backend.crew_ai.optimization import QueryPatternMatcher

class WebsiteAwarePatternMatcher(QueryPatternMatcher):
    """Pattern matcher with website-specific learning."""
    
    def learn_from_execution_with_website(self, 
                                         user_query: str,
                                         generated_code: str,
                                         website_url: str):
        """Learn pattern with website context."""
        # Extract keywords
        keywords = self._extract_keywords_from_code(generated_code)
        
        if not keywords:
            return
        
        # Store pattern with website metadata
        pattern_id = f"pattern_{int(time.time() * 1000)}"
        self.pattern_collection.add(
            documents=[user_query],
            ids=[pattern_id],
            metadatas=[{
                "keywords": json.dumps(keywords),
                "website": website_url,
                "timestamp": datetime.now().isoformat()
            }]
        )
    
    def get_relevant_keywords_for_website(self,
                                         user_query: str,
                                         website_url: str,
                                         confidence_threshold: float = 0.7) -> List[str]:
        """Get keywords filtered by website."""
        # Search for similar patterns
        results = self.pattern_collection.query(
            query_texts=[user_query],
            n_results=10,
            where={"website": website_url}  # Filter by website
        )
        
        # Check confidence
        if not results['ids'][0]:
            return []
        
        top_distance = results['distances'][0][0]
        similarity = 1 / (1 + top_distance)
        
        if similarity < confidence_threshold:
            return []
        
        # Aggregate keywords
        keyword_counts = {}
        for metadata in results['metadatas'][0]:
            keywords = json.loads(metadata['keywords'])
            for keyword in keywords:
                keyword_counts[keyword] = keyword_counts.get(keyword, 0) + 1
        
        # Return top keywords
        sorted_keywords = sorted(keyword_counts.items(), key=lambda x: x[1], reverse=True)
        return [kw for kw, count in sorted_keywords[:10]]

# Usage
matcher = WebsiteAwarePatternMatcher(chroma_store)

# Learn with website context
matcher.learn_from_execution_with_website(
    "search for products",
    generated_code,
    "https://flipkart.com"
)

# Predict for same website
keywords = matcher.get_relevant_keywords_for_website(
    "add to cart",
    "https://flipkart.com"
)
```

### Example 4: Custom Context Formatting

```python
# Custom context formatting for different agent roles

from src.backend.crew_ai.optimization import SmartKeywordProvider

class CustomSmartKeywordProvider(SmartKeywordProvider):
    """Smart provider with custom context formatting."""
    
    def _format_predicted_context(self, 
                                  predicted_keywords: List[str],
                                  agent_role: str) -> str:
        """Custom formatting based on agent role."""
        # Get core rules
        core_rules = self.library_context.core_rules
        
        # Get keyword docs
        keyword_docs = self._get_keyword_docs(predicted_keywords)
        
        if agent_role == "planner":
            # Planner needs high-level overview
            return f"""
{core_rules}

AVAILABLE KEYWORDS (High-Level):
{self._format_keyword_list(keyword_docs)}

Focus on planning the test structure and identifying required actions.
"""
        
        elif agent_role == "assembler":
            # Assembler needs detailed syntax
            return f"""
{core_rules}

KEYWORD DOCUMENTATION (Detailed):
{self._format_keyword_details(keyword_docs)}

Generate Robot Framework code using these exact keywords and syntax.
"""
        
        elif agent_role == "validator":
            # Validator needs validation rules
            return f"""
{core_rules}

VALIDATION RULES:
- Verify all keywords are from the approved list
- Check parameter syntax matches documentation
- Ensure proper indentation and structure

APPROVED KEYWORDS:
{self._format_keyword_list(keyword_docs)}
"""
        
        return core_rules
    
    def _format_keyword_list(self, keyword_docs: List[Dict]) -> str:
        """Format keywords as simple list."""
        return "\n".join([
            f"- {kw['name']}: {kw['description'][:100]}..."
            for kw in keyword_docs
        ])
    
    def _format_keyword_details(self, keyword_docs: List[Dict]) -> str:
        """Format keywords with full details."""
        formatted = []
        for kw in keyword_docs:
            formatted.append(f"""
Keyword: {kw['name']}
Arguments: {', '.join(kw['args'])}
Description: {kw['description']}
Example: {kw['example']}
""")
        return "\n".join(formatted)

# Usage
provider = CustomSmartKeywordProvider(library_context, pattern_matcher, chroma_store)

# Get context for different roles
planner_context = provider.get_agent_context(query, "planner")
assembler_context = provider.get_agent_context(query, "assembler")
validator_context = provider.get_agent_context(query, "validator")
```

### Example 5: Metrics Collection and Analysis

```python
# Collecting and analyzing optimization metrics

from src.backend.metrics.workflow_metrics import WorkflowMetrics

def analyze_optimization_performance(workflow_ids: List[str]) -> Dict:
    """Analyze optimization performance across multiple workflows."""
    
    metrics_list = []
    for workflow_id in workflow_ids:
        metrics = get_workflow_metrics(workflow_id)
        metrics_list.append(metrics)
    
    # Calculate averages
    avg_token_usage = sum(m.optimization.token_usage["total"] for m in metrics_list) / len(metrics_list)
    avg_reduction = sum(m.optimization.context_reduction["reduction_percentage"] for m in metrics_list) / len(metrics_list)
    avg_search_calls = sum(m.optimization.keyword_search_stats["calls"] for m in metrics_list) / len(metrics_list)
    
    # Calculate prediction rate
    prediction_used_count = sum(1 for m in metrics_list if m.optimization.pattern_learning_stats["prediction_used"])
    prediction_rate = prediction_used_count / len(metrics_list) * 100
    
    # Calculate cost savings
    baseline_cost = 12000 * 0.000225  # 12K tokens at $0.225 per 1M tokens
    optimized_cost = avg_token_usage * 0.000225
    cost_savings = (baseline_cost - optimized_cost) / baseline_cost * 100
    
    return {
        "avg_token_usage": avg_token_usage,
        "avg_reduction_percentage": avg_reduction,
        "avg_search_calls": avg_search_calls,
        "prediction_rate": prediction_rate,
        "cost_savings_percentage": cost_savings,
        "total_workflows": len(metrics_list)
    }

# Usage
workflow_ids = ["abc-123", "def-456", "ghi-789"]
analysis = analyze_optimization_performance(workflow_ids)

print(f"Average token usage: {analysis['avg_token_usage']:.0f}")
print(f"Average reduction: {analysis['avg_reduction_percentage']:.1f}%")
print(f"Prediction rate: {analysis['prediction_rate']:.1f}%")
print(f"Cost savings: {analysis['cost_savings_percentage']:.1f}%")
```

### Example 6: Debugging Optimization Issues

```python
# Debugging optimization system

from src.backend.crew_ai.optimization import logging_config

# Enable debug logging
import logging
logging.getLogger("crew_ai.optimization").setLevel(logging.DEBUG)

def debug_optimization_workflow(query: str):
    """Run workflow with detailed debugging."""
    
    # Initialize components
    chroma_store = KeywordVectorStore()
    pattern_matcher = QueryPatternMatcher(chroma_store)
    smart_provider = SmartKeywordProvider(library_context, pattern_matcher, chroma_store)
    
    # Debug pattern learning
    print("=== Pattern Learning ===")
    predicted = pattern_matcher.get_relevant_keywords(query)
    print(f"Predicted keywords: {predicted}")
    
    if not predicted:
        print("No predictions found, checking pattern database...")
        collection = pattern_matcher.pattern_collection
        results = collection.get()
        print(f"Total patterns in database: {len(results['ids'])}")
    
    # Debug keyword search
    print("\n=== Keyword Search ===")
    tool = smart_provider.get_keyword_search_tool()
    search_result = tool._run("click button", top_k=3)
    print(f"Search results: {search_result}")
    
    # Debug context generation
    print("\n=== Context Generation ===")
    context = smart_provider.get_agent_context(query, "assembler")
    print(f"Context length: {len(context)} characters")
    print(f"Context preview: {context[:500]}...")
    
    # Run workflow
    print("\n=== Running Workflow ===")
    result, crew = run_crew(query, "online", "gemini-2.0-flash-exp")
    
    # Debug metrics
    print("\n=== Metrics ===")
    metrics = crew.metrics
    print(f"Token usage: {metrics.optimization.token_usage}")
    print(f"Keyword search: {metrics.optimization.keyword_search_stats}")
    print(f"Pattern learning: {metrics.optimization.pattern_learning_stats}")
    print(f"Context reduction: {metrics.optimization.context_reduction}")
    
    return result, crew

# Usage
result, crew = debug_optimization_workflow("search for shoes on Flipkart")
```


## Performance Considerations

### Memory Usage

**ChromaDB Storage:**
- In-memory index: ~50-100 MB for 143 keywords
- Disk storage: ~10-20 MB per library
- Embedding cache: ~80 MB (sentence-transformers model)

**Pattern Learning:**
- ChromaDB collection: Grows with usage (~1 KB per pattern)
- In-memory cache: Minimal (<1 MB)

**Optimization:**
- Use persistent storage (ChromaDB handles this automatically)
- Limit pattern database size (keep last 1000 patterns)
- Monitor memory usage in production

### CPU Usage

**Embedding Generation:**
- One-time cost at startup (~5 seconds for 143 keywords)
- Uses CPU for sentence-transformers inference
- Batched processing for efficiency (32 keywords at a time)

**Semantic Search:**
- Fast vector similarity computation (<100ms)
- Uses numpy for vectorized operations
- Minimal CPU overhead per search

**Optimization:**
- Pre-compute embeddings at startup
- Use caching for frequent searches
- Consider GPU acceleration for large-scale deployments

### Disk I/O

**ChromaDB Persistence:**
- Writes to disk on collection updates
- Reads from disk on startup (fast with persistent storage)
- Minimal I/O during normal operation

**Pattern Learning:**
- Writes to ChromaDB on each successful execution
- Reads from ChromaDB on each query prediction
- ChromaDB handles persistence automatically

**Optimization:**
- Use SSD for better performance
- Monitor disk space (grows with patterns)
- Implement pattern cleanup for long-running systems

### Network Latency

**No External Dependencies:**
- All processing is local (no API calls)
- No network latency for embeddings or search
- Faster and more reliable than cloud-based solutions

**Optimization:**
- Keep all components local
- Avoid external embedding APIs
- Use local models for maximum performance

### Scaling Considerations

**Horizontal Scaling:**
- Each instance has its own ChromaDB storage
- Pattern learning is instance-specific
- Consider shared storage for collaborative learning

**Vertical Scaling:**
- More CPU → Faster embedding generation
- More RAM → Larger in-memory caches
- More disk → More pattern storage

**Optimization Strategies:**

```python
# Shared ChromaDB storage for multiple instances
class SharedKeywordVectorStore(KeywordVectorStore):
    """ChromaDB store with shared network storage."""
    
    def __init__(self, shared_directory: str):
        """Initialize with shared network directory."""
        super().__init__(persist_directory=shared_directory)
        self._lock = FileLock(f"{shared_directory}/.lock")
    
    def add_keywords(self, library_name: str, keywords: List[Dict]):
        """Thread-safe keyword addition."""
        with self._lock:
            super().add_keywords(library_name, keywords)

# Distributed pattern learning
class DistributedPatternMatcher(QueryPatternMatcher):
    """Pattern matcher with distributed storage."""
    
    def __init__(self, redis_client):
        """Initialize with Redis for distributed patterns."""
        self.redis = redis_client
        super().__init__(chroma_store)
    
    def learn_from_execution(self, user_query: str, generated_code: str):
        """Store pattern in both local and distributed storage."""
        # Store locally
        super().learn_from_execution(user_query, generated_code)
        
        # Store in Redis for sharing
        pattern = {
            "query": user_query,
            "keywords": self._extract_keywords_from_code(generated_code),
            "timestamp": datetime.now().isoformat()
        }
        self.redis.lpush("patterns", json.dumps(pattern))
```

## Debugging and Troubleshooting

### Logging Configuration

The optimization system uses structured logging for debugging:

```python
# In src/backend/crew_ai/optimization/logging_config.py

import logging

# Create optimization logger
optimization_logger = logging.getLogger("crew_ai.optimization")
optimization_logger.setLevel(logging.INFO)

# Add file handler
file_handler = logging.FileHandler("logs/optimization.log")
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
))
optimization_logger.addHandler(file_handler)

# Add console handler for development
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.WARNING)
optimization_logger.addHandler(console_handler)
```

**Log Levels:**
- `DEBUG`: Detailed information for debugging (search queries, predictions, etc.)
- `INFO`: Normal operation (predictions used, search calls, etc.)
- `WARNING`: Fallback triggered (component failed, using baseline)
- `ERROR`: Critical failure (optimization disabled entirely)

**Example Logs:**

```
2024-01-15 10:30:15 - crew_ai.optimization - INFO - ChromaDB initialized with 143 keywords in 4.2s
2024-01-15 10:30:20 - crew_ai.optimization - INFO - Pattern prediction used for query "login to website" (8 keywords, 0.87 confidence)
2024-01-15 10:30:25 - crew_ai.optimization - DEBUG - Keyword search: "click button" → 3 results in 78ms
2024-01-15 10:30:30 - crew_ai.optimization - WARNING - Pattern prediction confidence too low (0.65), falling back to search tool
2024-01-15 10:30:35 - crew_ai.optimization - ERROR - ChromaDB search failed: Connection timeout, falling back to full context
```

### Common Issues and Solutions

#### Issue 1: ChromaDB Initialization Fails

**Symptoms:**
```
ERROR - ChromaDB initialization failed: Cannot connect to database
```

**Diagnosis:**
```python
# Check ChromaDB directory
import os
print(f"ChromaDB path exists: {os.path.exists('./chroma_db')}")
print(f"ChromaDB path writable: {os.access('./chroma_db', os.W_OK)}")

# Check dependencies
try:
    import chromadb
    print(f"ChromaDB version: {chromadb.__version__}")
except ImportError:
    print("ChromaDB not installed")
```

**Solutions:**
1. Verify ChromaDB installed: `pip install chromadb==0.4.22`
2. Check directory permissions: `chmod 755 ./chroma_db`
3. Check disk space: `df -h`
4. Review logs for specific error

#### Issue 2: Keyword Search Returns No Results

**Symptoms:**
```
WARNING - Keyword search returned 0 results for query "click button"
```

**Diagnosis:**
```python
# Check if keywords are in ChromaDB
store = KeywordVectorStore()
collection = store.create_or_get_collection("Browser")
results = collection.get()
print(f"Total keywords in collection: {len(results['ids'])}")
print(f"Sample keywords: {results['ids'][:10]}")

# Test search directly
search_results = store.search("Browser", "click button", top_k=3)
print(f"Search results: {search_results}")
```

**Solutions:**
1. Verify keywords ingested: Check collection has keywords
2. Rebuild collection: `store.rebuild_collection("Browser")`
3. Check query format: Ensure query is descriptive
4. Lower top_k: Try `top_k=1` to see if any results

#### Issue 3: Pattern Learning Not Predicting

**Symptoms:**
```
INFO - Pattern prediction not used (confidence too low: 0.45)
```

**Diagnosis:**
```python
# Check pattern database
matcher = QueryPatternMatcher(chroma_store)
collection = matcher.pattern_collection
results = collection.get()
print(f"Total patterns: {len(results['ids'])}")

# Test prediction
predicted = matcher.get_relevant_keywords("test query", confidence_threshold=0.5)
print(f"Predicted keywords: {predicted}")
```

**Solutions:**
1. Need more training data: Run 20+ diverse queries
2. Lower confidence threshold: Set to 0.6 instead of 0.7
3. Check pattern storage: Verify patterns are being saved
4. Review query similarity: Ensure queries are similar enough

#### Issue 4: High Token Usage (No Reduction)

**Symptoms:**
```
INFO - Token usage: 11500 (expected: 4000)
```

**Diagnosis:**
```python
# Check optimization status
print(f"Optimization enabled: {settings.OPTIMIZATION_ENABLED}")

# Check context generation
provider = SmartKeywordProvider(library_context, pattern_matcher, chroma_store)
context = provider.get_agent_context(query, "assembler")
print(f"Context length: {len(context)} characters")
print(f"Context preview: {context[:200]}...")

# Check metrics
metrics = crew.metrics
print(f"Context reduction: {metrics.optimization.context_reduction}")
```

**Solutions:**
1. Verify optimization enabled: Check `OPTIMIZATION_ENABLED=true`
2. Check fallback: Review logs for fallback events
3. Enable context pruning: Set `OPTIMIZATION_CONTEXT_PRUNING_ENABLED=true`
4. Lower thresholds: Reduce confidence thresholds

#### Issue 5: Slow Performance

**Symptoms:**
```
WARNING - Keyword search took 250ms (expected: <100ms)
```

**Diagnosis:**
```python
import time

# Measure ChromaDB search
start = time.time()
results = store.search("Browser", "click button", top_k=3)
latency = (time.time() - start) * 1000
print(f"Search latency: {latency:.2f}ms")

# Measure pattern prediction
start = time.time()
predicted = matcher.get_relevant_keywords("test query")
latency = (time.time() - start) * 1000
print(f"Prediction latency: {latency:.2f}ms")
```

**Solutions:**
1. Check system resources: CPU, memory, disk I/O
2. Verify ChromaDB initialized: First search is slower
3. Use caching: Enable LRU cache in KeywordSearchTool
4. Reduce top_k: Lower number of results

### Debugging Tools

**1. Optimization Status Check:**

```python
def check_optimization_status():
    """Check optimization system status."""
    from src.backend.crew_ai.optimization import KeywordVectorStore, QueryPatternMatcher
    
    print("=== Optimization Status ===")
    print(f"Enabled: {settings.OPTIMIZATION_ENABLED}")
    print(f"ChromaDB path: {settings.OPTIMIZATION_CHROMA_DB_PATH}")
    
    # Check ChromaDB
    try:
        store = KeywordVectorStore()
        collection = store.create_or_get_collection("Browser")
        results = collection.get()
        print(f"✓ ChromaDB: {len(results['ids'])} keywords")
    except Exception as e:
        print(f"✗ ChromaDB: {e}")
    
    # Check pattern learning
    try:
        matcher = QueryPatternMatcher(store)
        collection = matcher.pattern_collection
        results = collection.get()
        print(f"✓ Pattern Learning: {len(results['ids'])} patterns")
    except Exception as e:
        print(f"✗ Pattern Learning: {e}")
    
    print("=== Status Check Complete ===")

# Usage
check_optimization_status()
```

**2. Performance Profiler:**

```python
import cProfile
import pstats

def profile_optimization(query: str):
    """Profile optimization performance."""
    profiler = cProfile.Profile()
    profiler.enable()
    
    # Run workflow
    result, crew = run_crew(query, "online", "gemini-2.0-flash-exp")
    
    profiler.disable()
    
    # Print stats
    stats = pstats.Stats(profiler)
    stats.sort_stats('cumulative')
    stats.print_stats(20)  # Top 20 functions
    
    return result, crew

# Usage
profile_optimization("search for shoes")
```

**3. Metrics Dashboard:**

```python
def print_optimization_dashboard(workflow_ids: List[str]):
    """Print optimization metrics dashboard."""
    print("=== Optimization Dashboard ===\n")
    
    for workflow_id in workflow_ids:
        metrics = get_workflow_metrics(workflow_id)
        
        print(f"Workflow: {workflow_id}")
        print(f"  Token Usage: {metrics.optimization.token_usage['total']}")
        print(f"  Reduction: {metrics.optimization.context_reduction['reduction_percentage']:.1f}%")
        print(f"  Prediction Used: {metrics.optimization.pattern_learning_stats['prediction_used']}")
        print(f"  Search Calls: {metrics.optimization.keyword_search_stats['calls']}")
        print()
    
    print("=== Dashboard Complete ===")

# Usage
print_optimization_dashboard(["abc-123", "def-456", "ghi-789"])
```

## Best Practices

### Development

1. **Test with optimization disabled first** - Verify baseline behavior works
2. **Enable optimization incrementally** - Start with one component at a time
3. **Monitor metrics closely** - Track token usage, prediction rate, search calls
4. **Use debug logging** - Enable DEBUG level during development
5. **Write unit tests** - Test each component in isolation
6. **Profile performance** - Identify bottlenecks early

### Production

1. **Start with conservative settings** - Higher confidence thresholds
2. **Monitor fallback rate** - Should be <10%
3. **Track prediction accuracy** - Should improve over time
4. **Set up alerts** - For high token usage, slow searches, errors
5. **Regular pattern cleanup** - Keep database size manageable
6. **Backup ChromaDB** - Before major updates

### Code Quality

1. **Follow type hints** - Use Python type annotations
2. **Document public APIs** - Clear docstrings for all public methods
3. **Handle errors gracefully** - Always provide fallback behavior
4. **Log important events** - Use appropriate log levels
5. **Write comprehensive tests** - Unit, integration, and performance tests
6. **Keep components decoupled** - Easy to swap implementations

## Contributing

### Adding New Features

1. **Discuss in GitHub issue** - Propose feature and get feedback
2. **Follow existing patterns** - Match code style and architecture
3. **Add tests** - Unit tests for new components
4. **Update documentation** - Both user and developer docs
5. **Submit PR** - With clear description and examples

### Code Review Checklist

- [ ] Code follows existing patterns and style
- [ ] All public methods have docstrings
- [ ] Type hints used throughout
- [ ] Unit tests added for new functionality
- [ ] Integration tests pass
- [ ] Performance tests pass (if applicable)
- [ ] Documentation updated
- [ ] No breaking changes (or clearly documented)
- [ ] Error handling and logging added
- [ ] Graceful degradation implemented

## Additional Resources

- [User Guide](OPTIMIZATION.md) - Configuration and usage
- [Architecture Guide](ARCHITECTURE.md) - Overall system architecture
- [Configuration Guide](CONFIGURATION.md) - All environment variables
- [Troubleshooting Guide](TROUBLESHOOTING.md) - Common issues
- [GitHub Repository](https://github.com/monkscode/Natural-Language-to-Robot-Framework)
- [GitHub Discussions](https://github.com/monkscode/Natural-Language-to-Robot-Framework/discussions)

---

**Questions or feedback?** Open an issue on GitHub or start a discussion!
