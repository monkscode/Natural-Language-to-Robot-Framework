"""
Schema Manager — Versioned database schema evolution.

Handles automatic migration of the execution_memory.db schema across phases.
Opening the database automatically applies pending migrations — zero manual steps.

How it works:
1. Check current schema version from 'schema_version' table
2. Apply all migrations from current_version+1 to latest
3. Each migration is wrapped in a transaction for atomicity

Future extensibility:
- Adding Phase 5 = add entry to SCHEMA_MIGRATIONS dict
- Rollback = not supported (schema additions are additive, never destructive)
- Version is monotonically increasing, never decremented

Referenced by: execution_memory.py, scripts/migrate_pattern_learning.py
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema Migrations — one entry per phase
# ---------------------------------------------------------------------------

SCHEMA_MIGRATIONS = {
    1: {
        "description": "Phase 1 Foundation — Core tables",
        "sql": [
            # --- Table 1: execution_records ---
            """
            CREATE TABLE execution_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT UNIQUE NOT NULL,
                timestamp TEXT NOT NULL,

                -- Input
                user_query TEXT NOT NULL,
                url TEXT,
                domain TEXT,

                -- Code Generation
                robot_code TEXT,
                code_structure TEXT,

                -- Execution Result
                test_status TEXT NOT NULL,
                execution_exit_code INTEGER,
                execution_duration_ms INTEGER,

                -- Failure Details
                failure_category TEXT,
                failed_keyword TEXT,
                error_message TEXT,

                -- Cost
                total_llm_calls INTEGER DEFAULT 0,
                total_cost REAL DEFAULT 0.0,

                -- User Feedback (populated later via /api/feedback)
                user_feedback TEXT,
                user_feedback_type TEXT,

                CONSTRAINT chk_status CHECK (test_status IN ('passed', 'failed', 'error'))
            )
            """,
            "CREATE INDEX idx_exec_domain ON execution_records(domain)",
            "CREATE INDEX idx_exec_status ON execution_records(test_status)",
            "CREATE INDEX idx_exec_failure ON execution_records(failure_category)",
            "CREATE INDEX idx_exec_timestamp ON execution_records(timestamp)",
            "CREATE INDEX idx_exec_structure ON execution_records(code_structure)",

            # --- Table 2: intent_patterns ---
            """
            CREATE TABLE intent_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_name TEXT UNIQUE NOT NULL,
                triggers_json TEXT NOT NULL,
                requires_json TEXT NOT NULL,
                source TEXT NOT NULL,
                score REAL DEFAULT 0.5,
                evidence_count INTEGER DEFAULT 0,
                counter_evidence INTEGER DEFAULT 0,
                last_triggered TEXT,
                last_updated TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_intent_score ON intent_patterns(score DESC)",
            "CREATE INDEX idx_intent_source ON intent_patterns(source)",

            # --- Table 3: structural_rules ---
            """
            CREATE TABLE structural_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_name TEXT UNIQUE NOT NULL,
                query_pattern TEXT NOT NULL,
                required_structure TEXT NOT NULL,
                required_keywords_json TEXT,
                code_template TEXT,
                score REAL DEFAULT 0.5,
                evidence_count INTEGER DEFAULT 0,
                counter_evidence INTEGER DEFAULT 0,
                last_triggered TEXT,
                last_updated TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_rules_score ON structural_rules(score DESC)",

            # --- Table 4: keyword_corrections ---
            """
            CREATE TABLE keyword_corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wrong_keyword TEXT NOT NULL,
                correct_keyword TEXT,
                library TEXT DEFAULT 'browser',
                error_pattern TEXT,
                score REAL DEFAULT 0.5,
                evidence_count INTEGER DEFAULT 1,
                last_seen TEXT NOT NULL,
                UNIQUE(wrong_keyword, library)
            )
            """,

            # --- Table 5: anti_patterns ---
            """
            CREATE TABLE anti_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                failure_category TEXT NOT NULL,
                query_pattern TEXT,
                bad_code_snippet TEXT,
                error_message TEXT,
                correct_alternative TEXT,
                domain TEXT,
                score REAL DEFAULT 0.5,
                evidence_count INTEGER DEFAULT 1,
                last_seen TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_anti_category ON anti_patterns(failure_category)",
            "CREATE INDEX idx_anti_domain ON anti_patterns(domain)",

            # --- Table 6: learning_stats ---
            """
            CREATE TABLE learning_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stat_date TEXT NOT NULL,
                total_executions INTEGER DEFAULT 0,
                total_passed INTEGER DEFAULT 0,
                total_failed INTEGER DEFAULT 0,
                rules_applied INTEGER DEFAULT 0,
                rules_effective INTEGER DEFAULT 0,
                avg_cost REAL,
                avg_delegation_count REAL,
                feedback_received INTEGER DEFAULT 0,
                UNIQUE(stat_date)
            )
            """,

            # --- Table 7: learning_metrics ---
            """
            CREATE TABLE learning_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT NOT NULL,
                user_query TEXT NOT NULL,
                is_first_attempt INTEGER NOT NULL DEFAULT 1,
                is_retry_after_feedback INTEGER NOT NULL DEFAULT 0,
                attempt_number INTEGER NOT NULL DEFAULT 1,
                hints_available INTEGER NOT NULL DEFAULT 0,
                hints_injected INTEGER NOT NULL DEFAULT 0,
                hint_sources TEXT DEFAULT '[]',
                llm_calls INTEGER NOT NULL DEFAULT 0,
                llm_cost REAL NOT NULL DEFAULT 0.0,
                hint_tokens INTEGER NOT NULL DEFAULT 0,
                test_passed INTEGER NOT NULL,
                timestamp TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_metrics_workflow ON learning_metrics(workflow_id)",
            "CREATE INDEX idx_metrics_first_attempt ON learning_metrics(is_first_attempt)",
            "CREATE INDEX idx_metrics_hints ON learning_metrics(hints_injected)",
        ],
    },
    2: {
        "description": "NL Feedback Corrections — User feedback hint storage",
        "sql": [
            # --- Table 8: nl_feedback_corrections ---
            """
            CREATE TABLE nl_feedback_corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feedback_text TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'uncategorized',
                scope TEXT NOT NULL DEFAULT 'domain',
                domain TEXT,
                url TEXT,
                original_failure_category TEXT,
                evidence_count INTEGER NOT NULL DEFAULT 1,
                applied_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                source_workflow_id TEXT,
                created_at TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                UNIQUE(feedback_text, domain, scope)
            )
            """,
            "CREATE INDEX idx_nlfc_domain ON nl_feedback_corrections(domain)",
            "CREATE INDEX idx_nlfc_scope ON nl_feedback_corrections(scope)",
            "CREATE INDEX idx_nlfc_active ON nl_feedback_corrections(is_active)",
        ],
    },
    3: {
        "description": "Performance indexes for hint injection queries",
        "sql": [
            # Composite index for anti_patterns hint retrieval
            # (used by _find_matching_anti_patterns WHERE score >= ? AND evidence_count >= ?)
            "CREATE INDEX IF NOT EXISTS idx_anti_score_evidence "
            "ON anti_patterns(score DESC, evidence_count DESC)",

            # Composite index for nl_feedback_corrections hint retrieval
            # (used by get_hints WHERE is_active = 1 AND (scope/domain/url))
            "CREATE INDEX IF NOT EXISTS idx_nlfc_domain_scope "
            "ON nl_feedback_corrections(domain, scope)",

            # Index for structural_rules evidence gating
            "CREATE INDEX IF NOT EXISTS idx_rules_score_evidence "
            "ON structural_rules(score DESC, evidence_count DESC)",
        ],
    },
    4: {
        "description": "Consolidate keyword_stats from pattern_learning.db",
        "sql": [
            """
            CREATE TABLE IF NOT EXISTS keyword_stats (
                keyword_name TEXT PRIMARY KEY,
                usage_count INTEGER DEFAULT 1,
                last_used TEXT NOT NULL
            )
            """,
        ],
    },
    5: {
        # Remediation: learning_metrics was added to v1's SQL list after v1
        # had already been applied to existing databases.  SchemaManager
        # skipped v1 (already at version >= 1), so the table was never
        # created.  This migration uses IF NOT EXISTS so it is safe for
        # both fresh databases (where v1 already created the table) and
        # existing databases (where the table is missing).
        "description": "Create learning_metrics table (remediation for v1 gap)",
        "sql": [
            """
            CREATE TABLE IF NOT EXISTS learning_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT NOT NULL,
                user_query TEXT NOT NULL,
                is_first_attempt INTEGER NOT NULL DEFAULT 1,
                is_retry_after_feedback INTEGER NOT NULL DEFAULT 0,
                attempt_number INTEGER NOT NULL DEFAULT 1,
                hints_available INTEGER NOT NULL DEFAULT 0,
                hints_injected INTEGER NOT NULL DEFAULT 0,
                hint_sources TEXT DEFAULT '[]',
                llm_calls INTEGER NOT NULL DEFAULT 0,
                llm_cost REAL NOT NULL DEFAULT 0.0,
                hint_tokens INTEGER NOT NULL DEFAULT 0,
                test_passed INTEGER NOT NULL,
                timestamp TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_metrics_workflow ON learning_metrics(workflow_id)",
            "CREATE INDEX IF NOT EXISTS idx_metrics_first_attempt ON learning_metrics(is_first_attempt)",
            "CREATE INDEX IF NOT EXISTS idx_metrics_hints ON learning_metrics(hints_injected)",
        ],
    },
}


# ---------------------------------------------------------------------------
# SchemaManager
# ---------------------------------------------------------------------------

class SchemaManager:
    """
    Auto-migrates database schema on connection open.

    Called once during ExecutionMemory._ensure_schema().
    Idempotent — safe to call repeatedly.
    """

    VERSION_TABLE_SQL = """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            description TEXT,
            applied_at TEXT NOT NULL
        )
    """

    @staticmethod
    def ensure_current(conn) -> int:
        """
        Apply pending migrations. Returns current version.

        Args:
            conn: SQLite connection object (must have WAL + busy_timeout set)

        Returns:
            Current schema version number after all migrations applied
        """
        conn.execute(SchemaManager.VERSION_TABLE_SQL)
        conn.commit()

        current = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_version"
        ).fetchone()[0]

        applied_count = 0
        for version in sorted(SCHEMA_MIGRATIONS.keys()):
            if version > current:
                migration = SCHEMA_MIGRATIONS[version]
                try:
                    for sql in migration["sql"]:
                        conn.execute(sql)
                    conn.execute(
                        "INSERT INTO schema_version "
                        "(version, description, applied_at) "
                        "VALUES (?, ?, ?)",
                        (
                            version,
                            migration["description"],
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                    conn.commit()
                    applied_count += 1
                    logger.info(
                        f"[LEARNING] Schema migrated to v{version}: "
                        f"{migration['description']}"
                    )
                except Exception as e:
                    conn.rollback()
                    logger.error(
                        f"[LEARNING] Schema migration to v{version} FAILED: {e}"
                    )
                    raise

        final_version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_version"
        ).fetchone()[0]

        if applied_count == 0:
            logger.debug(
                f"[LEARNING] Schema already at v{final_version} — no migrations needed"
            )

        return final_version

    @staticmethod
    def get_current_version(conn) -> int:
        """Get current schema version without applying migrations."""
        try:
            conn.execute(SchemaManager.VERSION_TABLE_SQL)
            return conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_version"
            ).fetchone()[0]
        except Exception:
            return 0

    @staticmethod
    def get_table_names(conn) -> list:
        """Get all user table names (excludes sqlite internals)."""
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        return [row[0] for row in rows]

    @staticmethod
    def get_index_names(conn) -> list:
        """Get all user index names."""
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        return [row[0] for row in rows]
