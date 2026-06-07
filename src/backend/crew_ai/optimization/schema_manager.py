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
    # 4: deliberately skipped during development — never released to any DB
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
    6: {
        # Case B re-run support + LLM-trigger conflict flagging columns.
        # - execution_records.working_code: stores the corrected passing
        #   robot code when a previously-failed workflow re-runs and passes.
        #   The original failing robot_code is preserved unchanged so failure
        #   context (failure_category, error_message, failed_keyword) stays
        #   intact for `process_user_feedback`.
        # - nl_feedback_corrections.conflict_flagged + metadata: Trigger 1
        #   and Trigger 2 LLM conflict detection (Step 5) suspend hints from
        #   injection without permanently deactivating them. is_active=0
        #   stays the sole automated path to permanent deactivation,
        #   reachable only via apply_hint_attribution (never-succeeded /
        #   never-used auto-disable).
        #
        # ALTER TABLE ADD COLUMN does not support IF NOT EXISTS in SQLite;
        # the schema-version gate guarantees each ALTER runs once per DB.
        "description": "Case B fix — working_code column + conflict_flagged for safe LLM hint suspension",
        "sql": [
            "ALTER TABLE execution_records ADD COLUMN working_code TEXT",
            "ALTER TABLE nl_feedback_corrections ADD COLUMN conflict_flagged INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE nl_feedback_corrections ADD COLUMN conflict_flagged_at TEXT",
            "ALTER TABLE nl_feedback_corrections ADD COLUMN conflict_flag_reason TEXT",
            "CREATE INDEX IF NOT EXISTS idx_nlfc_conflict ON nl_feedback_corrections(conflict_flagged)",
        ],
    },
    7: {
        # Trigger telemetry — observability for Trigger 1 / Trigger 2 LLM calls.
        # One row per trigger fire (or per "no active hints in scope" early
        # return — those rows have status='no_active_hints' and zero tokens
        # so cost dashboards reflect no charge). Failure to write a row is
        # absorbed by LearningWriteQueue's per-item try/except — telemetry
        # NEVER blocks the pipeline.
        #
        # Phase 2 stats SQL (LLM-accuracy KPI, cost) reads this table directly.
        # Column / index shapes are locked against those queries:
        # - flagged_hint_ids stored as JSON text array ("[13,42]"), expanded
        #   via json_each in the engagement/reversal queries.
        # - status is the discriminator the success-rate / timeout-rate queries
        #   group on.
        # - idx_created_at supports the 30-day window predicate.
        # - idx_workflow supports the per-workflow drilldown on the Trigger
        #   1 detail page.
        # - idx_type supports GROUP BY trigger_type aggregates.
        "description": "trigger_events audit log for Trigger 1 + Trigger 2 LLM conflict detection",
        "sql": [
            """
            CREATE TABLE IF NOT EXISTS trigger_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_type TEXT NOT NULL,
                workflow_id TEXT,
                domain TEXT,
                url TEXT,
                feedback_text TEXT,
                active_hint_ids TEXT,
                flagged_hint_ids TEXT,
                reason TEXT,
                llm_model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                llm_latency_ms INTEGER,
                status TEXT NOT NULL,
                error_message TEXT,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_trigger_events_created ON trigger_events(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_trigger_events_workflow ON trigger_events(workflow_id)",
            "CREATE INDEX IF NOT EXISTS idx_trigger_events_type ON trigger_events(trigger_type)",
        ],
    },
    8: {
        # Phase 2 admin curation support.
        # - nl_feedback_corrections.created_via: distinguishes hints submitted
        #   via the workflow /api/feedback path ('workflow', the default for all
        #   existing rows) from hints added directly by an admin via the Phase 2
        #   dashboard ('admin'). Affects hint inventory display and stats SQL;
        #   does NOT affect retrieval ordering or get_hints() filtering.
        # - hint_audit: append-only manual-action log. Every unflag/retract/
        #   reactivate/scope-change/category-change/create action writes one row.
        #   Also receives implicit 'unflag' rows from the UPSERT path in
        #   learn_from_feedback when a user re-submits identical feedback that
        #   was previously LLM-flagged (Gap 7 refinement). The Phase 2 LLM-
        #   accuracy KPI (reversal rate) counts both explicit and implicit
        #   unflags via this table.
        #
        # ADD COLUMN does not support IF NOT EXISTS in SQLite; the schema-
        # version gate guarantees this ALTER runs exactly once per database.
        "description": "Phase 2 — admin curation: hint_audit table + created_via column",
        "sql": [
            "ALTER TABLE nl_feedback_corrections ADD COLUMN created_via TEXT NOT NULL DEFAULT 'workflow'",
            """CREATE TABLE IF NOT EXISTS hint_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hint_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                reason TEXT,
                before_value TEXT,
                after_value TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(hint_id) REFERENCES nl_feedback_corrections(id)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_hint_audit_hint ON hint_audit(hint_id)",
            "CREATE INDEX IF NOT EXISTS idx_hint_audit_created ON hint_audit(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_hint_audit_actor ON hint_audit(actor)",
        ],
    },
    9: {
        # LLM admin hint review system.
        # - nl_feedback_corrections.disabled_at: exact timestamp when a hint was
        #   deactivated (auto-disable or admin-disable). NULL = never disabled.
        #   Used by the LLM review fetch to include recently-disabled hints (30d)
        #   without relying on last_seen (ambiguous). ADD COLUMN runs once per DB
        #   via the schema-version gate.
        # - hint_review_sessions: one row per admin-triggered LLM review cycle.
        # - hint_review_recommendations: individual LLM decisions within a session.
        "description": "LLM admin hint review — disabled_at column + hint_review_sessions + hint_review_recommendations",
        "sql": [
            "ALTER TABLE nl_feedback_corrections ADD COLUMN disabled_at TEXT",
            """CREATE TABLE IF NOT EXISTS hint_review_sessions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                status          TEXT NOT NULL DEFAULT 'pending_llm',
                hint_count      INTEGER NOT NULL DEFAULT 0,
                llm_model       TEXT,
                llm_latency_ms  INTEGER,
                warning         TEXT,
                error_message   TEXT,
                created_at      TEXT NOT NULL,
                completed_at    TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS hint_review_recommendations (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id       INTEGER NOT NULL REFERENCES hint_review_sessions(id),
                hint_id          INTEGER NOT NULL REFERENCES nl_feedback_corrections(id),
                recommendation   TEXT NOT NULL,
                reason           TEXT NOT NULL,
                exoneration_count INTEGER NOT NULL DEFAULT 0,
                admin_decision   TEXT,
                admin_notes      TEXT,
                decided_at       TEXT,
                applied          INTEGER NOT NULL DEFAULT 0,
                created_at       TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_hrr_session ON hint_review_recommendations(session_id)",
            "CREATE INDEX IF NOT EXISTS idx_hrr_hint ON hint_review_recommendations(hint_id)",
        ],
    },
    10: {
        # Learning-prompt hardening (docs/LEARNING_PROMPTS_ANALYSIS.md §7 Q2, §12).
        # - execution_records.injected_hint_ids: JSON array of the hint IDs
        #   SmartKeywordProvider actually injected into the agents when this
        #   workflow's v1 code was generated. Trigger 1 / Trigger 2 read this
        #   instead of get_active_hints_raw() so the LLM only judges hints that
        #   could have shaped v1 (resolves analysis C1/C2). Placed on
        #   execution_records (not learning_metrics) because workflow_id is
        #   UNIQUE here — both trigger paths already load this single row, and
        #   the Case-B re-run path preserves it unchanged (correct v1 set).
        #   Value semantics: '[]' = known-empty (no hints shaped v1 → Trigger
        #   has nothing to judge); NULL = legacy / unknown → Trigger 1/2 skip
        #   the LLM call entirely (preservation is safer than judging against
        #   an unknown set). ADD COLUMN does not support IF NOT EXISTS in
        #   SQLite; the schema-version gate guarantees this ALTER runs once
        #   per DB.
        # - hint_review_pages: per-chunk status for paginated P3 hint review
        #   (§12). One row per chunk (globals chunk + one per domain) within a
        #   hint_review_sessions row. Separate table (not JSON on the session)
        #   so "which scopes failed?" is plain SQL and a future "re-run one
        #   failed chunk" needs no schema change.
        "description": "Learning-prompt hardening — injected_hint_ids column + hint_review_pages table",
        "sql": [
            "ALTER TABLE execution_records ADD COLUMN injected_hint_ids TEXT",
            """CREATE TABLE IF NOT EXISTS hint_review_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                scope_type TEXT NOT NULL,
                scope_value TEXT,
                status TEXT NOT NULL,
                hint_count INTEGER NOT NULL,
                llm_latency_ms INTEGER,
                error_message TEXT,
                retry_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY (session_id) REFERENCES hint_review_sessions(id)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_review_pages_session ON hint_review_pages(session_id)",
        ],
    },
    11: {
        # Query-similarity hint filtering + measurement instrumentation
        # (docs/QUERY_SIMILARITY_HINT_INJECTION_PLAN.md).
        # - nl_feedback_corrections.anchor_query: the natural-language user
        #   query that produced the hint (or the admin-typed anchor for
        #   admin-created hints). The similarity filter embeds this and keeps
        #   a hint only when it is semantically close to the current request.
        #   SQL-authoritative; the learning_anchors ChromaDB collection is a
        #   rebuildable index over it.
        # - execution_records.model_version: "{provider}/{model}" stamped at
        #   write time. A reporting dimension only (never a retrieval filter)
        #   so pass rate / cost / lift can be sliced by model.
        # - learning_metrics.was_holdout: 1 when the R7 random-holdout
        #   suppressed otherwise-available hints for this run, enabling an
        #   unbiased lift measurement (Cat B - Cat C).
        #
        # The UPDATE backfills anchor_query for rows that pre-date this
        # migration; it MUST follow the ADD COLUMN above — statements run in
        # list order and an UPDATE before its column fails "no such column".
        # execution_records.workflow_id is UNIQUE so the subquery is
        # single-valued; a row whose source_workflow_id does not resolve
        # stays NULL (fail-closed, harmless). The migration is atomic via the
        # schema-version gate's transaction — all four statements commit
        # together or roll back together.
        #
        # ADD COLUMN does not support IF NOT EXISTS in SQLite; the schema-
        # version gate guarantees these ALTERs run exactly once per database.
        "description": "Query-similarity anchors — anchor_query/model_version/was_holdout columns + anchor_query backfill",
        "sql": [
            "ALTER TABLE nl_feedback_corrections ADD COLUMN anchor_query TEXT",
            "ALTER TABLE execution_records ADD COLUMN model_version TEXT",
            "ALTER TABLE learning_metrics ADD COLUMN was_holdout INTEGER NOT NULL DEFAULT 0",
            """
            UPDATE nl_feedback_corrections
            SET anchor_query = (
                SELECT user_query FROM execution_records
                WHERE execution_records.workflow_id
                      = nl_feedback_corrections.source_workflow_id
            )
            WHERE anchor_query IS NULL
            """,
        ],
    },
    12: {
        # Split the dual-meaning `flagged_hint_ids` column on trigger_events.
        # Until v12, that column was simultaneously asked to mean:
        #   1. "What the LLM recommended to flag" — read by the weekly hint
        #      review's _compute_exonerations to score per-hint LLM judgment
        #      frequency.
        #   2. "What was actually flagged" — used by the engagement/reversal
        #      KPIs to measure whether humans followed up on flag actions.
        # Those two meanings diverge whenever the strong-history protection
        # guard in conflict_flag_hints suppresses an LLM recommendation: the
        # row stayed unflagged in nl_feedback_corrections but still appeared
        # in flagged_hint_ids, inflating the engagement-rate denominator with
        # events no admin could ever review.
        #
        # `actually_flagged_hint_ids` is the post-guard enforcement set,
        # written by the same task that updates conflict_flagged. Existing
        # rows have NULL here — KPI queries fall back via COALESCE so legacy
        # data preserves its pre-v12 (potentially over-counted) behavior, and
        # the 30-day rolling window ages it out cleanly.
        "description": "Split trigger_events.flagged_hint_ids into recommendation vs enforcement",
        "sql": [
            "ALTER TABLE trigger_events ADD COLUMN actually_flagged_hint_ids TEXT",
        ],
    },
    13: {
        # Usage-aware hint attribution (Part 2). Three additive columns plus a
        # one-time counter reset (FR1):
        #  - nl_feedback_corrections.unused_count: the "injected but not used
        #    here" axis (applied = success + failure + unused). DEFAULT 0.
        #  - execution_records.hint_attribution_done: the per-workflow once-guard
        #    for pass-time attribution. DEFAULT 1 (NOT 0) with NO backfill —
        #    SQLite materialises the constant for existing rows in O(1), so every
        #    pre-v13 row reads 1 ("already attributed under the old Step-7
        #    credit"), preventing a cross-deploy re-run from double-crediting.
        #    New rows are inserted with an explicit 0 by _store_sqlite so they
        #    attribute normally; apply_hint_attribution's atomic claim flips the
        #    winner to 1. DEFAULT 1 is the safe fail-direction (a forgotten write
        #    reads "done" → at worst a missed credit, never a double-credit).
        #  - trigger_events.used_hint_ids / unused_hint_ids: attribution
        #    telemetry alongside the existing flagged_hint_ids columns.
        #
        # FR1 — reset every NL hint's applied/success/failure to 0. The legacy
        # counters credited ALL injected hints (the exact bug Part 2 fixes), so
        # they are untrustworthy; resetting starts each hint on clean, used-only
        # data so the new success/(success+failure) rules judge accurate numbers
        # from day one. unused_count already defaults 0. evidence_count,
        # anchor_query, conflict_flagged/_at/_reason, is_active, created_at, and
        # last_seen are deliberately left untouched. The UPDATE MUST follow the
        # ADD COLUMNs (statements run in list order); the whole migration is
        # atomic via the schema-version gate's transaction.
        "description": "Usage attribution — unused_count/hint_attribution_done/trigger_events telemetry + reset NL counters (FR1)",
        "sql": [
            "ALTER TABLE nl_feedback_corrections ADD COLUMN unused_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE execution_records ADD COLUMN hint_attribution_done INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE trigger_events ADD COLUMN used_hint_ids TEXT",
            "ALTER TABLE trigger_events ADD COLUMN unused_hint_ids TEXT",
            """
            UPDATE nl_feedback_corrections
            SET applied_count = 0,
                success_count = 0,
                failure_count = 0
            """,
        ],
    },
    14: {
        # N3 observability — per-(workflow, hint) selection→attribution trace.
        # ONE table `hint_workflow_trace` keyed (workflow_id, hint_id) with NO
        # foreign key (R-N3): a deduped run has no execution_records row, so the
        # trace must stand alone; the per-workflow dashboard view LEFT JOINs
        # execution_records for run-context. Trace scope is NL-centric — only NL
        # hints carry ids, so per-hint rows are NL (source='nl'); the `source`
        # column is kept generic for future per-engine extension.
        #
        # Columns:
        #  - scope/source/priority: hint identity carried from selection.
        #  - similarity_score: cosine sim from filter_by_query_similarity; NULL
        #    for fail_open/no_anchor and for non-similarity-filtered sources.
        #  - available: passed similarity + dedup + the NL internal cap (reached
        #    the candidate pool). injected: survived the per-agent budget cap
        #    (in nl_injected_ids). Both default 0; written explicitly per row.
        #  - drop_reason: free TEXT (enum documented, NOT a CHECK, so new values
        #    need no migration) ∈ {similarity_below, no_anchor, dedup, cap,
        #    holdout}; NULL when injected.
        #  - attribution_bucket ∈ {used, harmful, unused, unsure} /
        #    attribution_reason: filled later by apply_hint_attribution as a
        #    SEPARATE commit after the counter transaction (NULL until then;
        #    stays NULL on a deduped/holdout run where attribution is skipped).
        #  - created_at: ISO string, indexed for the G3 90-day opportunistic
        #    prune on the writer thread (observability-only — pruning never
        #    touches the learning counters, which live on the hint row).
        #
        # CREATE TABLE/INDEX IF NOT EXISTS (matches the v5 pattern); the
        # schema-version gate already guarantees a single application.
        "description": "Hint workflow trace — per-(workflow,hint) selection/attribution observability (N3)",
        "sql": [
            """
            CREATE TABLE IF NOT EXISTS hint_workflow_trace (
                workflow_id TEXT NOT NULL,
                hint_id INTEGER NOT NULL,
                scope TEXT,
                source TEXT,
                priority TEXT,
                similarity_score REAL,
                available INTEGER NOT NULL DEFAULT 0,
                injected INTEGER NOT NULL DEFAULT 0,
                drop_reason TEXT,
                attribution_bucket TEXT,
                attribution_reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (workflow_id, hint_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_hint_trace_created_at "
            "ON hint_workflow_trace(created_at)",
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
