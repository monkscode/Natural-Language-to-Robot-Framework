"""
Context Pruner for Smart Keyword Filtering

This module classifies user queries into action categories and prunes
keyword context to include only relevant keywords, reducing token usage
while maintaining code generation accuracy.
"""

import logging
import numpy as np
from typing import List, Dict
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)


class ContextPruner:
    """
    Classify queries and prune context to relevant keyword categories.
    
    Uses semantic similarity to classify queries into action categories
    (navigation, input, interaction, extraction, assertion, wait) and
    filters keywords to only those in relevant categories.
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
    
    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        """
        Initialize with sentence transformer model for classification.
        
        Args:
            model_name: Name of sentence-transformers model to use
        """
        logger.info(f"Initializing ContextPruner with model: {model_name}")
        self.model = SentenceTransformer(model_name)
        self._init_category_embeddings()
        logger.info("ContextPruner initialized successfully")
    
    def _init_category_embeddings(self):
        """
        Pre-compute embeddings for category descriptions.
        
        Creates semantic representations of each category for fast
        similarity comparison during query classification.
        """
        logger.debug("Pre-computing category embeddings")
        
        # Define category descriptions for semantic matching
        category_descriptions = {
            "navigation": "open browser navigate to website go to page url address",
            "input": "type text fill form input data enter information write",
            "interaction": "click button press element hover drag drop select",
            "extraction": "get text retrieve data extract information read content",
            "assertion": "verify check validate assert should be equal confirm",
            "wait": "wait for element visible ready loaded appear timeout"
        }
        
        # Pre-compute embeddings for all categories
        self.category_embeddings = {}
        for category, description in category_descriptions.items():
            embedding = self.model.encode([description])[0]
            self.category_embeddings[category] = embedding
            logger.debug(f"Category '{category}' embedding shape: {embedding.shape}")
        
        logger.info(f"Pre-computed embeddings for {len(self.category_embeddings)} categories")
    
    def classify_query(self, user_query: str, confidence_threshold: float = 0.8) -> List[str]:
        """
        Classify query into action categories using semantic similarity.
        
        Computes similarity between the query and each category description.
        Returns categories that meet the confidence threshold, or all categories
        if no category meets the threshold (graceful degradation).
        
        Args:
            user_query: User's natural language query
            confidence_threshold: Minimum similarity for category inclusion (0.0-1.0)
            
        Returns:
            List of relevant category names (e.g., ["input", "interaction"])
            Returns all categories if confidence too low (fallback)
        """
        logger.debug(f"Classifying query: {user_query[:50]}...")
        
        # Encode query
        query_embedding = self.model.encode([user_query])[0]
        
        # Compute similarity with each category
        similarities = {}
        for category, category_embedding in self.category_embeddings.items():
            # Cosine similarity using dot product (embeddings are normalized)
            similarity = np.dot(query_embedding, category_embedding)
            similarities[category] = similarity
            logger.debug(f"Category '{category}' similarity: {similarity:.3f}")
        
        # Get categories above threshold
        relevant_categories = [
            category for category, similarity in similarities.items()
            if similarity >= confidence_threshold
        ]
        
        # If no categories meet threshold, return all (fallback)
        if not relevant_categories:
            logger.warning(
                f"No categories met threshold {confidence_threshold}, "
                f"max similarity: {max(similarities.values()):.3f}. "
                "Falling back to all categories."
            )
            return list(self.KEYWORD_CATEGORIES.keys())
        
        logger.info(
            f"Query classified into {len(relevant_categories)} categories: "
            f"{', '.join(relevant_categories)}"
        )
        return relevant_categories
    
    def prune_keywords(self, all_keywords: List[Dict], categories: List[str]) -> List[Dict]:
        """
        Filter keywords to only those in relevant categories.
        
        Args:
            all_keywords: All available keywords (list of dicts with 'name' key)
            categories: Relevant categories from classification
            
        Returns:
            Filtered list of keywords matching the categories
        """
        logger.debug(f"Pruning keywords for categories: {', '.join(categories)}")
        
        # Collect all keyword names from relevant categories
        relevant_keyword_names = set()
        for category in categories:
            category_keywords = self.KEYWORD_CATEGORIES.get(category, [])
            relevant_keyword_names.update(category_keywords)
            logger.debug(f"Category '{category}': {len(category_keywords)} keywords")
        
        # Filter keywords
        pruned_keywords = [
            kw for kw in all_keywords
            if kw.get('name') in relevant_keyword_names
        ]
        
        logger.info(
            f"Pruned keywords: {len(all_keywords)} -> {len(pruned_keywords)} "
            f"({len(pruned_keywords)/len(all_keywords)*100:.1f}% retained)"
        )
        
        return pruned_keywords
    
    def get_pruning_stats(self, original_count: int, pruned_count: int) -> Dict[str, float]:
        """
        Calculate pruning statistics.
        
        Args:
            original_count: Number of keywords before pruning
            pruned_count: Number of keywords after pruning
            
        Returns:
            Dictionary with pruning statistics
        """
        if original_count == 0:
            return {
                "original_count": 0,
                "pruned_count": 0,
                "reduction_percentage": 0.0,
                "retention_percentage": 0.0
            }
        
        reduction_percentage = ((original_count - pruned_count) / original_count) * 100
        retention_percentage = (pruned_count / original_count) * 100
        
        return {
            "original_count": original_count,
            "pruned_count": pruned_count,
            "reduction_percentage": reduction_percentage,
            "retention_percentage": retention_percentage
        }
