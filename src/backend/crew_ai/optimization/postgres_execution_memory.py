"""
PostgresExecutionMemory — PostgreSQL relational store for the learning system
(Phase 4 migration; the Postgres equivalent of execution_memory.ExecutionMemory).

The writer connection and read connections are pg_compat.CompatConnection
objects, so this class AND the learning engines (which reach into
`_writer_conn` / `read_conn()`) all speak SQLite-dialect SQL (`?` placeholders,
sqlite3.Row access, `except sqlite3.IntegrityError`) that the adapter translates
to psycopg/Postgres. Genuinely dialect-specific spots (RETURNING, json_each,
strftime, INSERT OR IGNORE) are ported at the call sites, not here.

Single-writer model preserved: ONE writer connection used only by the
learning-writer thread (LearningWriteQueue), guarded by _assert_writer_thread.
Reads use a pooled connection per call. ChromaDB (vectors) is reused as-is for
now and moves to pgvector in the vector slice.

Reuses ExecutionRecord / helpers from execution_memory.py.
Referenced by: learning_registry.py (after cutover).
"""

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

import psycopg
from psycopg_pool import ConnectionPool

from src.backend.core.config import settings
from src.backend.crew_ai.optimization import pg_compat, pg_schema
from src.backend.crew_ai.optimization.pg_compat import CompatConnection, compat_row
from src.backend.crew_ai.optimization.execution_memory import (
    ExecutionRecord,
    _assert_writer_thread,
    _mark,
)
from src.backend.crew_ai.optimization.learning_config import (
    LEARNING_CONFIG,
    ExecutionStore,
    SemanticStore,
)

logger = logging.getLogger(__name__)


class PostgresExecutionMemory(ExecutionStore, SemanticStore):
    """PostgreSQL + ChromaDB store for execution records (relational on Postgres)."""

    DEDUPLICATION_THRESHOLD = LEARNING_CONFIG["DEDUPLICATION_THRESHOLD"]

    _CHROMADB_INIT_FAILED = object()
    _CHROMA_RETRY_COOLDOWN_S: int = 300

    def __init__(self, dsn: str = None, chroma_dir: str = None):
        self.dsn = dsn or settings.DATABASE_URL
        self._chroma_dir = chroma_dir or LEARNING_CONFIG["CHROMADB_DIR"]

        # Schema bootstrap on a throwaway raw connection (pg_schema uses native
        # %s SQL, so it must NOT go through the ?-translating compat adapter).
        _setup = psycopg.connect(self.dsn, autocommit=True)
        try:
            pg_schema.ensure_schema(_setup)
        finally:
            _setup.close()

        # Writer: one compat connection, used only by the learning-writer thread.
        self._writer_conn = pg_compat.connect(self.dsn, autocommit=False)
        # Reads: pooled raw connections (compat_row factory) wrapped per borrow.
        self._read_pool = ConnectionPool(
            conninfo=self.dsn, min_size=1, max_size=10,
            kwargs={"row_factory": compat_row, "autocommit": True}, open=True,
        )

        # ChromaDB: lazy (vectors stay on Chroma until the pgvector slice).
        self._chroma_client = None
        self._execution_collection = None
        self._learning_anchors = None
        self._chroma_failed_at = None
        self._chroma_last_error = None
        self._chroma_init_lock = threading.Lock()
        self._last_trace_prune_ts = 0.0
        logger.debug("[LEARNING] PostgresExecutionMemory initialised")

    # -------------------------------------------------------------------
    # Connections (compat — engines reach into these directly)
    # -------------------------------------------------------------------

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
        self._store_chromadb(record)

    def store_execution(self, record: ExecutionRecord) -> None:
        _assert_writer_thread("PostgresExecutionMemory.store_execution")
        self.store(record)

    def _store_relational(self, record: ExecutionRecord) -> None:
        _assert_writer_thread("PostgresExecutionMemory._store_relational")
        normalized_query = record.user_query.strip().lower()
        domain = record.domain or ""
        try:
            run_count = self._writer_conn.execute(
                "SELECT COUNT(*) AS n FROM execution_records "
                "WHERE LOWER(TRIM(user_query)) = ? "
                "AND COALESCE(domain, '') = ? AND test_status = ?",
                (normalized_query, domain, record.test_status),
            ).fetchone()["n"]

            if run_count >= self.DEDUPLICATION_THRESHOLD:
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
            else:
                self._writer_conn.execute(
                    """
                    INSERT INTO execution_records (
                        workflow_id, timestamp, user_query, url, domain,
                        robot_code, code_structure, test_status, execution_exit_code,
                        execution_duration_ms, failure_category, failed_keyword,
                        error_message, total_llm_calls, total_cost, injected_hint_ids,
                        model_version, hint_attribution_done
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
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
            self._writer_conn.rollback()
            if "workflow_id" in str(e).lower() and record.test_status == "passed":
                self._update_to_passing_state(record.workflow_id, record.robot_code)
            else:
                raise
        except Exception:
            self._writer_conn.rollback()
            raise

    def _update_to_passing_state(self, workflow_id: str, working_code: str) -> None:
        _assert_writer_thread("PostgresExecutionMemory._update_to_passing_state")
        self._writer_conn.execute(
            "UPDATE execution_records "
            "SET working_code = ?, test_status = 'passed' WHERE workflow_id = ?",
            (working_code, workflow_id),
        )
        self._writer_conn.commit()
        logger.info("[LEARNING] Workflow %s updated to passing state", workflow_id)

    def update_user_feedback(self, workflow_id: str, feedback_text: str, feedback_type: str) -> None:
        _assert_writer_thread("PostgresExecutionMemory.update_user_feedback")
        self._writer_conn.execute(
            "UPDATE execution_records SET user_feedback = ?, user_feedback_type = ? "
            "WHERE workflow_id = ?",
            (feedback_text, feedback_type, workflow_id),
        )
        self._writer_conn.commit()

    def update_daily_stats(self, test_status: str) -> None:
        _assert_writer_thread("PostgresExecutionMemory.update_daily_stats")
        today = datetime.now().strftime("%Y-%m-%d")
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
            injected_hint_ids=row["injected_hint_ids"],
            model_version=row["model_version"],
            hint_attribution_done=row["hint_attribution_done"],
        )

    # -------------------------------------------------------------------
    # ChromaDB (vector) methods — reused as-is; move to pgvector later.
    # -------------------------------------------------------------------

    def _init_chromadb(self):
        if self._chromadb_available:
            return
        with self._chroma_init_lock:
            if self._chromadb_available:
                return
            if self._chroma_client is self._CHROMADB_INIT_FAILED:
                if self._chroma_failed_at is None:
                    return
                if time.monotonic() - self._chroma_failed_at < self._CHROMA_RETRY_COOLDOWN_S:
                    return
                self._chroma_client = None
            try:
                import chromadb
                from chromadb.config import Settings as ChromaSettings
                client = chromadb.PersistentClient(
                    path=self._chroma_dir, settings=ChromaSettings(anonymized_telemetry=False))
                self._execution_collection = client.get_or_create_collection(
                    name="execution_embeddings", metadata={"hnsw:space": "cosine"})
                self._learning_anchors = client.get_or_create_collection(
                    name="learning_anchors", metadata={"hnsw:space": "cosine"})
                self._chroma_client = client
                self._chroma_failed_at = None
                self._chroma_last_error = None
            except Exception as e:
                logger.error("[LEARNING] ChromaDB init failed: %s", e)
                self._chroma_client = self._CHROMADB_INIT_FAILED
                self._execution_collection = None
                self._learning_anchors = None
                self._chroma_failed_at = time.monotonic()
                self._chroma_last_error = str(e)

    @property
    def _chromadb_available(self) -> bool:
        return (self._chroma_client is not None
                and self._chroma_client is not self._CHROMADB_INIT_FAILED
                and self._execution_collection is not None)

    def _store_chromadb(self, record: ExecutionRecord) -> None:
        _assert_writer_thread("PostgresExecutionMemory._store_chromadb")
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
                documents=[record.user_query], ids=[record.workflow_id], metadatas=[metadata])
        except Exception:
            try:
                self._execution_collection.upsert(
                    documents=[record.user_query], ids=[record.workflow_id], metadatas=[metadata])
            except Exception as upsert_err:
                logger.warning("[LEARNING] ChromaDB store failed (non-blocking): %s", upsert_err)

    def find_similar_executions(self, user_query: str, top_k: int = 5) -> list:
        self._init_chromadb()
        if not self._chromadb_available:
            return []
        try:
            return self._execution_collection.query(query_texts=[user_query], n_results=top_k)
        except Exception as e:
            logger.warning("[LEARNING] ChromaDB search failed: %s", e)
            return []

    def store_embedding(self, text: str, metadata: dict) -> None:
        self._init_chromadb()
        if not self._chromadb_available:
            return
        try:
            self._execution_collection.add(
                documents=[text], ids=[metadata.get("workflow_id", str(id(text)))], metadatas=[metadata])
        except Exception as e:
            logger.warning("[LEARNING] ChromaDB store_embedding failed: %s", e)

    def search_similar(self, query: str, top_k: int = 5) -> list:
        return self.find_similar_executions(query, top_k)

    def filter_by_query_similarity(self, user_query, candidate_ids, kind,
                                   threshold=0.55, score_sink=None) -> set:
        if not user_query or not user_query.strip() or not candidate_ids:
            return set()
        _mark(score_sink, candidate_ids, "no_anchor")
        self._init_chromadb()
        coll = self._learning_anchors
        if coll is None:
            _mark(score_sink, candidate_ids, "fail_open")
            return set(candidate_ids)
        try:
            expected_ids = [f"{kind}:{rid}" for rid in candidate_ids]
            present = coll.get(ids=expected_ids, include=[])
            n = len(present["ids"])
            if n == 0:
                if coll.count() == 0:
                    _mark(score_sink, candidate_ids, "fail_open")
                    return set(candidate_ids)
                return set()
            result = coll.query(
                query_texts=[user_query], n_results=n,
                where={"$and": [{"kind": kind}, {"record_id": {"$in": list(candidate_ids)}}]},
                include=["metadatas", "distances"])
        except Exception as e:
            logger.warning("[LEARNING] similarity filter query failed (fail-open): %s", e)
            _mark(score_sink, candidate_ids, "fail_open")
            return set(candidate_ids)
        survivors: set = set()
        metas = result["metadatas"][0] if result.get("metadatas") else []
        dists = result["distances"][0] if result.get("distances") else []
        for meta, dist in zip(metas, dists):
            rid = meta.get("record_id")
            if rid is None:
                continue
            sim = 1.0 - dist
            if sim >= threshold:
                survivors.add(rid)
                if score_sink is not None:
                    score_sink[rid] = {"sim": sim, "outcome": "survived"}
            elif score_sink is not None:
                score_sink[rid] = {"sim": sim, "outcome": "similarity_below"}
        return survivors

    def add_anchor(self, kind: str, record_id: int, anchor_query: str) -> None:
        _assert_writer_thread("PostgresExecutionMemory.add_anchor")
        if not anchor_query or not anchor_query.strip():
            return
        self._init_chromadb()
        coll = self._learning_anchors
        if coll is None:
            return
        try:
            coll.upsert(ids=[f"{kind}:{record_id}"], documents=[anchor_query],
                        metadatas=[{"kind": kind, "record_id": record_id}])
        except Exception as e:
            logger.warning("[LEARNING] add_anchor failed (non-blocking): %s", e)

    def reconcile_anchors(self) -> dict:
        _assert_writer_thread("PostgresExecutionMemory.reconcile_anchors")
        ran_at = datetime.now(timezone.utc).isoformat()
        self._init_chromadb()
        coll = self._learning_anchors
        if coll is None:
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
            existing = set(coll.get(ids=[d[0] for d in desired], include=[])["ids"])
        except Exception as e:
            logger.error("[LEARNING] reconcile_anchors get() failed: %s", e)
            return {"ran_at": ran_at, "checked": checked, "missing_count": checked}
        missing = [d for d in desired if d[0] not in existing]
        if not missing:
            return {"ran_at": ran_at, "checked": checked, "missing_count": 0}
        try:
            coll.upsert(ids=[d[0] for d in missing], documents=[d[3] for d in missing],
                        metadatas=[{"kind": d[2], "record_id": d[1]} for d in missing])
            return {"ran_at": ran_at, "checked": checked, "missing_count": 0}
        except Exception as e:
            logger.error("[LEARNING] reconcile_anchors upsert failed: %s", e)
            return {"ran_at": ran_at, "checked": checked, "missing_count": len(missing)}

    def close(self):
        try:
            if self._writer_conn:
                self._writer_conn.close()
        finally:
            try:
                self._read_pool.close()
            except Exception:
                pass
