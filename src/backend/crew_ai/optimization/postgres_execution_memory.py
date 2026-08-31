"""
PostgresExecutionMemory — PostgreSQL relational store for the learning system
(Phase 4 migration; replaced the removed SQLite ExecutionMemory store).

The writer connection and read connections are pg_compat.CompatConnection
objects, so this class AND the learning engines (which reach into
`_writer_conn` / `read_conn()`) all speak SQLite-dialect SQL (`?` placeholders,
sqlite3.Row access, `except sqlite3.IntegrityError`) that the adapter translates
to psycopg/Postgres. Genuinely dialect-specific spots (RETURNING, strftime,
INSERT OR IGNORE, and the jsonb hint-id KPI queries) are ported at the call
sites, not here.

Single-writer model preserved: ONE writer connection used only by the
learning-writer thread (LearningWriteQueue), guarded by _assert_writer_thread
and reopened transparently if Postgres drops it (the _writer_conn property).
Reads use a pooled connection per call. Vectors live in pgvector, embedded
with fastembed (see the vector methods below).

Reuses ExecutionRecord / helpers from execution_memory.py.
Referenced by: learning_registry.py (after cutover).
"""

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

import psycopg
from psycopg_pool import ConnectionPool

from src.backend.core.config import PG_CONNECT_TIMEOUT_S, settings
from src.backend.crew_ai.optimization import embedding as _shared_embedding
from src.backend.crew_ai.optimization import pg_compat, pg_schema
from src.backend.crew_ai.optimization.pg_compat import CompatConnection, compat_row
from src.backend.crew_ai.optimization.execution_memory import (
    ExecutionRecord,
    _assert_writer_thread,
    _mark,
)
from src.backend.crew_ai.optimization.learning_config import (
    ExecutionStore,
    SemanticStore,
)

logger = logging.getLogger(__name__)

# The model name lives in embedding.EMBED_MODEL — this store borrows that
# module's singleton (Task 31) and no longer constructs its own TextEmbedding,
# so a local copy of the constant would be a second source of truth for the
# same value. Same model ChromaDB used by default (all-MiniLM-L6-v2, 384-dim),
# so the similarity distribution — and the 0.55 retrieval threshold tuned
# against it — carry over unchanged.


class PostgresExecutionMemory(ExecutionStore, SemanticStore):
    """PostgreSQL store for execution records + pgvector embeddings (Phase 4).

    Relational data lives in Postgres; the learning vectors (anchors + execution
    embeddings) live in pgvector, embedded with fastembed (standalone ONNX). The
    `_chroma_*` attribute names are retained because the shared FeedbackLoop
    observability (get_health_status / get_learning_stats) reads them — they now
    track the fastembed embedder state, not ChromaDB.
    """

    # Sentinel for failed embedder init. The shared FeedbackLoop health check
    # compares against the store instance's own class attribute
    # (`self.execution_memory._CHROMADB_INIT_FAILED`), so a dedicated object
    # is sufficient — no cross-class identity required. A dedicated object
    # (not a magic string) keeps `is not None` checks honest.
    _CHROMADB_INIT_FAILED = object()
    _CHROMA_RETRY_COOLDOWN_S: int = 300

    def __init__(self, dsn: str = None, chroma_dir: str = None):
        self.dsn = dsn or settings.DATABASE_URL

        # Schema bootstrap on a throwaway raw connection (pg_schema uses native
        # %s SQL, so it must NOT go through the ?-translating compat adapter).
        _setup = psycopg.connect(
            self.dsn, autocommit=True, connect_timeout=PG_CONNECT_TIMEOUT_S)
        try:
            pg_schema.ensure_schema(_setup)
        finally:
            _setup.close()

        # Writer: one compat connection, used only by the learning-writer thread.
        # Accessed through the _writer_conn property, which transparently
        # reopens it if Postgres dropped it (see property docstring).
        self._writer_conn = pg_compat.connect(self.dsn, autocommit=False)
        # Reads: pooled raw connections (compat_row factory) wrapped per borrow.
        self._read_pool = ConnectionPool(
            conninfo=self.dsn, min_size=1, max_size=10,
            kwargs={"row_factory": compat_row, "autocommit": True,
                    "connect_timeout": PG_CONNECT_TIMEOUT_S},
            open=True,
        )

        # fastembed embedder: lazy. `_chroma_client` holds None (not yet loaded),
        # the TextEmbedding instance (ready), or _CHROMADB_INIT_FAILED (disabled
        # in tests / init failed). `_chroma_failed_at` None + sentinel = disabled.
        self._chroma_client = None
        self._chroma_failed_at = None
        self._chroma_last_error = None
        self._chroma_init_lock = threading.Lock()
        # Text -> literal memo (Task 31), invalidated when the client identity
        # changes (tests inject fakes; a reload after failure). Same rationale
        # as embedding._memo: the query is embedded repeatedly per workflow.
        self._embed_memo: dict = {}
        self._embed_memo_client = None
        self._last_trace_prune_ts = 0.0
        logger.debug("[LEARNING] PostgresExecutionMemory initialised")

    # -------------------------------------------------------------------
    # Connections (compat — engines reach into these directly)
    # -------------------------------------------------------------------

    @property
    def _writer_conn(self):
        """The single writer connection, reopened transparently if it died.

        Unlike the pooled read connections (psycopg_pool replaces broken pool
        members on its own), this is a bare long-lived connection: without this
        check, one Postgres restart or dropped TCP session would fail every
        learning write until the app restarts — silently, since all write
        callers log-and-continue. The write that hit the outage is still lost
        (learning is best-effort by design); the next one recovers.

        Writer-thread-only access (enforced by _assert_writer_thread at the
        call sites), so the check-then-reconnect needs no lock. Reconnect
        failures propagate to the caller's existing error handling.
        """
        conn = self._writer
        if conn is None or getattr(conn, "closed", False) or getattr(conn, "broken", False):
            self._writer = pg_compat.connect(self.dsn, autocommit=False)
            if conn is not None:
                logger.warning(
                    "[LEARNING] writer connection was %s — reopened",
                    "broken" if getattr(conn, "broken", False) else "closed",
                )
        return self._writer

    @_writer_conn.setter
    def _writer_conn(self, value):
        # Tests inject fakes/proxies here; __init__ and reconnect set the real one.
        self._writer = value

    @contextmanager
    def read_conn(self):
        with self._read_pool.connection() as raw:
            yield CompatConnection(raw)

    def get_read_connection(self):
        """Standalone compat read connection (caller closes)."""
        return pg_compat.connect(self.dsn, autocommit=True)

    # -------------------------------------------------------------------
    # Core write operations
    # -------------------------------------------------------------------

    def store(self, record: ExecutionRecord) -> None:
        _assert_writer_thread("PostgresExecutionMemory.store")
        self._store_relational(record)
        self._store_execution_embedding(record)

    def store_execution(self, record: ExecutionRecord) -> None:
        _assert_writer_thread("PostgresExecutionMemory.store_execution")
        self.store(record)

    def _store_relational(self, record: ExecutionRecord) -> None:
        """One row per run, always. Never aggregate repeats.

        An aggregation branch used to UPDATE the newest row in the
        (query, domain, status, org) bucket once it held 5 rows, instead of
        inserting. It contradicted `workflow_id TEXT UNIQUE NOT NULL`: the
        6th run had no record, so `em.get(workflow_id)` — the only way
        feedback and `is_first_attempt` reach a run — returned None, and the
        UPDATE (keyed on ORDER BY timestamp, not on this record) wrote one
        run's code onto another's while bypassing the Case-B arm below. It
        saved nothing: `_store_execution_embedding` writes the far larger
        384-dim row unconditionally either way. Do not reintroduce it.
        """
        _assert_writer_thread("PostgresExecutionMemory._store_relational")
        try:
            self._writer_conn.execute(
                """
                INSERT INTO execution_records (
                    workflow_id, timestamp, user_query, url, domain,
                    robot_code, code_structure, test_status, execution_exit_code,
                    execution_duration_ms, failure_category, failed_keyword,
                    error_message, total_llm_calls, total_cost, injected_hint_ids,
                    model_version, org_id, hint_attribution_done
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    record.workflow_id, record.timestamp.isoformat(),
                    record.user_query, record.url, record.domain,
                    record.robot_code, record.code_structure, record.test_status,
                    record.execution_exit_code, record.execution_duration_ms,
                    record.failure_category, record.failed_keyword,
                    record.error_message, record.total_llm_calls, record.total_cost,
                    record.injected_hint_ids, record.model_version,
                    record.org_id,
                ),
            )
            self._writer_conn.commit()
        except sqlite3.IntegrityError as e:
            # Case B: a re-run of an existing workflow_id now passes.
            self._writer_conn.rollback()
            if "workflow_id" in str(e).lower() and record.test_status == "passed":
                self._update_to_passing_state(record.workflow_id, record.robot_code)
            else:
                # F6 (recorded, not fixed): a FAILED re-run of the same
                # workflow_id falls here instead — the guard above only
                # handles "passed" — and leaves the v1 row untouched. Feedback
                # on this workflow_id then reads v1's robot_code and
                # error_message, and Trigger 2 judges active hints against
                # code older than the run the user is giving feedback on. If
                # v1 credited hints its row also carries
                # hint_attribution_done = 1, so the re-run's usage is never
                # credited either.
                #
                # This is the ORDINARY path, not an API-caller edge case, and
                # it has been executed: GeneratePage sends
                # `workflow_id: workflowId.current`, which is set once when
                # the code is generated and cleared only by "New Test" or by
                # emptying the code box — so every repeat click of "Run Test"
                # on the same generated code re-enters here. Driven through
                # the real SPA, the second failing run logged
                # "[LEARNING] Write failed (non-blocking): duplicate key ...
                # execution_records_workflow_id_key" and the row kept the
                # FIRST run's timestamp and error_message.
                #
                # Left as-is deliberately: workflow_id is UNIQUE, so the only
                # alternatives are overwriting v1 (destroying the record
                # feedback is filed against) or a schema change. Raising here
                # is what keeps _process_learning_record's is_first_attempt
                # honest — see the DEPENDENCY note at its pre_run_record read.
                raise
        except Exception:
            self._writer_conn.rollback()
            raise

    def _update_to_passing_state(self, workflow_id: str, working_code: str) -> None:
        _assert_writer_thread("PostgresExecutionMemory._update_to_passing_state")
        try:
            self._writer_conn.execute(
                "UPDATE execution_records "
                "SET working_code = ?, test_status = 'passed' WHERE workflow_id = ?",
                (working_code, workflow_id),
            )
            self._writer_conn.commit()
            logger.info("[LEARNING] Workflow %s updated to passing state", workflow_id)
        except Exception:
            self._writer_conn.rollback()
            raise

    def update_user_feedback(self, workflow_id: str, feedback_text: str, feedback_type: str) -> None:
        _assert_writer_thread("PostgresExecutionMemory.update_user_feedback")
        try:
            self._writer_conn.execute(
                "UPDATE execution_records SET user_feedback = ?, user_feedback_type = ? "
                "WHERE workflow_id = ?",
                (feedback_text, feedback_type, workflow_id),
            )
            self._writer_conn.commit()
        except Exception:
            self._writer_conn.rollback()
            raise

    def update_daily_stats(self, test_status: str) -> None:
        _assert_writer_thread("PostgresExecutionMemory.update_daily_stats")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")  # UTC day buckets
        try:
            self._writer_conn.execute(
                """
                INSERT INTO learning_stats (stat_date, total_executions, total_passed, total_failed)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(stat_date) DO UPDATE SET
                    total_executions = learning_stats.total_executions + 1,
                    total_passed = learning_stats.total_passed + EXCLUDED.total_passed,
                    total_failed = learning_stats.total_failed + EXCLUDED.total_failed
                """,
                (today, 1 if test_status == "passed" else 0, 1 if test_status != "passed" else 0),
            )
            self._writer_conn.commit()
        except Exception:
            self._writer_conn.rollback()
            raise

    def store_hint_workflow_trace(self, workflow_id: str, trace: dict) -> None:
        _assert_writer_thread("PostgresExecutionMemory.store_hint_workflow_trace")
        if not trace:
            return
        created_at = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                workflow_id, hid, e.get("scope"), e.get("source"), e.get("priority"),
                e.get("similarity_score"), 1 if e.get("available") else 0,
                1 if e.get("injected") else 0, e.get("drop_reason"), created_at,
            )
            for hid, e in trace.items()
        ]
        try:
            self._writer_conn.executemany(
                "INSERT INTO hint_workflow_trace "
                "(workflow_id, hint_id, scope, source, priority, "
                " similarity_score, available, injected, drop_reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(workflow_id, hint_id) DO NOTHING",
                rows,
            )
            self._writer_conn.commit()
        except Exception as e:
            logger.warning("[LEARNING] store_hint_workflow_trace failed (non-blocking): %s", e)
            self._writer_conn.rollback()
        self._maybe_prune_hint_workflow_trace()

    def _maybe_prune_hint_workflow_trace(self) -> None:
        try:
            now = time.time()
            if now - self._last_trace_prune_ts < 86400:
                return
            self._last_trace_prune_ts = now
            self.prune_hint_workflow_trace(settings.HINT_TRACE_RETENTION_DAYS)
        except Exception as e:
            logger.warning("[LEARNING] trace prune guard failed (non-blocking): %s", e)

    def prune_hint_workflow_trace(self, retention_days: int) -> int:
        _assert_writer_thread("PostgresExecutionMemory.prune_hint_workflow_trace")
        if not retention_days or retention_days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        try:
            cur = self._writer_conn.execute(
                "DELETE FROM hint_workflow_trace WHERE created_at < ?", (cutoff,))
            deleted = cur.rowcount
            self._writer_conn.commit()
            return deleted
        except Exception as e:
            logger.warning("[LEARNING] prune_hint_workflow_trace failed (non-blocking): %s", e)
            try:
                self._writer_conn.rollback()
            except Exception:
                pass
            return 0

    # -------------------------------------------------------------------
    # Read operations
    # -------------------------------------------------------------------

    def get(self, workflow_id: str):
        with self.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM execution_records WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def query_by_domain(self, domain: str, limit: int = 50):
        with self.read_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM execution_records WHERE domain = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (domain, limit),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def query_failures(self, category: str = None, domain: str = None, limit: int = 50):
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
        return [self._row_to_record(r) for r in rows]

    def get_domain_stats(self, domain: str) -> dict:
        with self.read_conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                    SUM(CASE WHEN test_status = 'passed' THEN 1 ELSE 0 END) AS passed,
                    SUM(CASE WHEN test_status = 'failed' THEN 1 ELSE 0 END) AS failed,
                    AVG(execution_duration_ms) AS avg_duration,
                    AVG(total_cost) AS avg_cost
                FROM execution_records WHERE domain = ?
                """,
                (domain,),
            ).fetchone()
        if row and row["total"] > 0:
            return {
                "total": row["total"], "passed": row["passed"] or 0, "failed": row["failed"] or 0,
                "avg_duration_ms": row["avg_duration"], "avg_cost": row["avg_cost"],
                "pass_rate": (row["passed"] or 0) / row["total"],
            }
        return {"total": 0, "passed": 0, "failed": 0,
                "avg_duration_ms": None, "avg_cost": None, "pass_rate": 0}

    def get_total_records(self) -> int:
        with self.read_conn() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM execution_records").fetchone()["n"]

    def _row_to_record(self, row) -> ExecutionRecord:
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
            # injected_hint_ids is jsonb (psycopg returns a parsed list); the
            # ExecutionRecord contract is a JSON string, so re-serialize.
            injected_hint_ids=(
                json.dumps(row["injected_hint_ids"])
                if row["injected_hint_ids"] is not None else None
            ),
            model_version=row["model_version"],
            # The owning org must survive the round trip: this is the record
            # process_user_feedback hands to the engines, and T9's write guard
            # refuses an org-less one. Dropping it here made every read-back
            # record look untenanted.
            org_id=row["org_id"],
            hint_attribution_done=row["hint_attribution_done"],
        )

    # -------------------------------------------------------------------
    # Vector methods — pgvector + fastembed (Phase 4 slice 6).
    # -------------------------------------------------------------------

    def _init_chromadb(self):
        """Bind the SHARED fastembed model into `_chroma_client` (idempotent).

        Task 31: the store no longer loads its own TextEmbedding — it borrows
        the process-wide singleton from embedding.get_embedder() (same model,
        same cache dir), removing the duplicate ~1.3s ONNX cold load. The old
        ChromaDB lazy-init tri-state is preserved: the _CHROMADB_INIT_FAILED
        sentinel with `_chroma_failed_at is None` means "explicitly disabled"
        (tests) and stays disabled; with a timestamp it is a genuine failure
        retried after the cooldown (embedding.py keeps its own cooldown too).
        """
        if self._chromadb_available:
            return
        with self._chroma_init_lock:
            if self._chromadb_available:
                return
            if self._chroma_client is self._CHROMADB_INIT_FAILED:
                if self._chroma_failed_at is None:
                    return  # explicitly disabled
                if time.monotonic() - self._chroma_failed_at < self._CHROMA_RETRY_COOLDOWN_S:
                    return
                self._chroma_client = None
            shared = _shared_embedding.get_embedder()
            if shared is not None:
                self._chroma_client = shared
                self._chroma_failed_at = None
                self._chroma_last_error = None
            else:
                logger.error(
                    "[LEARNING] shared fastembed embedder unavailable")
                self._chroma_client = self._CHROMADB_INIT_FAILED
                self._chroma_failed_at = time.monotonic()
                self._chroma_last_error = "shared fastembed embedder unavailable"

    @property
    def _chromadb_available(self) -> bool:
        """Passive (no init) — the embedder is loaded and usable."""
        return (self._chroma_client is not None
                and self._chroma_client is not self._CHROMADB_INIT_FAILED)

    def _embed(self, text: str) -> str | None:
        """Embed `text` to a pgvector literal '[v1,v2,...]', or None if disabled.

        Memoized per client instance (Task 31); failures are never cached.

        NOT delegated to embedding.embed_to_literal(): that helper always uses
        the module singleton, whereas this store embeds through whatever
        `_chroma_client` currently holds — tests swap in their own instance
        (conftest `em_vec`) and disable it via the _CHROMADB_INIT_FAILED
        sentinel. Hence a second memo, keyed on the client identity. Literal
        formatting IS shared, so both paths stay byte-identical.
        """
        self._init_chromadb()
        if not self._chromadb_available:
            return None
        client = self._chroma_client
        if self._embed_memo_client is not client:
            self._embed_memo = {}
            self._embed_memo_client = client
        hit = self._embed_memo.get(text)
        if hit is not None:
            return hit
        try:
            vec = next(iter(client.embed([text])))
        except Exception as e:
            logger.warning("[LEARNING] embedding failed (non-blocking): %s", e)
            return None
        lit = _shared_embedding.to_literal(vec)
        if len(self._embed_memo) >= 64:
            self._embed_memo.clear()
        self._embed_memo[text] = lit
        return lit

    def _store_execution_embedding(self, record: ExecutionRecord) -> None:
        _assert_writer_thread("PostgresExecutionMemory._store_execution_embedding")
        if not record.user_query:
            return
        vec = self._embed(record.user_query)
        if not vec:
            return
        try:
            self._writer_conn.execute(
                "INSERT INTO execution_embeddings "
                "(workflow_id, user_query, test_status, failure_category, domain, "
                " code_structure, embedding, org_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?::vector, ?) "
                "ON CONFLICT (workflow_id) DO UPDATE SET "
                "  user_query = EXCLUDED.user_query, test_status = EXCLUDED.test_status, "
                "  failure_category = EXCLUDED.failure_category, domain = EXCLUDED.domain, "
                "  code_structure = EXCLUDED.code_structure, embedding = EXCLUDED.embedding, "
                "  org_id = EXCLUDED.org_id",
                (record.workflow_id, record.user_query, record.test_status,
                 record.failure_category or "", record.domain or "",
                 record.code_structure or "", vec, record.org_id),
            )
            self._writer_conn.commit()
        except Exception as e:
            logger.warning("[LEARNING] execution embedding store failed (non-blocking): %s", e)
            try:
                self._writer_conn.rollback()
            except Exception:
                pass

    def find_similar_executions(self, user_query: str, top_k: int = 5,
                                org_id: str | None = None) -> list:
        """Nearest past executions by embedding distance, scoped to one org.

        A missing org returns [] (T9): the filter used to be dropped entirely,
        which handed an org-less caller every org's executions.
        """
        if org_id is None:
            logger.warning(
                "[LEARNING] execution similarity read refused (no org): "
                "returning no executions rather than every org's")
            return []
        vec = self._embed(user_query)
        if not vec:
            return []
        try:
            with self.read_conn() as conn:
                rows = conn.execute(
                    "SELECT workflow_id, test_status, failure_category, domain, "
                    "       code_structure, 1 - (embedding <=> ?::vector) AS similarity "
                    "FROM execution_embeddings WHERE org_id = ? "
                    "ORDER BY embedding <=> ?::vector LIMIT ?",
                    [vec, org_id, vec, top_k],
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("[LEARNING] similarity search failed: %s", e)
            return []

    def store_embedding(self, text: str, metadata: dict) -> None:
        _assert_writer_thread("PostgresExecutionMemory.store_embedding")
        vec = self._embed(text)
        if not vec:
            return
        wid = metadata.get("workflow_id") or str(id(text))
        try:
            self._writer_conn.execute(
                "INSERT INTO execution_embeddings "
                "(workflow_id, user_query, test_status, failure_category, domain, "
                " code_structure, embedding, org_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?::vector, ?) "
                "ON CONFLICT (workflow_id) DO UPDATE SET "
                "  user_query = EXCLUDED.user_query, embedding = EXCLUDED.embedding, "
                "  org_id = EXCLUDED.org_id",
                (wid, text, metadata.get("test_status"), metadata.get("failure_category"),
                 metadata.get("domain"), metadata.get("code_structure"), vec,
                 metadata.get("org_id")),
            )
            self._writer_conn.commit()
        except Exception as e:
            logger.warning("[LEARNING] store_embedding failed (non-blocking): %s", e)
            try:
                self._writer_conn.rollback()
            except Exception:
                pass

    def search_similar(self, query: str, top_k: int = 5) -> list:
        return self.find_similar_executions(query, top_k)

    def filter_by_query_similarity(self, user_query, candidate_ids, kind,
                                   threshold=0.55, score_sink=None,
                                   org_id: str | None = None) -> set:
        if not user_query or not user_query.strip() or not candidate_ids:
            return set()
        _mark(score_sink, candidate_ids, "no_anchor")
        qvec = self._embed(user_query)
        if not qvec:  # embedder disabled / failed / empty → fail-open
            _mark(score_sink, candidate_ids, "fail_open")
            return set(candidate_ids)
        total = None
        try:
            with self.read_conn() as conn:
                if org_id is not None:
                    rows = conn.execute(
                        "SELECT record_id, 1 - (embedding <=> ?::vector) AS sim "
                        "FROM learning_anchors "
                        "WHERE kind = ? AND record_id = ANY(?) "
                        "  AND (org_id = ? OR org_id IS NULL)",
                        (qvec, kind, list(candidate_ids), org_id),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT record_id, 1 - (embedding <=> ?::vector) AS sim "
                        "FROM learning_anchors WHERE kind = ? AND record_id = ANY(?)",
                        (qvec, kind, list(candidate_ids)),
                    ).fetchall()
                if not rows:
                    # COUNT only on the empty branch (Task 31): it exists
                    # solely to tell cold start (no anchors at all) from
                    # genuinely unanchored candidates below.
                    total = conn.execute(
                        "SELECT COUNT(*) AS n FROM learning_anchors"
                    ).fetchone()["n"]
        except Exception as e:
            logger.warning("[LEARNING] similarity filter query failed (fail-open): %s", e)
            _mark(score_sink, candidate_ids, "fail_open")
            return set(candidate_ids)
        if not rows:
            # No anchor exists for any candidate. Cold start (no anchors at all)
            # fails open; otherwise these candidates are genuinely unanchored.
            if total == 0:
                _mark(score_sink, candidate_ids, "fail_open")
                return set(candidate_ids)
            return set()
        survivors: set = set()
        for r in rows:
            rid = r["record_id"]
            sim = float(r["sim"])
            if sim >= threshold:
                survivors.add(rid)
                if score_sink is not None:
                    score_sink[rid] = {"sim": sim, "outcome": "survived"}
            elif score_sink is not None:
                score_sink[rid] = {"sim": sim, "outcome": "similarity_below"}
        return survivors

    def add_anchor(self, kind: str, record_id: int, anchor_query: str,
                   org_id: str | None = None) -> None:
        _assert_writer_thread("PostgresExecutionMemory.add_anchor")
        if not anchor_query or not anchor_query.strip():
            return
        vec = self._embed(anchor_query)
        if not vec:
            return
        try:
            self._writer_conn.execute(
                "INSERT INTO learning_anchors "
                "(anchor_key, kind, record_id, anchor_query, embedding, org_id) "
                "VALUES (?, ?, ?, ?, ?::vector, ?) "
                "ON CONFLICT (anchor_key) DO UPDATE SET "
                "  kind = EXCLUDED.kind, record_id = EXCLUDED.record_id, "
                "  anchor_query = EXCLUDED.anchor_query, embedding = EXCLUDED.embedding, "
                "  org_id = EXCLUDED.org_id",
                (f"{kind}:{record_id}", kind, record_id, anchor_query, vec, org_id),
            )
            self._writer_conn.commit()
        except Exception as e:
            logger.warning("[LEARNING] add_anchor failed (non-blocking): %s", e)
            try:
                self._writer_conn.rollback()
            except Exception:
                pass

    def reconcile_anchors(self) -> dict:
        _assert_writer_thread("PostgresExecutionMemory.reconcile_anchors")
        ran_at = datetime.now(timezone.utc).isoformat()
        self._init_chromadb()
        if not self._chromadb_available:  # embedder disabled / failed
            return {"ran_at": ran_at, "checked": 0, "missing_count": 0}
        desired: list = []
        with self.read_conn() as conn:
            for r in conn.execute(
                "SELECT id, anchor_query FROM nl_feedback_corrections "
                "WHERE anchor_query IS NOT NULL AND TRIM(anchor_query) != ''"
            ).fetchall():
                desired.append((f"nl:{r['id']}", r["id"], "nl", r["anchor_query"]))
            for r in conn.execute(
                "SELECT id, query_pattern FROM anti_patterns "
                "WHERE query_pattern IS NOT NULL AND TRIM(query_pattern) != ''"
            ).fetchall():
                desired.append((f"anti:{r['id']}", r["id"], "anti", r["query_pattern"]))
        checked = len(desired)
        if checked == 0:
            return {"ran_at": ran_at, "checked": 0, "missing_count": 0}
        try:
            with self.read_conn() as conn:
                existing = {
                    row["anchor_key"] for row in conn.execute(
                        "SELECT anchor_key FROM learning_anchors WHERE anchor_key = ANY(?)",
                        ([d[0] for d in desired],),
                    ).fetchall()
                }
        except Exception as e:
            logger.error("[LEARNING] reconcile_anchors get() failed: %s", e)
            return {"ran_at": ran_at, "checked": checked, "missing_count": checked}
        missing = [d for d in desired if d[0] not in existing]
        if not missing:
            return {"ran_at": ran_at, "checked": checked, "missing_count": 0}
        for _key, rid, kind, query in missing:
            self.add_anchor(kind, rid, query)  # embeds + upserts; swallows errors
        # Re-check which of the missing keys are now present for an honest count.
        try:
            with self.read_conn() as conn:
                now_present = {
                    row["anchor_key"] for row in conn.execute(
                        "SELECT anchor_key FROM learning_anchors WHERE anchor_key = ANY(?)",
                        ([d[0] for d in missing],),
                    ).fetchall()
                }
        except Exception as e:
            logger.error("[LEARNING] reconcile_anchors recheck failed: %s", e)
            return {"ran_at": ran_at, "checked": checked, "missing_count": len(missing)}
        still_missing = sum(1 for d in missing if d[0] not in now_present)
        return {"ran_at": ran_at, "checked": checked, "missing_count": still_missing}

    # -------------------------------------------------------------------
    # Startup backfill — attribute pre-tenancy org_ids
    # -------------------------------------------------------------------

    def _backfill_conn(self):
        """Short-lived dedicated connection for org_id backfill (NOT the writer conn).

        Opens a new pg_compat connection on self.dsn (which already carries the
        correct search_path in the DSN options), so the backfill UPDATEs land on
        the same schema as the rest of the store — including the isolated test
        schema (learning_test) during tests.
        """
        return pg_compat.connect(self.dsn, autocommit=True)

    def _exec_backfill(self, conn, sql: str, params=()) -> int:
        """Execute one backfill UPDATE and return rowcount; log + return 0 on error."""
        try:
            cur = conn.execute(sql, params)
            return cur.rowcount or 0
        except Exception as e:
            logger.warning("[LEARNING] backfill stmt failed: %s", e)
            return 0

    def backfill_org_ids(self, home_org_id: str | None) -> dict[str, int]:
        """Attribute pre-tenancy learning rows. Idempotent.

        Owner-linked rows map via test_runs.org_id (JOIN on workflow_id /
        source_workflow_id). Ownerless rows that are still NULL after the
        owner-linked pass fall back to home_org_id — the oldest personal org —
        so the current single tenant keeps the learning it already had.

        Runs on a dedicated short-lived connection (NOT the writer conn) with
        autocommit=True so each UPDATE commits independently. finally: conn.close().
        """
        out: dict[str, int] = {}
        conn = self._backfill_conn()
        try:
            owner_linked = {
                "execution_records":
                    "UPDATE execution_records e SET org_id = r.org_id FROM test_runs r "
                    "WHERE e.org_id IS NULL AND e.workflow_id = r.run_id AND r.org_id IS NOT NULL",
                "execution_embeddings":
                    "UPDATE execution_embeddings x SET org_id = r.org_id FROM test_runs r "
                    "WHERE x.org_id IS NULL AND x.workflow_id = r.run_id AND r.org_id IS NOT NULL",
                "nl_feedback_corrections":
                    "UPDATE nl_feedback_corrections n SET org_id = r.org_id FROM test_runs r "
                    "WHERE n.org_id IS NULL AND n.source_workflow_id = r.run_id AND r.org_id IS NOT NULL",
                # nl anchors can be owner-linked because nl_feedback_corrections was
                # just attributed above. anti-pattern anchors have no equivalent path
                # (anti_patterns carry no workflow->owner link), so both the
                # anti_patterns rows and their anchors are attributed by the home
                # fallback below — there is deliberately no "anchors_anti" entry here.
                "anchors_nl":
                    "UPDATE learning_anchors a SET org_id = n.org_id FROM nl_feedback_corrections n "
                    "WHERE a.org_id IS NULL AND a.kind = 'nl' AND a.record_id = n.id AND n.org_id IS NOT NULL",
            }
            for key, sql in owner_linked.items():
                out[key] = self._exec_backfill(conn, sql)
            if home_org_id:
                for tbl in ("execution_records", "execution_embeddings",
                            "nl_feedback_corrections", "anti_patterns", "learning_anchors"):
                    out[f"{tbl}_home"] = self._exec_backfill(
                        conn, f"UPDATE {tbl} SET org_id = ? WHERE org_id IS NULL", (home_org_id,))
        finally:
            conn.close()
        return out

    def close(self):
        try:
            # Backing attribute, NOT the property — the property would reopen
            # a dead connection just so close() could close it again.
            if self._writer:
                self._writer.close()
        finally:
            try:
                self._read_pool.close()
            except Exception:
                pass
