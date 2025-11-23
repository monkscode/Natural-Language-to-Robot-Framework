"""SQL queries for pattern learning system."""

# Schema queries
CREATE_KEYWORD_STATS_TABLE = """
    CREATE TABLE IF NOT EXISTS keyword_stats (
        keyword_name TEXT PRIMARY KEY,
        usage_count INTEGER DEFAULT 1,
        last_used TEXT NOT NULL
    )
"""

CREATE_KEYWORD_STATS_INDEX = """
    CREATE INDEX IF NOT EXISTS idx_keyword_stats_name 
    ON keyword_stats(keyword_name)
"""

# Data manipulation queries
INSERT_OR_UPDATE_KEYWORD = """
    INSERT INTO keyword_stats (keyword_name, usage_count, last_used)
    VALUES (?, 1, ?)
    ON CONFLICT(keyword_name) DO UPDATE SET
        usage_count = usage_count + 1,
        last_used = ?
"""

SELECT_ALL_KEYWORD_STATS = """
    SELECT keyword_name, usage_count, last_used
    FROM keyword_stats
    ORDER BY usage_count DESC
"""

SELECT_KEYWORD_BY_NAME = """
    SELECT keyword_name, usage_count, last_used
    FROM keyword_stats
    WHERE keyword_name = ?
"""
