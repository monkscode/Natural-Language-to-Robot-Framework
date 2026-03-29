"""
Execution Memory — Single source of truth for all learning data.

Records every workflow execution as a structured, queryable record in SQLite
with optional ChromaDB semantic search for finding similar past executions.

Storage strategy:
- SQLite: Structured queries, aggregation, pattern mining
  (e.g., "Show me all A1 failures on demoqa.com")
- ChromaDB: Semantic search over user_query
  (e.g., "Find executions similar to 'filter table and verify rows'")

Threading model:
- ExecutionMemory performs direct writes — callers are responsible for
  submitting via LearningWriteQueue for concurrent workloads.
- SQLite connection uses WAL mode + busy_timeout=5000 for safe concurrent reads.

Referenced by: All learning engines (DAY_02 through DAY_21).
Depends on: schema_manager.py (DAY_00), learning_config.py (DAY_00).
"""

import sqlite3
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict
from src.backend.crew_ai.optimization.schema_manager import SchemaManager
from src.backend.crew_ai.optimization.learning_config import (
    LEARNING_CONFIG,
    ExecutionStore,
    SemanticStore,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ExecutionRecord Dataclass
# ---------------------------------------------------------------------------

@dataclass
class ExecutionRecord:
    """
    Complete record of one workflow execution. Phase 1 fields only.

    Fields are grouped by purpose:
    - Identity: workflow_id, timestamp
    - Input: user_query, url, domain
    - Code Generation: robot_code, code_structure
    - Execution Result: test_status, exit_code, duration
    - Failure Details: category, failed keyword, error message
    - Cost: LLM call count and total cost
    - User Feedback: text and type (populated later via /api/feedback)
    """

    # Identity
    workflow_id: str                          # UUID
    timestamp: datetime                       # When executed

    # Input
    user_query: str                           # Original NL query
    url: Optional[str] = None                 # Target website URL
    domain: Optional[str] = None              # Extracted from URL (e.g., "demoqa.com")

    # Code Generation
    robot_code: Optional[str] = None          # Final .robot file content
    code_structure: Optional[str] = None      # "linear" | "for_loop" | "conditional" | "mixed"

    # Execution Result
    test_status: str = "error"                # "passed" | "failed" | "error"
    execution_exit_code: Optional[int] = None
    execution_duration_ms: Optional[int] = None

    # Failure Details
    failure_category: Optional[str] = None    # From taxonomy (A1, B2, C1, etc.)
    failed_keyword: Optional[str] = None
    error_message: Optional[str] = None

    # Cost
    total_llm_calls: int = 0
    total_cost: float = 0.0

    # User Feedback (populated later)
    user_feedback: Optional[str] = None
    user_feedback_type: Optional[str] = None  # "close_enough" | "completely_wrong"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------



class CodeStructureExtractor:
    """
    Analyzes generated robot code to determine its structure type.

    Used to populate the code_structure field in ExecutionRecord.
    Detection uses line-level parsing -- checks only indented keyword
    lines to avoid false positives from comments and string content.
    """

    @staticmethod
    def detect(robot_code: str) -> str:
        """
        Detect code structure from Robot Framework code.

        Returns: "linear" | "for_loop" | "conditional" | "mixed"
        """
        if not robot_code:
            return "linear"

        has_for = False
        has_condition = False

        for line in robot_code.split("\n"):
            stripped = line.strip()
            # Skip empty lines, comments, and section headers
            if not stripped or stripped.startswith("#") or stripped.startswith("*"):
                continue

            # Check for RF control structures as keyword calls (first token)
            first_token = stripped.split()[0] if stripped.split() else ""
            if first_token == "FOR":
                has_for = True
            elif first_token == "IF":
                has_condition = True
            elif stripped.startswith("Run Keyword If"):
                has_condition = True

        if has_for and has_condition:
            return "mixed"
        elif has_for:
            return "for_loop"
        elif has_condition:
            return "conditional"
        return "linear"


# ---------------------------------------------------------------------------
# ExecutionMemory — Main Storage Class
# ---------------------------------------------------------------------------

class ExecutionMemory(ExecutionStore, SemanticStore):
    """
    SQLite + ChromaDB storage for execution records.

    Implements both ExecutionStore (structured queries) and SemanticStore
    (semantic search) interfaces from the repository pattern.

    Key design decisions:
    - SQLite initialized eagerly (fast, ~5ms)
    - ChromaDB initialized lazily (loads 80MB model on first use)
    - Deduplication after 5 identical runs (same query + domain + status)
    - All operations wrapped in try/except — never blocks the pipeline
    """

    DEDUPLICATION_THRESHOLD = LEARNING_CONFIG["DEDUPLICATION_THRESHOLD"]

    # Sentinel for failed ChromaDB initialization. Using a dedicated
    # object (not a magic string like "FAILED") ensures that any code
    # checking `self._chroma_client is not None` won't mistakenly
    # treat the sentinel as a valid client.
    _CHROMADB_INIT_FAILED = object()

    def __init__(self, db_path: str = None, chroma_dir: str = None):
        """
        Initialize ExecutionMemory with SQLite (eager) and ChromaDB (lazy).

        Args:
            db_path: Path to SQLite database. Defaults to LEARNING_CONFIG value.
            chroma_dir: Path to ChromaDB directory. Defaults to LEARNING_CONFIG value.
        """
        self.db_path = db_path or LEARNING_CONFIG["EXECUTION_MEMORY_DB"]
        self._chroma_dir = chroma_dir or LEARNING_CONFIG["CHROMADB_DIR"]

        # SQLite: initialized eagerly (fast)
        self._init_sqlite()

        # ChromaDB: initialized lazily (model loading is slow)
        self._chroma_client = None
        self._execution_collection = None

    def _init_sqlite(self):
        """Connect to SQLite with WAL mode and named-column access.

        Thread safety model:
        - check_same_thread=False allows this conn to be used across threads.
        - ALL writes go through LearningWriteQueue (single writer thread),
          eliminating write-write races.
        - WAL mode allows concurrent reads during writes (snapshot isolation).
        - For 5-20 concurrent users this is safe. Beyond that, consider
          per-thread connections via get_read_connection().
        """
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._ensure_schema()
        logger.debug("[LEARNING] SQLite connection initialized: %s", self.db_path)

    def get_read_connection(self) -> sqlite3.Connection:
        """Create an independent read-only connection for thread-safe reads.

        Use this when reading from a thread that may overlap with writes.
        Caller is responsible for closing the returned connection.
        """
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA query_only=ON")
        return conn

    def _ensure_schema(self):
        """Apply pending schema migrations using SchemaManager (from DAY_00)."""
        SchemaManager.ensure_current(self.conn)

    def _init_chromadb(self):
        """
        Initialize ChromaDB execution_embeddings collection.

        Called lazily on first semantic operation to avoid loading the
        80MB embedding model at application startup.
        """
        if self._chroma_client is not None:
            return

        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings
            self._chroma_client = chromadb.PersistentClient(
                path=self._chroma_dir,
                settings=ChromaSettings(anonymized_telemetry=False),
            )
            self._execution_collection = self._chroma_client.get_or_create_collection(
                name="execution_embeddings",
                metadata={"hnsw:space": "cosine"},
            )
            logger.info(
                "[LEARNING] ChromaDB initialized: %s (collection: execution_embeddings)",
                self._chroma_dir,
            )
        except Exception as e:
            logger.error("[LEARNING] ChromaDB initialization failed: %s", e)
            # Mark as failed so we don't retry on every call
            self._chroma_client = self._CHROMADB_INIT_FAILED
            self._execution_collection = None

    @property
    def _chromadb_available(self) -> bool:
        """Check if ChromaDB was successfully initialized."""
        return (
            self._chroma_client is not None
            and self._chroma_client is not self._CHROMADB_INIT_FAILED
            and self._execution_collection is not None
        )

    # -------------------------------------------------------------------
    # Core Write Operations
    # -------------------------------------------------------------------

    def store(self, record: ExecutionRecord) -> None:
        """
        Store execution record in SQLite and ChromaDB.

        This is the primary write method called after every workflow execution.
        ChromaDB storage is best-effort — failures don't block SQLite storage.
        """
        self._store_sqlite(record)
        self._store_chromadb(record)

    # ExecutionStore ABC implementation
    def store_execution(self, record: ExecutionRecord) -> None:
        """ExecutionStore interface — delegates to store()."""
        self.store(record)

    def _store_sqlite(self, record: ExecutionRecord) -> None:
        """
        INSERT execution record into SQLite with deduplication.

        Deduplication rule:
        - "Identical" = same normalized_query AND same domain AND same test_status
        - normalized_query = user_query.strip().lower()
        - After DEDUPLICATION_THRESHOLD (5) identical runs, update the most
          recent record's counts instead of inserting a new row.
        - The most recent robot_code and error_message are kept.

        Uses BEGIN IMMEDIATE to ensure the count-check + write is atomic,
        preventing race conditions if the write queue is bypassed.
        """
        normalized_query = record.user_query.strip().lower()
        domain = record.domain or ""

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # Step 1: Count existing identical runs (within transaction)
            run_count = self.conn.execute(
                "SELECT COUNT(*) FROM execution_records "
                "WHERE LOWER(TRIM(user_query)) = ? AND domain = ? AND test_status = ?",
                (normalized_query, domain, record.test_status),
            ).fetchone()[0]

            if run_count >= self.DEDUPLICATION_THRESHOLD:
                # Step 2: Aggregate — update the most recent matching record
                self.conn.execute(
                    """
                    UPDATE execution_records
                    SET robot_code = ?, error_message = ?, timestamp = ?,
                        total_llm_calls = total_llm_calls + ?,
                        total_cost = total_cost + ?
                    WHERE id = (
                        SELECT id FROM execution_records
                        WHERE LOWER(TRIM(user_query)) = ? AND domain = ? AND test_status = ?
                        ORDER BY timestamp DESC LIMIT 1
                    )
                    """,
                    (
                        record.robot_code, record.error_message,
                        record.timestamp.isoformat(),
                        record.total_llm_calls, record.total_cost,
                        normalized_query, domain, record.test_status,
                    ),
                )
                logger.debug(
                    "[LEARNING] Deduplicated execution record (run #%d): %s",
                    run_count + 1, record.workflow_id,
                )
            else:
                # Normal insert
                self.conn.execute(
                    """
                    INSERT INTO execution_records (
                        workflow_id, timestamp, user_query, url, domain,
                        robot_code, code_structure, test_status, execution_exit_code,
                        execution_duration_ms, failure_category, failed_keyword,
                        error_message, total_llm_calls, total_cost
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.workflow_id, record.timestamp.isoformat(),
                        record.user_query, record.url, record.domain,
                        record.robot_code, record.code_structure, record.test_status,
                        record.execution_exit_code, record.execution_duration_ms,
                        record.failure_category, record.failed_keyword,
                        record.error_message, record.total_llm_calls, record.total_cost,
                    ),
                )

            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _store_chromadb(self, record: ExecutionRecord) -> None:
        """
        Store execution embedding in ChromaDB for semantic search.

        Best-effort: failures are logged but don't block the pipeline.
        Triggers lazy initialization on first call.
        """
        self._init_chromadb()
        if not self._chromadb_available:
            return

        try:
            self._execution_collection.add(
                documents=[record.user_query],
                ids=[record.workflow_id],
                metadatas=[{
                    "test_status": record.test_status,
                    "failure_category": record.failure_category or "",
                    "domain": record.domain or "",
                    "code_structure": record.code_structure or "",
                    "workflow_id": record.workflow_id,
                }],
            )
        except Exception as e:
            logger.warning("[LEARNING] ChromaDB store failed (non-blocking): %s", e)

    # -------------------------------------------------------------------
    # Read Operations
    # -------------------------------------------------------------------

    def get(self, workflow_id: str) -> Optional[ExecutionRecord]:
        """Retrieve a single execution record by workflow_id."""
        row = self.conn.execute(
            "SELECT * FROM execution_records WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if row:
            return self._row_to_record(row)
        return None

    def query_by_domain(self, domain: str, limit: int = 50) -> List[ExecutionRecord]:
        """Get recent executions for a specific domain."""
        rows = self.conn.execute(
            "SELECT * FROM execution_records WHERE domain = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (domain, limit),
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def query_failures(
        self,
        category: Optional[str] = None,
        domain: Optional[str] = None,
        limit: int = 50,
    ) -> List[ExecutionRecord]:
        """Get failed executions, optionally filtered by category and/or domain."""
        sql = "SELECT * FROM execution_records WHERE test_status = 'failed'"
        params: list = []

        if category:
            sql += " AND failure_category = ?"
            params.append(category)
        if domain:
            sql += " AND domain = ?"
            params.append(domain)

        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_record(row) for row in rows]

    def find_similar_executions(
        self, user_query: str, top_k: int = 5
    ) -> List[dict]:
        """
        Semantic search over past executions using ChromaDB.

        Triggers lazy ChromaDB initialization on first call.

        Returns:
            List of result dicts from ChromaDB query, or empty list if
            ChromaDB is unavailable.
        """
        self._init_chromadb()
        if not self._chromadb_available:
            return []

        try:
            results = self._execution_collection.query(
                query_texts=[user_query],
                n_results=top_k,
            )
            return results
        except Exception as e:
            logger.warning("[LEARNING] ChromaDB search failed: %s", e)
            return []

    # SemanticStore ABC implementation
    def store_embedding(self, text: str, metadata: dict) -> None:
        """SemanticStore interface — store text with metadata."""
        self._init_chromadb()
        if not self._chromadb_available:
            return

        try:
            doc_id = metadata.get("workflow_id", str(id(text)))
            self._execution_collection.add(
                documents=[text],
                ids=[doc_id],
                metadatas=[metadata],
            )
        except Exception as e:
            logger.warning("[LEARNING] ChromaDB store_embedding failed: %s", e)

    def search_similar(self, query: str, top_k: int = 5) -> list:
        """SemanticStore interface — delegates to find_similar_executions."""
        return self.find_similar_executions(query, top_k)

    # -------------------------------------------------------------------
    # Update Operations
    # -------------------------------------------------------------------

    def update_user_feedback(
        self, workflow_id: str, feedback_text: str, feedback_type: str
    ) -> None:
        """Update execution record with user's NL feedback."""
        self.conn.execute(
            "UPDATE execution_records SET user_feedback = ?, user_feedback_type = ? "
            "WHERE workflow_id = ?",
            (feedback_text, feedback_type, workflow_id),
        )
        self.conn.commit()

    # -------------------------------------------------------------------
    # Aggregation / Stats
    # -------------------------------------------------------------------

    def get_domain_stats(self, domain: str) -> dict:
        """Get aggregated stats for a domain."""
        row = self.conn.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN test_status = 'passed' THEN 1 ELSE 0 END) as passed,
                SUM(CASE WHEN test_status = 'failed' THEN 1 ELSE 0 END) as failed,
                AVG(execution_duration_ms) as avg_duration,
                AVG(total_cost) as avg_cost
            FROM execution_records WHERE domain = ?
            """,
            (domain,),
        ).fetchone()

        if row and row["total"] > 0:
            return {
                "total": row["total"],
                "passed": row["passed"],
                "failed": row["failed"],
                "avg_duration_ms": row["avg_duration"],
                "avg_cost": row["avg_cost"],
                "pass_rate": row["passed"] / row["total"],
            }
        return {"total": 0, "passed": 0, "failed": 0,
                "avg_duration_ms": None, "avg_cost": None, "pass_rate": 0}

    def get_total_records(self) -> int:
        """Get total number of stored executions."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM execution_records"
        ).fetchone()[0]

    def update_daily_stats(self, test_status: str) -> None:
        """Update learning_stats table with daily aggregates.

        Uses INSERT ... ON CONFLICT to atomically create or update the
        row for today's date.  Called by FeedbackLoop after every execution.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        self.conn.execute(
            """
            INSERT INTO learning_stats
                (stat_date, total_executions, total_passed, total_failed)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(stat_date) DO UPDATE SET
                total_executions = total_executions + 1,
                total_passed = total_passed + excluded.total_passed,
                total_failed = total_failed + excluded.total_failed
            """,
            (
                today,
                1 if test_status == "passed" else 0,
                1 if test_status != "passed" else 0,
            ),
        )
        self.conn.commit()

    # -------------------------------------------------------------------
    # Internal Helpers
    # -------------------------------------------------------------------

    def _row_to_record(self, row: sqlite3.Row) -> ExecutionRecord:
        """
        Convert SQLite Row to ExecutionRecord dataclass.

        Uses named columns via sqlite3.Row factory — adding new columns in
        later phases requires no changes here.
        """
        return ExecutionRecord(
            workflow_id=row["workflow_id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            user_query=row["user_query"],
            url=row["url"],
            domain=row["domain"],
            robot_code=row["robot_code"],
            code_structure=row["code_structure"],
            test_status=row["test_status"],
            execution_exit_code=row["execution_exit_code"],
            execution_duration_ms=row["execution_duration_ms"],
            failure_category=row["failure_category"],
            failed_keyword=row["failed_keyword"],
            error_message=row["error_message"],
            total_llm_calls=row["total_llm_calls"] or 0,
            total_cost=row["total_cost"] or 0.0,
            user_feedback=row["user_feedback"],
            user_feedback_type=row["user_feedback_type"],
        )

    def close(self):
        """Close database connections."""
        if self.conn:
            self.conn.close()
            logger.debug("[LEARNING] SQLite connection closed")
