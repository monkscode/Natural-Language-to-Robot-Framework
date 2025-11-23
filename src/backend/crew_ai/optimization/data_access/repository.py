"""Repository pattern for database operations."""

import sqlite3
import logging
from typing import Dict, Tuple, Optional, Any
from pathlib import Path
from .queries import (
    CREATE_KEYWORD_STATS_TABLE,
    CREATE_KEYWORD_STATS_INDEX,
    INSERT_OR_UPDATE_KEYWORD,
    SELECT_ALL_KEYWORD_STATS,
    SELECT_KEYWORD_BY_NAME,
)

logger = logging.getLogger(__name__)


class KeywordStatsRepository:
    """Repository for keyword statistics database operations."""
    
    def __init__(self, db_path: str):
        """
        Initialize repository with database path.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
    
    def initialize_schema(self) -> None:
        """Create database schema if it doesn't exist."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(CREATE_KEYWORD_STATS_TABLE)
            cursor.execute(CREATE_KEYWORD_STATS_INDEX)
            conn.commit()
        
        logger.info("Database schema initialized successfully")
    
    def upsert_keyword(self, keyword: str, timestamp: str) -> None:
        """
        Insert or update keyword statistics.
        
        Args:
            keyword: Keyword name to insert/update
            timestamp: ISO timestamp of the operation
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(INSERT_OR_UPDATE_KEYWORD, (keyword, timestamp, timestamp))
            conn.commit()
    
    def get_all_stats(self) -> Dict[str, Dict[str, Any]]:
        """
        Get all keyword statistics.
        
        Returns:
            Dictionary mapping keyword names to usage statistics
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(SELECT_ALL_KEYWORD_STATS)
            
            stats = {}
            for keyword_name, usage_count, last_used in cursor.fetchall():
                stats[keyword_name] = {
                    "usage_count": usage_count,
                    "last_used": last_used
                }
            return stats
    
    def get_keyword_stats(self, keyword: str) -> Optional[Tuple[str, int, str]]:
        """
        Get statistics for a specific keyword.
        
        Args:
            keyword: Keyword name to query
            
        Returns:
            Tuple of (keyword_name, usage_count, last_used) or None if not found
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(SELECT_KEYWORD_BY_NAME, (keyword,))
            return cursor.fetchone()
