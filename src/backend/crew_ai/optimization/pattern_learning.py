"""
Pattern Learning System for Query-Keyword Association

This module implements a pattern learning system that learns which keywords
are commonly used for specific query types and predicts relevant keywords
for new queries based on similarity to past queries.

Uses ChromaDB for semantic similarity search — stores (user_query → keywords) patterns
from every successful execution and queries them at generation time.
"""

import re
import json
import uuid
import logging
from datetime import datetime
from typing import List

logger = logging.getLogger(__name__)


class QueryPatternMatcher:
    """
    Learn and predict keyword usage patterns using ChromaDB for semantic similarity.

    Stores (user_query → keywords) patterns from successful executions and
    queries them at generation time to predict relevant keywords for new queries.
    """

    def __init__(self, chroma_store=None):
        """
        Initialize with ChromaDB store for query pattern embeddings.

        Args:
            chroma_store: KeywordVectorStore instance (for query embeddings)
        """
        self.chroma_store = chroma_store

        # Get or create ChromaDB collection for query patterns
        if self.chroma_store:
            self.pattern_collection = self.chroma_store.get_or_create_pattern_collection()
        else:
            logger.warning("No ChromaDB store provided, pattern learning will be limited")
            self.pattern_collection = None

        logger.info("QueryPatternMatcher initialized")

    def _extract_keywords_from_code(self, code: str) -> List[str]:
        """
        Extract Robot Framework keywords from generated code.

        Args:
            code: Generated Robot Framework code

        Returns:
            List of unique keyword names used in code
        """
        keywords = set()

        # Parse code line by line
        in_test_case = False
        test_case_name_next = False

        for line in code.split('\n'):
            original_line = line
            line = line.strip()

            # Skip empty lines and comments
            if not line or line.startswith('#'):
                continue

            # Check if we're in test case section
            if line.startswith('*** Test Cases ***'):
                in_test_case = True
                test_case_name_next = True
                continue

            # Skip section headers
            if line.startswith('***'):
                in_test_case = False
                test_case_name_next = False
                continue

            # Skip test case names (they appear right after *** Test Cases ***)
            # Test case names start at column 0; keyword lines are indented
            if in_test_case and test_case_name_next and not original_line[0:1].isspace():
                # This is a test case name, skip it
                test_case_name_next = False
                continue

            # Extract keywords from test case lines (must be indented with 2+ spaces)
            if in_test_case and original_line[0:1].isspace() and not line.startswith('['):
                # Split by 2+ spaces (Robot Framework separator convention)
                parts = [p.strip() for p in re.split(r'\s{2,}', line) if p.strip()]

                if parts:
                    # First part might be a variable assignment
                    first_part = parts[0]

                    # Check if it's a variable assignment (${var}= or ${var} =)
                    if '=' in first_part and first_part.strip().startswith('${'):
                        # Keyword is the second part
                        if len(parts) > 1:
                            keyword = parts[1]
                            if not keyword.startswith('${') and not keyword.startswith('@{'):
                                keywords.add(keyword)
                    else:
                        # First part is the keyword
                        if not first_part.startswith('${') and not first_part.startswith('@{'):
                            keywords.add(first_part)

        logger.debug(f"Extracted {len(keywords)} keywords from code: {keywords}")
        return list(keywords)

    def learn_from_execution(self, user_query: str, generated_code: str):
        """
        Extract keywords from generated code and store pattern in ChromaDB.

        Args:
            user_query: Original user query
            generated_code: Successfully generated Robot Framework code (passed tests only)
        """
        try:
            used_keywords = self._extract_keywords_from_code(generated_code)

            if not used_keywords:
                logger.warning("No keywords extracted from code, skipping pattern learning")
                return

            if self.pattern_collection:
                pattern_id = f"pattern_{uuid.uuid4().hex}"
                try:
                    self.pattern_collection.add(
                        documents=[user_query],
                        ids=[pattern_id],
                        metadatas=[{
                            "keywords": json.dumps(used_keywords),
                            "timestamp": datetime.now().isoformat()
                        }]
                    )
                    logger.debug(f"Stored pattern in ChromaDB: {pattern_id}")
                except Exception as chroma_e:
                    logger.warning(f"Failed to store pattern in ChromaDB: {chroma_e}")

            logger.info(f"Learned pattern: query='{user_query[:50]}...', keywords={used_keywords}")

        except Exception as e:
            logger.error(f"Failed to learn from execution: {e}", exc_info=True)

    def get_relevant_keywords(self, user_query: str, confidence_threshold: float = 0.7) -> List[str]:
        """
        Predict relevant keywords based on similar past queries using ChromaDB.

        Args:
            user_query: New user query
            confidence_threshold: Minimum similarity score (0.0-1.0)

        Returns:
            List of predicted keyword names (empty if confidence too low)
        """
        try:
            if not self.pattern_collection:
                logger.debug("No ChromaDB pattern collection available")
                return []

            # Guard: ChromaDB raises if n_results > collection size
            count = self.pattern_collection.count()
            if count == 0:
                logger.debug("No patterns in ChromaDB yet")
                return []

            results = self.pattern_collection.query(
                query_texts=[user_query],
                n_results=min(5, count)
            )

            if not results['ids'][0]:
                return []

            # Check confidence (ChromaDB returns distances, lower is better)
            # Convert distance to similarity: similarity = 1 / (1 + distance)
            top_distance = results['distances'][0][0]
            similarity = 1 / (1 + top_distance)

            if similarity < confidence_threshold:
                logger.debug(f"Top similarity {similarity:.3f} below threshold {confidence_threshold}")
                return []

            # Aggregate keywords from similar patterns
            keyword_counts = {}
            for i, metadata in enumerate(results['metadatas'][0]):
                # Get distance for this result
                distance = results['distances'][0][i]
                result_similarity = 1 / (1 + distance)

                # Only use results above threshold
                if result_similarity >= confidence_threshold:
                    keywords = json.loads(metadata['keywords'])
                    for keyword in keywords:
                        keyword_counts[keyword] = keyword_counts.get(keyword, 0) + 1

            # Return top 10 most common keywords
            sorted_keywords = sorted(keyword_counts.items(), key=lambda x: x[1], reverse=True)
            predicted_keywords = [kw for kw, count in sorted_keywords[:10]]

            logger.info(f"Predicted {len(predicted_keywords)} keywords with confidence {similarity:.3f}")
            logger.debug(f"Predicted keywords: {predicted_keywords}")

            return predicted_keywords

        except Exception as e:
            logger.error(f"Failed to predict keywords: {e}", exc_info=True)
            return []


