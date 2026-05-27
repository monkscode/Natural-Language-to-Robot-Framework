"""
Execution Memory — Single source of truth for all learning data.

Records every workflow execution as a structured, queryable record in SQLite
with optional ChromaDB semantic search for finding similar past executions.

Storage strategy:
- SQLite: Structured queries, aggregation, pattern mining
  (e.g., "Show me all A1 failures on demoqa.com")
- ChromaDB: Semantic search over user_query
  (e.g., "Find executions similar to 'filter table and verify rows'")

Threading model (Q2/D1):
- ExecutionMemory owns ONE writer connection (_writer_conn) used only by
  the learning-writer thread (LearningWriteQueue worker). Every write method
  asserts threading.current_thread().name == WRITER_THREAD_NAME so any code
  path that bypasses the queue fails loudly.
- All reads go through read_conn(): a context manager that opens a fresh,
  short-lived query_only connection per call. Safe to call from FastAPI
  request handlers, the workflow background thread, the writer thread itself,
  or test threads.
- SQLite WAL mode (database-level, persistent) gives each read conn a
  consistent snapshot at open time. busy_timeout=5000 serializes cross-process
  contention. foreign_keys=ON enforces referential integrity on both paths.

Referenced by: All learning engines (DAY_02 through DAY_21).
Depends on: schema_manager.py (DAY_00), learning_config.py (DAY_00).
"""

import sqlite3
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict
from src.backend.crew_ai.optimization.schema_manager import SchemaManager
from src.backend.crew_ai.optimization.learning_config import (
    LEARNING_CONFIG,
    WRITER_THREAD_NAME,
    ExecutionStore,
    SemanticStore,
)


def _assert_writer_thread(method_name: str) -> None:
    """Guard for engine/ExecutionMemory write methods.

    Writes must run on the LearningWriteQueue worker thread so they serialize
    on the single _writer_conn. Tests can opt in by setting the calling thread
    name to WRITER_THREAD_NAME (see tests/test_optimization/conftest.py).
    """
    current = threading.current_thread().name
    if current != WRITER_THREAD_NAME:
        raise AssertionError(
            f"{method_name} must be called via LearningWriteQueue.submit(); "
            f"called from thread {current!r} instead."
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

    # Case B: corrected passing code (set when a re-run of a previously-failed
    # workflow passes). Original robot_code is preserved unchanged so failure
    # context (failure_category, error_message, failed_keyword) stays intact.
    # NULL for: (1) workflows that passed on first try, (2) workflows that
    # only ever failed, (3) all rows inserted before schema v6.
    working_code: Optional[str] = None

    # JSON array of NL feedback hint IDs that were injected into this test's
    # prompt (e.g. '[5, 12]'). '[]' = optimization enabled but no NL hints
    # injected. NULL = row written before schema v10 (unknown, not empty).
    # Written once on INSERT; never overwritten by the dedup UPDATE or Case B.
    injected_hint_ids: Optional[str] = None

    # "{provider}/{model}" of the LLM that generated this workflow's code
    # (e.g. "gemini/gemini-2.5-flash"). A reporting dimension only — sliced
    # in metrics, never used to filter hint retrieval. NULL for rows written
    # before schema v11. Refreshed on the dedup UPDATE (a current-state field,
    # kept latest alongside robot_code/timestamp) — NOT write-once. The Case B
    # passing-state UPDATE leaves it untouched (that path preserves the
    # original failed run's context).
    model_version: Optional[str] = None


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

    # Seconds to wait before retrying ChromaDB after a failed init.
    # Class-level so tests can patch it without sleeping.
    _CHROMA_RETRY_COOLDOWN_S: int = 300

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
        self._learning_anchors = None
        self._chroma_failed_at: float | None = None
        self._chroma_last_error: str | None = None
        # Serializes lazy _init_chromadb() across the writer thread and the
        # crew/request threads so the 80MB model is loaded exactly once.
        self._chroma_init_lock = threading.Lock()

    def _init_sqlite(self):
        """Connect SQLite writer conn (owned by learning-writer thread).

        check_same_thread=False is required because the conn is created on
        the main/init thread, then handed off to the learning-writer thread
        for all subsequent writes. After init, only the learning-writer
        thread touches _writer_conn — the thread-name assertion in every
        write method enforces this.

        WAL is set once at the database level (persists across all conns).
        foreign_keys/busy_timeout are connection-level and re-applied to
        every read conn opened via read_conn().
        """
        self._writer_conn = sqlite3.connect(
            self.db_path, check_same_thread=False,
        )
        self._writer_conn.row_factory = sqlite3.Row
        self._writer_conn.execute("PRAGMA journal_mode=WAL")
        self._writer_conn.execute("PRAGMA busy_timeout=5000")
        self._writer_conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()
        logger.debug("[LEARNING] SQLite writer conn initialized: %s", self.db_path)

    def get_read_connection(self) -> sqlite3.Connection:
        """Open a fresh, query_only read connection.

        PRAGMA order matters: foreign_keys + busy_timeout are configuration
        pragmas (no write involved); query_only is set LAST because once on,
        further pragma writes would fail.

        Caller owns the returned conn and is responsible for closing it.
        Prefer the read_conn() context manager for automatic cleanup.
        """
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA query_only=ON")
        return conn

    @contextmanager
    def read_conn(self):
        """Context manager yielding a fresh read-only connection.

        Use this in every engine read method instead of holding a long-lived
        connection. SQLite open is ~2 ms; the overhead is negligible against
        workflow runtime and the per-call isolation eliminates cross-thread
        cursor contention.
        """
        conn = self.get_read_connection()
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception as e:
                logger.debug("[LEARNING] read_conn close failed: %s", e)

    def _ensure_schema(self):
        """Apply pending schema migrations on the writer conn (init path).

        Runs once before any read conn is opened, so reads always see a
        fully-migrated schema.
        """
        SchemaManager.ensure_current(self._writer_conn)

    def _init_chromadb(self):
        """Initialize ChromaDB execution_embeddings collection.

        Called lazily on first semantic operation to avoid loading the
        80MB embedding model at application startup.

        After a failed init, retries are suppressed for _CHROMA_RETRY_COOLDOWN_S
        seconds (default 300s). This prevents log-flooding when the ChromaDB
        directory is missing or corrupted. The retry window is large enough for
        an operator to fix the underlying problem (restart, repair dir) and have
        the next call succeed automatically without a full process restart.

        _chroma_failed_at and _chroma_last_error are exposed via
        FeedbackLoop.get_learning_stats() → "execution_memory" so operators can
        observe the failure and its age without reading logs.
        """
        # Fast path: already initialized — no lock, no contention.
        if self._chromadb_available:
            return

        with self._chroma_init_lock:
            # Double-checked: another thread may have finished init while we
            # waited for the lock.
            if self._chromadb_available:
                return

            if self._chroma_client is self._CHROMADB_INIT_FAILED:
                if self._chroma_failed_at is None:
                    return  # Sentinel set without failure timestamp — permanent disable (test/manual)
                if time.monotonic() - self._chroma_failed_at < self._CHROMA_RETRY_COOLDOWN_S:
                    return
                # Cooldown expired — clear sentinel and retry
                self._chroma_client = None
                self._execution_collection = None
                self._learning_anchors = None

            try:
                import chromadb
                from chromadb.config import Settings as ChromaSettings
                client = chromadb.PersistentClient(
                    path=self._chroma_dir,
                    settings=ChromaSettings(anonymized_telemetry=False),
                )
                execution_collection = client.get_or_create_collection(
                    name="execution_embeddings",
                    metadata={"hnsw:space": "cosine"},
                )
                # learning_anchors: one anchor-query embedding per learned item
                # (NL hint or anti-pattern). MUST be cosine — the similarity
                # filter converts distance via `1 - distance`, which is only
                # correct for cosine. A collection's distance metric cannot be
                # changed after creation, so verify it and fail loud on mismatch.
                learning_anchors = client.get_or_create_collection(
                    name="learning_anchors",
                    metadata={"hnsw:space": "cosine"},
                )
                anchor_space = (learning_anchors.metadata or {}).get("hnsw:space")
                if anchor_space != "cosine":
                    raise RuntimeError(
                        f"learning_anchors collection has hnsw:space="
                        f"{anchor_space!r}, expected 'cosine' — the distance "
                        f"metric cannot be changed after creation"
                    )
                # Publish collections before the client. _chromadb_available
                # checks _chroma_client, so a concurrent lock-free fast-path
                # reader must never see the client set with collections still
                # None — assign _chroma_client last.
                self._execution_collection = execution_collection
                self._learning_anchors = learning_anchors
                self._chroma_client = client
                self._chroma_failed_at = None
                self._chroma_last_error = None
                logger.info(
                    "[LEARNING] ChromaDB initialized: %s "
                    "(collections: execution_embeddings, learning_anchors)",
                    self._chroma_dir,
                )
            except Exception as e:
                logger.error("[LEARNING] ChromaDB initialization failed: %s", e)
                self._chroma_client = self._CHROMADB_INIT_FAILED
                self._execution_collection = None
                self._learning_anchors = None
                self._chroma_failed_at = time.monotonic()
                self._chroma_last_error = str(e)

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
        _assert_writer_thread("ExecutionMemory.store")
        self._store_sqlite(record)
        self._store_chromadb(record)

    # ExecutionStore ABC implementation
    def store_execution(self, record: ExecutionRecord) -> None:
        """ExecutionStore interface — delegates to store()."""
        _assert_writer_thread("ExecutionMemory.store_execution")
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
        _assert_writer_thread("ExecutionMemory._store_sqlite")
        normalized_query = record.user_query.strip().lower()
        domain = record.domain or ""

        self._writer_conn.execute("BEGIN IMMEDIATE")
        try:
            # Step 1: Count existing identical runs (within transaction).
            # COALESCE(domain,'') so URL-less workflows — whose domain is
            # stored NULL — also deduplicate: a plain `domain = ''` never
            # equals NULL, which silently disabled dedup for them. For a
            # workflow that has a domain this is a no-op (COALESCE of a
            # non-null value is the value itself).
            run_count = self._writer_conn.execute(
                "SELECT COUNT(*) FROM execution_records "
                "WHERE LOWER(TRIM(user_query)) = ? "
                "AND COALESCE(domain, '') = ? AND test_status = ?",
                (normalized_query, domain, record.test_status),
            ).fetchone()[0]

            if run_count >= self.DEDUPLICATION_THRESHOLD:
                # Step 2: Aggregate — update the most recent matching record
                self._writer_conn.execute(
                    """
                    UPDATE execution_records
                    SET robot_code = ?, error_message = ?, timestamp = ?,
                        model_version = ?,
                        total_llm_calls = total_llm_calls + ?,
                        total_cost = total_cost + ?
                    WHERE id = (
                        SELECT id FROM execution_records
                        WHERE LOWER(TRIM(user_query)) = ?
                        AND COALESCE(domain, '') = ? AND test_status = ?
                        ORDER BY timestamp DESC LIMIT 1
                    )
                    """,
                    (
                        record.robot_code, record.error_message,
                        record.timestamp.isoformat(), record.model_version,
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
                self._writer_conn.execute(
                    """
                    INSERT INTO execution_records (
                        workflow_id, timestamp, user_query, url, domain,
                        robot_code, code_structure, test_status, execution_exit_code,
                        execution_duration_ms, failure_category, failed_keyword,
                        error_message, total_llm_calls, total_cost, injected_hint_ids,
                        model_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.workflow_id, record.timestamp.isoformat(),
                        record.user_query, record.url, record.domain,
                        record.robot_code, record.code_structure, record.test_status,
                        record.execution_exit_code, record.execution_duration_ms,
                        record.failure_category, record.failed_keyword,
                        record.error_message, record.total_llm_calls, record.total_cost,
                        record.injected_hint_ids, record.model_version,
                    ),
                )

            self._writer_conn.commit()
        except sqlite3.IntegrityError as e:
            # Case B: a re-run of an existing workflow_id now passes.
            # Run 1 stored {workflow_id, robot_code=v1, test_status='failed'};
            # Run 2 fires INSERT with the same workflow_id → UNIQUE violation.
            # Recovery: keep the original failing robot_code (failure context),
            # store the corrected code in working_code, and flip test_status
            # to 'passed' so the record reflects the current truth.
            #
            # IntegrityError must be caught BEFORE the generic Exception
            # handler — it is a subclass of Exception.
            self._writer_conn.rollback()
            if (
                "workflow_id" in str(e).lower()
                and record.test_status == "passed"
            ):
                self._update_to_passing_state(
                    record.workflow_id, record.robot_code,
                )
            else:
                # Other integrity violations (e.g. CHECK failures, or a
                # workflow_id collision on a non-passing re-run) propagate
                # so they surface in logs instead of being silently swallowed.
                raise
        except Exception:
            self._writer_conn.rollback()
            raise

    def _update_to_passing_state(
        self, workflow_id: str, working_code: str,
    ) -> None:
        """Apply Case B recovery to an existing execution_records row.

        Preserves the original failing robot_code, failure_category,
        error_message, and failed_keyword — they describe what went wrong
        on Run 1 and remain valuable context for `process_user_feedback`
        and Trigger 1 (compares v1 vs v2 robot code).

        Sets test_status='passed' to reflect the current outcome — this
        satisfies the existing CHECK (test_status IN ('passed','failed','error'))
        and corrects domain pass-rate stats / query_failures() results that
        would otherwise treat this workflow as permanently failed.
        """
        _assert_writer_thread("ExecutionMemory._update_to_passing_state")
        self._writer_conn.execute(
            "UPDATE execution_records "
            "SET working_code = ?, test_status = 'passed' "
            "WHERE workflow_id = ?",
            (working_code, workflow_id),
        )
        self._writer_conn.commit()
        logger.info(
            "[LEARNING] Workflow %s updated to passing state "
            "(working_code stored, test_status=passed)",
            workflow_id,
        )

    def _store_chromadb(self, record: ExecutionRecord) -> None:
        """
        Store execution embedding in ChromaDB for semantic search.

        Best-effort: failures are logged but don't block the pipeline.
        Triggers lazy initialization on first call.

        Case B re-run path: when add() fails on a duplicate workflow_id
        (the SQLite-side recovery has already updated the row, but the
        ChromaDB document still carries Run 1's metadata), fall through
        to upsert() so the metadata reflects the new test_status and
        failure_category. ChromaDB has no exists() check and the duplicate-id
        error string is an internal detail across versions — catch-and-upsert
        is the version-stable pattern. upsert() re-embeds the document
        (~10-50ms ONNX on this write-queue worker thread); acceptable
        because the metadata replacement IS the goal.
        """
        _assert_writer_thread("ExecutionMemory._store_chromadb")
        self._init_chromadb()
        if not self._chromadb_available:
            return

        metadata = {
            "test_status": record.test_status,
            "failure_category": record.failure_category or "",
            "domain": record.domain or "",
            "code_structure": record.code_structure or "",
            "workflow_id": record.workflow_id,
        }

        try:
            self._execution_collection.add(
                documents=[record.user_query],
                ids=[record.workflow_id],
                metadatas=[metadata],
            )
        except Exception as add_err:
            try:
                self._execution_collection.upsert(
                    documents=[record.user_query],
                    ids=[record.workflow_id],
                    metadatas=[metadata],
                )
            except Exception as upsert_err:
                logger.warning(
                    "[LEARNING] ChromaDB store failed (non-blocking): "
                    "add=%s, upsert=%s",
                    add_err, upsert_err,
                )

    # -------------------------------------------------------------------
    # Read Operations
    # -------------------------------------------------------------------

    def get(self, workflow_id: str) -> Optional[ExecutionRecord]:
        """Retrieve a single execution record by workflow_id.

        Returns None for missing rows (legacy contract — do not change).
        """
        with self.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM execution_records WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
        if row:
            return self._row_to_record(row)
        return None

    def query_by_domain(self, domain: str, limit: int = 50) -> List[ExecutionRecord]:
        """Get recent executions for a specific domain."""
        with self.read_conn() as conn:
            rows = conn.execute(
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

        with self.read_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
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
    # Learning Anchors — query-similarity hint filtering
    # -------------------------------------------------------------------

    def filter_by_query_similarity(
        self,
        user_query: str,
        candidate_ids: List[int],
        kind: str,
        threshold: float = 0.55,
    ) -> set[int]:
        """Return the subset of candidate_ids whose anchor query is cosine-
        similar (>= threshold) to user_query.

        kind: "nl" | "anti" — selects which engine's anchors to match.

        Failure modes:
            ChromaDB unavailable / query raises -> fail-OPEN: all candidates
                returned (degrade to scope-only behaviour, loud log).
            Candidate with no anchor doc        -> fail-CLOSED: that one
                candidate is excluded (a missing embedding can't be judged).
            Empty user_query / no candidates    -> empty set.
        """
        if not user_query or not user_query.strip() or not candidate_ids:
            return set()

        self._init_chromadb()                  # lazy ONNX load, idempotent
        coll = self._learning_anchors
        if coll is None:                       # ChromaDB unavailable
            logger.warning(
                "[LEARNING] learning_anchors unavailable — "
                "similarity filter fails open"
            )
            return set(candidate_ids)

        try:
            # Size n_results to the docs that ACTUALLY exist for these
            # candidates — never coll.count() (which counts both kinds and
            # every record, so it could exceed the filtered set and the
            # behaviour of n_results > matches is not something to depend
            # on). Doc ids are deterministic — f"{kind}:{record_id}" — so a
            # get() by id reports exactly which candidates have a doc.
            expected_ids = [f"{kind}:{rid}" for rid in candidate_ids]
            present = coll.get(ids=expected_ids, include=[])   # ids only
            n = len(present["ids"])
            if n == 0:
                # No candidate has a doc. Distinguish the two causes:
                if coll.count() == 0:
                    logger.error(
                        "[LEARNING] learning_anchors empty but candidates "
                        "exist — reconcile likely failed; failing open"
                    )
                    return set(candidate_ids)        # whole store empty
                return set()                         # un-reconciled -> drop
            result = coll.query(
                query_texts=[user_query],
                n_results=n,                         # exact filtered count
                where={"$and": [{"kind": kind},
                                {"record_id": {"$in": list(candidate_ids)}}]},
                include=["metadatas", "distances"],
            )
        except Exception as e:
            logger.warning(
                "[LEARNING] similarity filter query failed (fail-open): %s", e
            )
            return set(candidate_ids)

        survivors: set[int] = set()
        metas = result["metadatas"][0] if result.get("metadatas") else []
        dists = result["distances"][0] if result.get("distances") else []
        for meta, dist in zip(metas, dists):
            sim = 1.0 - dist                   # learning_anchors is cosine
            if sim >= threshold:
                survivors.add(meta["record_id"])

        logger.info(
            "[LEARNING] sim_filter kind=%s in=%d out=%d",
            kind, len(candidate_ids), len(survivors),
        )
        return survivors

    def add_anchor(self, kind: str, record_id: int, anchor_query: str) -> None:
        """Add (or replace) one anchor-query document in learning_anchors.

        Best-effort: a ChromaDB failure is logged and swallowed — the SQL
        row stands and the missing doc is healed by the next reconcile.
        Uses upsert so a repeated call (reconcile, retry) is idempotent.
        Must run on the learning-writer thread.
        """
        _assert_writer_thread("ExecutionMemory.add_anchor")
        if not anchor_query or not anchor_query.strip():
            logger.warning(
                "[LEARNING] add_anchor skipped — empty anchor "
                "(kind=%s record_id=%s)", kind, record_id,
            )
            return
        self._init_chromadb()
        coll = self._learning_anchors
        if coll is None:
            logger.warning(
                "[LEARNING] add_anchor skipped — learning_anchors "
                "unavailable (kind=%s record_id=%s)", kind, record_id,
            )
            return
        try:
            coll.upsert(
                ids=[f"{kind}:{record_id}"],
                documents=[anchor_query],
                metadatas=[{"kind": kind, "record_id": record_id}],
            )
        except Exception as e:
            logger.warning(
                "[LEARNING] add_anchor failed (non-blocking, "
                "kind=%s record_id=%s): %s", kind, record_id, e,
            )

    def reconcile_anchors(self) -> dict:
        """Ensure every NL hint and anti-pattern with a non-empty anchor has
        a learning_anchors document; embed and add any that are missing.

        Subsumes the initial backfill (first run embeds every existing row)
        and heals incremental anchor-add failures. Idempotent — a second run
        with nothing missing does only a cheap id-diff. Must run on the
        learning-writer thread (submitted to LearningWriteQueue).

        Returns {ran_at, checked, missing_count} where missing_count is the
        POST-run residual — rows still lacking a doc after this run (0 in the
        normal case). ChromaDB being unavailable is reported as a no-op
        (checked=0, missing_count=0); that failure is surfaced by the
        ChromaDB health check, not by this signal.
        """
        _assert_writer_thread("ExecutionMemory.reconcile_anchors")
        ran_at = datetime.now(timezone.utc).isoformat()
        self._init_chromadb()
        coll = self._learning_anchors
        if coll is None:
            logger.error(
                "[LEARNING] reconcile_anchors skipped — "
                "learning_anchors unavailable"
            )
            return {"ran_at": ran_at, "checked": 0, "missing_count": 0}

        # Desired docs: (doc_id, record_id, kind, anchor_text) from SQL.
        desired: list = []
        with self.read_conn() as conn:
            for r in conn.execute(
                "SELECT id, anchor_query FROM nl_feedback_corrections "
                "WHERE anchor_query IS NOT NULL AND TRIM(anchor_query) != ''"
            ).fetchall():
                desired.append(
                    (f"nl:{r['id']}", r["id"], "nl", r["anchor_query"])
                )
            for r in conn.execute(
                "SELECT id, query_pattern FROM anti_patterns "
                "WHERE query_pattern IS NOT NULL AND TRIM(query_pattern) != ''"
            ).fetchall():
                desired.append(
                    (f"anti:{r['id']}", r["id"], "anti", r["query_pattern"])
                )
            null_anchors = conn.execute(
                "SELECT COUNT(*) FROM nl_feedback_corrections "
                "WHERE anchor_query IS NULL OR TRIM(anchor_query) = ''"
            ).fetchone()[0]

        if null_anchors:
            logger.warning(
                "[LEARNING] reconcile_anchors: %d nl_feedback_corrections "
                "row(s) have no anchor_query — skipped (cannot embed)",
                null_anchors,
            )

        checked = len(desired)
        if checked == 0:
            return {"ran_at": ran_at, "checked": 0, "missing_count": 0}

        try:
            existing = set(
                coll.get(ids=[d[0] for d in desired], include=[])["ids"]
            )
        except Exception as e:
            logger.error("[LEARNING] reconcile_anchors get() failed: %s", e)
            return {"ran_at": ran_at, "checked": checked,
                    "missing_count": checked}

        missing = [d for d in desired if d[0] not in existing]
        if not missing:
            logger.info(
                "[LEARNING] reconcile_anchors: %d anchors present, "
                "none missing", checked,
            )
            return {"ran_at": ran_at, "checked": checked, "missing_count": 0}

        try:
            coll.upsert(
                ids=[d[0] for d in missing],
                documents=[d[3] for d in missing],
                metadatas=[{"kind": d[2], "record_id": d[1]} for d in missing],
            )
            logger.info(
                "[LEARNING] reconcile_anchors: embedded %d missing anchor(s) "
                "of %d checked", len(missing), checked,
            )
            return {"ran_at": ran_at, "checked": checked, "missing_count": 0}
        except Exception as e:
            logger.error("[LEARNING] reconcile_anchors upsert failed: %s", e)
            return {"ran_at": ran_at, "checked": checked,
                    "missing_count": len(missing)}

    # -------------------------------------------------------------------
    # Update Operations
    # -------------------------------------------------------------------

    def update_user_feedback(
        self, workflow_id: str, feedback_text: str, feedback_type: str
    ) -> None:
        """Update execution record with user's NL feedback."""
        _assert_writer_thread("ExecutionMemory.update_user_feedback")
        self._writer_conn.execute(
            "UPDATE execution_records SET user_feedback = ?, user_feedback_type = ? "
            "WHERE workflow_id = ?",
            (feedback_text, feedback_type, workflow_id),
        )
        self._writer_conn.commit()

    # -------------------------------------------------------------------
    # Aggregation / Stats
    # -------------------------------------------------------------------

    def get_domain_stats(self, domain: str) -> dict:
        """Get aggregated stats for a domain."""
        with self.read_conn() as conn:
            row = conn.execute(
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
        with self.read_conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM execution_records"
            ).fetchone()[0]

    def update_daily_stats(self, test_status: str) -> None:
        """Update learning_stats table with daily aggregates.

        Uses INSERT ... ON CONFLICT to atomically create or update the
        row for today's date.  Called by FeedbackLoop after every execution.
        """
        _assert_writer_thread("ExecutionMemory.update_daily_stats")
        today = datetime.now().strftime("%Y-%m-%d")
        self._writer_conn.execute(
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
        self._writer_conn.commit()

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
            working_code=row["working_code"],
            injected_hint_ids=row["injected_hint_ids"],
            model_version=row["model_version"],
        )

    def close(self):
        """Close database connections."""
        if self._writer_conn:
            self._writer_conn.close()
            logger.debug("[LEARNING] SQLite writer connection closed")
