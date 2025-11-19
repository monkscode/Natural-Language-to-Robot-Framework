"""
Pattern Learning System for Query-Keyword Association

This module implements a pattern learning system that learns which keywords
are commonly used for specific query types and predicts relevant keywords
for new queries based on similarity to past queries.

Uses ChromaDB for semantic similarity search (efficient) and SQLite for usage statistics.
"""

import sqlite3
import json
import time
import logging
from datetime import datetime
from typing import List, Dict, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class QueryPatternMatcher:
    """
    Learn and predict keyword usage patterns using ChromaDB for embeddings and SQLite for statistics.
    Uses ChromaDB for semantic similarity search (efficient) and SQLite for usage tracking.
    """
    
    def __init__(self, db_path: str = "./data/pattern_learning.db", chroma_store=None):
        """
        Initialize with SQLite database path and ChromaDB store.
        
        Args:
            db_path: Path to SQLite database file (for usage statistics)
            chroma_store: KeywordVectorStore instance (for query embeddings)
        """
        self.db_path = db_path
        self.chroma_store = chroma_store
        
        # Ensure data directory exists
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        
        # Initialize database schema (SQLite for statistics only)
        self._init_database()
        
        # Get or create ChromaDB collection for query patterns
        if self.chroma_store:
            self.pattern_collection = self.chroma_store.get_or_create_pattern_collection()
        else:
            logger.warning("No ChromaDB store provided, pattern learning will be limited")
            self.pattern_collection = None
        
        logger.info(f"QueryPatternMatcher initialized with database: {db_path}")
    
    def _init_database(self):
        """Create database schema if it doesn't exist (SQLite for statistics only)."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Create keyword_stats table (usage tracking)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS keyword_stats (
                keyword_name TEXT PRIMARY KEY,
                usage_count INTEGER DEFAULT 1,
                last_used TEXT NOT NULL
            )
        """)
        
        # Create index for performance
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_keyword_stats_name 
            ON keyword_stats(keyword_name)
        """)
        
        conn.commit()
        conn.close()
        
        logger.info("Database schema initialized successfully")
    
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
            if in_test_case and test_case_name_next and not original_line.startswith('    '):
                # This is a test case name, skip it
                test_case_name_next = False
                continue
            
            # Extract keywords from test case lines (must be indented)
            if in_test_case and original_line.startswith('    ') and not line.startswith('['):
                # Split by multiple spaces (Robot Framework separator)
                parts = [p.strip() for p in line.split('    ') if p.strip()]
                
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
        Extract keywords from generated code and store pattern in ChromaDB + SQLite.
        
        Args:
            user_query: Original user query
            generated_code: Successfully generated Robot Framework code
        """
        try:
            # Extract keywords used in code
            used_keywords = self._extract_keywords_from_code(generated_code)
            
            if not used_keywords:
                logger.warning("No keywords extracted from code, skipping pattern learning")
                return
            
            timestamp = datetime.now().isoformat()
            
            # Store pattern in ChromaDB (for semantic search)
            if self.pattern_collection:
                pattern_id = f"pattern_{int(time.time() * 1000)}"
                self.pattern_collection.add(
                    documents=[user_query],
                    ids=[pattern_id],
                    metadatas=[{
                        "keywords": json.dumps(used_keywords),
                        "timestamp": timestamp
                    }]
                )
                logger.debug(f"Stored pattern in ChromaDB: {pattern_id}")
            
            # Update keyword statistics in SQLite
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            for keyword in used_keywords:
                cursor.execute("""
                    INSERT INTO keyword_stats (keyword_name, usage_count, last_used)
                    VALUES (?, 1, ?)
                    ON CONFLICT(keyword_name) DO UPDATE SET
                        usage_count = usage_count + 1,
                        last_used = ?
                """, (keyword, timestamp, timestamp))
            
            conn.commit()
            conn.close()
            
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
            
            # Search for similar patterns in ChromaDB
            results = self.pattern_collection.query(
                query_texts=[user_query],
                n_results=5  # Get top 5 similar patterns
            )
            
            # Check if we have results
            if not results['ids'][0]:
                logger.debug("No patterns in ChromaDB yet")
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

    
    def get_keyword_stats(self) -> Dict[str, Dict]:
        """
        Get statistics about keyword usage.
        
        Returns:
            Dictionary mapping keyword names to usage statistics
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT keyword_name, usage_count, last_used
                FROM keyword_stats
                ORDER BY usage_count DESC
            """)
            
            stats = {}
            for keyword_name, usage_count, last_used in cursor.fetchall():
                stats[keyword_name] = {
                    "usage_count": usage_count,
                    "last_used": last_used
                }
            
            conn.close()
            return stats
            
        except Exception as e:
            logger.error(f"Failed to get keyword stats: {e}", exc_info=True)
            return {}
    
    def get_pattern_count(self) -> int:
        """
        Get the number of patterns stored in ChromaDB.
        
        Returns:
            Number of patterns
        """
        try:
            if not self.pattern_collection:
                return 0
            
            # Get count from ChromaDB collection
            count = self.pattern_collection.count()
            return count
            
        except Exception as e:
            logger.error(f"Failed to get pattern count: {e}", exc_info=True)
            return 0
