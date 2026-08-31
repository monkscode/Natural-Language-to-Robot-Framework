"""
Pattern Learning System for Query-Keyword Association

This module implements a pattern learning system that learns which keywords
are commonly used for specific query types and predicts relevant keywords
for new queries based on similarity to past queries.

Uses pgvector (via KeywordVectorStore) for semantic similarity search — stores
(user_query → keywords) patterns from every successful execution and queries
them at generation time. Distance is L2; similarity = 1/(1+distance).
"""

import re
import logging
from typing import List

logger = logging.getLogger(__name__)


class QueryPatternMatcher:
    """
    Learn and predict keyword usage patterns using pgvector semantic similarity.

    Stores (user_query → keywords) patterns from successful executions and
    queries them at generation time to predict relevant keywords for new queries.
    """

    def __init__(self, chroma_store=None):
        """
        Initialize with the KeywordVectorStore for query-pattern embeddings.

        Args:
            chroma_store: KeywordVectorStore instance (pgvector-backed). The
                parameter name is kept for call-site compatibility.
        """
        self.chroma_store = chroma_store
        if not self.chroma_store:
            logger.warning("No vector store provided, pattern learning will be limited")
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

    def learn_from_execution(self, user_query: str, generated_code: str,
                             org_id: str | None = None):
        """
        Extract keywords from generated code and store pattern in ChromaDB.

        Args:
            user_query: Original user query
            generated_code: Successfully generated Robot Framework code (passed tests only)
            org_id: Organisation that owns this pattern (None = unscoped / legacy)
        """
        try:
            used_keywords = self._extract_keywords_from_code(generated_code)

            if not used_keywords:
                logger.warning("No keywords extracted from code, skipping pattern learning")
                return

            if self.chroma_store:
                pattern_id = self.chroma_store.add_pattern(
                    user_query, used_keywords, org_id=org_id)
                if pattern_id:
                    logger.debug(f"Stored query pattern: {pattern_id}")
                    logger.info(f"Learned pattern: query='{user_query[:50]}...', keywords={used_keywords}")

        except Exception as e:
            logger.error(f"Failed to learn from execution: {e}", exc_info=True)

    def get_relevant_keywords(self, user_query: str, confidence_threshold: float = 0.7,
                              org_id: str | None = None) -> List[str]:
        """
        Predict relevant keywords based on similar past queries using ChromaDB.

        Args:
            user_query: New user query
            confidence_threshold: Minimum similarity score (0.0-1.0)
            org_id: Organisation scope for the pattern search (None = unscoped / legacy)

        Returns:
            List of predicted keyword names (empty if confidence too low)
        """
        try:
            if not self.chroma_store:
                logger.debug("No vector store available")
                return []

            count = self.chroma_store.pattern_count()
            if count == 0:
                logger.debug("No query patterns yet")
                return []

            results = self.chroma_store.search_patterns(
                user_query, top_k=min(5, count), org_id=org_id)
            if not results:
                return []

            # Confidence from the nearest pattern (L2 distance, lower is better):
            # similarity = 1 / (1 + distance).
            top_distance = results[0]["distance"]
            similarity = 1 / (1 + top_distance)

            if similarity < confidence_threshold:
                logger.debug(f"Top similarity {similarity:.3f} below threshold {confidence_threshold}")
                return []

            # Aggregate keywords from patterns above threshold.
            keyword_counts = {}
            for res in results:
                result_similarity = 1 / (1 + res["distance"])
                if result_similarity >= confidence_threshold:
                    for keyword in (res["keywords"] or []):
                        keyword_counts[keyword] = keyword_counts.get(keyword, 0) + 1

            sorted_keywords = sorted(keyword_counts.items(), key=lambda x: x[1], reverse=True)
            predicted_keywords = [kw for kw, _ in sorted_keywords[:10]]

            logger.info(f"Predicted {len(predicted_keywords)} keywords with confidence {similarity:.3f}")
            logger.debug(f"Predicted keywords: {predicted_keywords}")
            return predicted_keywords

        except Exception as e:
            logger.error(f"Failed to predict keywords: {e}", exc_info=True)
            return []


