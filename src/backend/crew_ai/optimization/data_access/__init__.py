"""Data access layer for pattern learning system."""

from .queries import (
    CREATE_KEYWORD_STATS_TABLE,
    CREATE_KEYWORD_STATS_INDEX,
    INSERT_OR_UPDATE_KEYWORD,
    SELECT_ALL_KEYWORD_STATS,
    SELECT_KEYWORD_BY_NAME,
)
from .repository import KeywordStatsRepository

__all__ = [
    "CREATE_KEYWORD_STATS_TABLE",
    "CREATE_KEYWORD_STATS_INDEX",
    "INSERT_OR_UPDATE_KEYWORD",
    "SELECT_ALL_KEYWORD_STATS",
    "SELECT_KEYWORD_BY_NAME",
    "KeywordStatsRepository",
]
