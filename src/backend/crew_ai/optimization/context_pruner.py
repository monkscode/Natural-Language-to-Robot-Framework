"""
Context Pruner for Smart Keyword Filtering

This module classifies user queries into action categories and prunes
keyword context to include only relevant keywords, reducing token usage
while maintaining code generation accuracy.
"""

import logging
from typing import List, Dict
import chromadb
from chromadb.config import Settings
from chromadb.utils import embedding_functions

logger = logging.getLogger(__name__)


class ContextPruner:
    """
    Classify queries and prune context to relevant keyword categories.
    
    Uses ChromaDB for semantic similarity to classify queries into action 
    categories (navigation, input, interaction, extraction, assertion, wait) 
    and filters keywords to only those in relevant categories.
    """
    
    # Keyword category mappings
    KEYWORD_CATEGORIES = {
        "navigation": [
            "New Browser", "New Page", "Go To", "Go Back", "Go Forward",
            "Close Browser", "Close Page", "Switch Page", "New Context"
        ],
        "input": [
            "Fill Text", "Input Text", "Type Text", "Press Keys", 
            "Upload File", "Type Secret", "Clear Text"
        ],
        "interaction": [
            "Click", "Click Element", "Hover", "Drag And Drop",
            "Select Options By", "Check Checkbox", "Uncheck Checkbox"
        ],
        "extraction": [
            "Get Text", "Get Attribute", "Get Element Count", 
            "Get Property", "Get Style", "Get Url", "Get Title"
        ],
        "assertion": [
            "Should Be Equal", "Should Contain", "Should Be Visible",
            "Should Not Be Visible", "Should Be Enabled", "Should Be Disabled"
        ],
        "wait": [
            "Wait For Elements State", "Wait Until Element Is Visible",
            "Wait For Condition", "Wait For Load State", "Sleep"
        ]
    }
    
    def __init__(
        self, 
        model_name: str = "all-MiniLM-L6-v2",
        persist_directory: str = "./chroma_db"
    ):
        """
        Initialize with ChromaDB for semantic classification.
        
        Args:
            model_name: Name of sentence-transformers model to use
            persist_directory: Path to ChromaDB storage directory
        """
        logger.info(f"Initializing ContextPruner with ChromaDB at {persist_directory}")
        
        try:
            # Initialize ChromaDB client (same pattern as KeywordVectorStore)
            self.client = chromadb.PersistentClient(
                path=persist_directory,
                settings=Settings(
                    anonymized_telemetry=False,
                    allow_reset=True
                )
            )
            
            # Initialize embedding function
            self.embedding_function = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=model_name
            )
            
            # Create or get category collection
            self.collection = self.client.get_or_create_collection(
                name="category_descriptions",
                embedding_function=self.embedding_function,
                metadata={"type": "query_categories"}
            )
            
            # Initialize category descriptions in ChromaDB
            self._init_category_collection()
            
            logger.info("ContextPruner initialized successfully with ChromaDB")
            
        except Exception as e:
            logger.error(f"Failed to initialize ContextPruner: {e}")
            raise
    
    def _init_category_collection(self):
        """
        Initialize ChromaDB collection with category descriptions.
        
        Stores semantic representations of each category for fast
        similarity comparison during query classification.
        """
        logger.debug("Initializing category descriptions in ChromaDB")
        
        # Define category descriptions for semantic matching
        category_descriptions = {
            "navigation": "open browser navigate to website go to page url address",
            "input": "type text fill form input data enter information write",
            "interaction": "click button press element hover drag drop select",
            "extraction": "get text retrieve data extract information read content",
            "assertion": "verify check validate assert should be equal confirm",
            "wait": "wait for element visible ready loaded appear timeout"
        }
        
        # Check if collection is already populated
        existing_count = self.collection.count()
        if existing_count == len(category_descriptions):
            logger.debug(f"Category collection already populated with {existing_count} entries")
            return
        
        # Add category descriptions to ChromaDB
        try:
            ids = list(category_descriptions.keys())
            documents = list(category_descriptions.values())
            metadatas = [{"category": cat} for cat in ids]
            
            # Upsert to handle re-initialization
            self.collection.upsert(
                ids=ids,
                documents=documents,
                metadatas=metadatas
            )
            
            logger.info(f"Initialized {len(category_descriptions)} category descriptions in ChromaDB")
            
        except Exception as e:
            logger.error(f"Failed to initialize category collection: {e}")
            raise
    
    def classify_query(
        self, 
        user_query: str, 
        confidence_threshold: float = 0.8
    ) -> List[str]:
        """
        Classify query into action categories using ChromaDB semantic search.
        
        Queries the ChromaDB collection to find similar categories based on
        normalized cosine similarity. Returns categories that meet the 
        confidence threshold, or all categories if no category meets the 
        threshold (graceful degradation).
        
        Args:
            user_query: User's natural language query
            confidence_threshold: Minimum similarity for category inclusion (0.0-1.0)
            
        Returns:
            List of relevant category names (e.g., ["input", "interaction"])
            Returns all categories if confidence too low (fallback)
        """
        logger.debug(f"Classifying query: {user_query[:50]}...")
        
        try:
            # Query ChromaDB for similar categories
            # ChromaDB returns normalized cosine distance (0 = identical, 2 = opposite)
            # We need to convert to similarity: similarity = 1 - (distance / 2)
            results = self.collection.query(
                query_texts=[user_query],
                n_results=len(self.KEYWORD_CATEGORIES)
            )
            
            # Extract categories and convert distances to similarities
            similarities = {}
            if results['ids'] and len(results['ids'][0]) > 0:
                for idx, category_id in enumerate(results['ids'][0]):
                    distance = results['distances'][0][idx]
                    # Convert cosine distance to similarity (0-1 range)
                    # ChromaDB cosine distance range: [0, 2]
                    # Similarity = 1 - (distance / 2) gives us [0, 1] range
                    similarity = 1.0 - (distance / 2.0)
                    similarities[category_id] = similarity
                    logger.debug(f"Category '{category_id}': distance={distance:.4f}, similarity={similarity:.4f}")
            
            # Filter categories by confidence threshold
            relevant_categories = [
                cat for cat, sim in similarities.items() 
                if sim >= confidence_threshold
            ]
            
            if relevant_categories:
                logger.info(
                    f"Classified query into {len(relevant_categories)} categories: "
                    f"{relevant_categories} (threshold={confidence_threshold})"
                )
                return relevant_categories
            else:
                # Graceful degradation: return all categories if none meet threshold
                all_categories = list(self.KEYWORD_CATEGORIES.keys())
                logger.warning(
                    f"No categories met threshold {confidence_threshold}. "
                    f"Highest similarity: {max(similarities.values()):.4f}. "
                    f"Falling back to all categories."
                )
                return all_categories
                
        except Exception as e:
            logger.error(f"Classification failed: {e}. Falling back to all categories.")
            return list(self.KEYWORD_CATEGORIES.keys())
    
    def prune_keywords(
        self, 
        all_keywords: List[Dict], 
        categories: List[str]
    ) -> List[Dict]:
        """
        Filter keywords to only those in relevant categories.
        
        Args:
            all_keywords: List of keyword dicts with 'name' field
            categories: List of relevant category names
            
        Returns:
            Filtered list of keyword dicts
        """
        logger.debug(f"Pruning keywords for categories: {categories}")
        
        # Build set of relevant keyword names
        relevant_names = set()
        for category in categories:
            if category in self.KEYWORD_CATEGORIES:
                relevant_names.update(self.KEYWORD_CATEGORIES[category])
        
        # Filter keywords
        pruned = [
            kw for kw in all_keywords 
            if kw.get("name") in relevant_names
        ]
        
        logger.info(
            f"Pruned {len(all_keywords)} keywords to {len(pruned)} "
            f"({len(pruned)/len(all_keywords)*100:.1f}% retained)"
        )
        
        return pruned
    
    def get_pruning_stats(
        self, 
        original_count: int, 
        pruned_count: int
    ) -> Dict[str, float]:
        """
        Calculate pruning statistics.
        
        Args:
            original_count: Number of keywords before pruning
            pruned_count: Number of keywords after pruning
            
        Returns:
            Dict with original_count, pruned_count, retention_rate, reduction_rate, and reduction_percentage
        """
        if original_count == 0:
            return {
                "original_count": 0,
                "pruned_count": 0,
                "retention_rate": 0.0,
                "reduction_rate": 0.0,
                "reduction_percentage": 0.0
            }
        
        retention = pruned_count / original_count
        reduction = 1.0 - retention
        
        return {
            "original_count": original_count,
            "pruned_count": pruned_count,
            "retention_rate": retention,
            "reduction_rate": reduction,
            "reduction_percentage": reduction * 100
        }
