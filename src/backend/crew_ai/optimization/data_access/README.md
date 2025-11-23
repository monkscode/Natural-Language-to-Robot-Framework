# Data Access Layer Documentation

## Overview

This directory contains the data access layer (DAL) for the pattern learning system. It separates SQL queries from business logic following the repository pattern.

## Architecture

```
data_access/
├── __init__.py      # Module exports
├── queries.py       # SQL query constants
└── repository.py    # Database operations
```

## Components

### `queries.py`
Contains all SQL query constants used by the repository:

- `CREATE_KEYWORD_STATS_TABLE` - Creates the keyword_stats table
- `CREATE_KEYWORD_STATS_INDEX` - Creates index for performance
- `INSERT_OR_UPDATE_KEYWORD` - Upserts keyword statistics
- `SELECT_ALL_KEYWORD_STATS` - Retrieves all keyword stats
- `SELECT_KEYWORD_BY_NAME` - Retrieves specific keyword stats

### `repository.py`
Implements the `KeywordStatsRepository` class with methods:

- `initialize_schema()` - Creates database schema
- `upsert_keyword(keyword, timestamp)` - Inserts or updates keyword
- `get_all_stats()` - Gets all keyword statistics
- `get_keyword_stats(keyword)` - Gets specific keyword statistics

## Usage

```python
from crew_ai.optimization.data_access import KeywordStatsRepository

# Initialize repository
repo = KeywordStatsRepository(db_path="./data/pattern_learning.db")
repo.initialize_schema()

# Insert/update keyword
repo.upsert_keyword("Click Element", "2025-01-01T00:00:00")

# Get all statistics
stats = repo.get_all_stats()
# Returns: {"Click Element": {"usage_count": 1, "last_used": "2025-01-01T00:00:00"}}

# Get specific keyword stats
keyword_info = repo.get_keyword_stats("Click Element")
# Returns: ("Click Element", 1, "2025-01-01T00:00:00")
```

## Benefits

1. **Separation of Concerns**: SQL is separated from business logic
2. **Testability**: Easy to mock repository for unit tests
3. **Maintainability**: All SQL queries in one place
4. **Reusability**: Queries can be shared across modules
5. **Migration Ready**: Easy to switch to SQLAlchemy or other ORMs

## Database Schema

### `keyword_stats` Table

| Column       | Type    | Constraints    | Description                    |
|-------------|---------|----------------|--------------------------------|
| keyword_name | TEXT    | PRIMARY KEY    | Name of the Robot Framework keyword |
| usage_count  | INTEGER | DEFAULT 1      | Number of times keyword used   |
| last_used    | TEXT    | NOT NULL       | ISO timestamp of last use      |

**Index**: `idx_keyword_stats_name` on `keyword_name` for fast lookups.

## Future Enhancements

- Add connection pooling
- Implement query caching
- Add query builder for complex queries
- Migrate to SQLAlchemy ORM
- Add database migration system (Alembic)
