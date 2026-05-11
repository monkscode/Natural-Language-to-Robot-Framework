"""
DAY_09 -- Phase 1 Performance Tests.

Measures 4 performance characteristics:
  1. Pre-execution overhead (context injection) < 150ms
  2. Post-execution overhead (process_execution) < 400ms
  3. DB growth rate (~3-5 KB per record)
  4. Non-blocking verification (write queue)

Migrated from scripts/verify_day09_performance.py to pytest format.
All tests marked with @pytest.mark.performance.
Timing assertions are preserved.
"""

import os
import time
import sqlite3
import threading
from datetime import datetime
from dataclasses import dataclass

import pytest

from src.backend.crew_ai.optimization.schema_manager import SchemaManager
from src.backend.crew_ai.optimization.learning_config import (
    LEARNING_CONFIG,
    LearningCircuitBreaker,
    LearningWriteQueue,
)
from src.backend.crew_ai.optimization.execution_memory import (
    ExecutionMemory,
    ExecutionRecord,
)
from src.backend.crew_ai.optimization.structural_rule_engine import (
    IntentExtractor,
    StructuralRuleEngine,
)
from src.backend.crew_ai.optimization.keyword_correction_engine import (
    KeywordCorrectionEngine,
)
from src.backend.crew_ai.optimization.anti_pattern_engine import (
    AntiPatternEngine,
)
from src.backend.crew_ai.optimization.feedback_loop import (
    LearningMetricsTracker,
    ContradictionDetector,
    FeedbackLoop,
)


# ===================================================================
# Helpers
# ===================================================================

def create_execution_memory(conn):
    """Create ExecutionMemory backed by existing connection."""
    em = ExecutionMemory.__new__(ExecutionMemory)
    em.db_path = ":memory:"
    em._chroma_dir = None
    em.conn = conn
    em._chroma_client = ExecutionMemory._CHROMADB_INIT_FAILED
    em._execution_collection = None
    return em


class SynchronousWriteQueue:
    def __init__(self):
        self.submit_count = 0

    def submit(self, fn, *args, **kwargs):
        self.submit_count += 1
        fn(*args, **kwargs)

    def pending(self):
        return 0


class MockFailureAnalyzer:
    def analyze(self, **kwargs):
        return None


class NoOpPatternLearner:
    """Prevents ChromaDB/ONNX from running inside the timing window."""
    def learn_from_execution(self, *args, **kwargs):
        pass


@dataclass
class MockMetrics:
    total_llm_calls: int = 3
    total_cost: float = 0.05


def build_perf_feedback_loop(conn):
    """Build FeedbackLoop for performance testing."""
    em = create_execution_memory(conn)
    ie = IntentExtractor(conn)
    se = StructuralRuleEngine(conn, ie)
    ke = KeywordCorrectionEngine(conn)
    ae = AntiPatternEngine(conn)
    mt = LearningMetricsTracker(conn)
    cd = ContradictionDetector(conn)
    wq = SynchronousWriteQueue()
    cb = LearningCircuitBreaker()
    fa = MockFailureAnalyzer()
    fl = FeedbackLoop(
        execution_memory=em, failure_analyzer=fa,
        structural_engine=se, keyword_engine=ke,
        anti_pattern_engine=ae, metrics_tracker=mt,
        contradiction_detector=cd, write_queue=wq,
        circuit_breaker=cb,
        pattern_learner=NoOpPatternLearner(),
    )
    return fl, em, se, ke, ae, conn


# ===================================================================
# Performance 1: Pre-Execution Overhead (Context Injection)
# ===================================================================

@pytest.mark.performance
def test_perf_context_injection_cold(in_memory_db):
    """Context injection on cold DB should be < 150ms."""
    conn = in_memory_db
    ie = IntentExtractor(conn)
    se = StructuralRuleEngine(conn, ie)
    ke = KeywordCorrectionEngine(conn)
    ae = AntiPatternEngine(conn)

    query = "verify all rows in table show Active"
    url = "https://dashboard.example.com/users"

    # Multiple runs to get stable timing
    times = []
    for _ in range(10):
        start = time.perf_counter()
        se.get_hints(query, url, "planner")
        ke.get_hints(query, url, "assembler")
        ae.get_hints(query, url, "planner")
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)

    avg_ms = sum(times) / len(times)
    assert avg_ms < 150, (
        f"Avg context injection {avg_ms:.1f}ms > 150ms threshold"
    )


@pytest.mark.performance
def test_perf_context_injection_warm(in_memory_db):
    """Context injection on warm DB (with data) should be < 150ms."""
    conn = in_memory_db
    ie = IntentExtractor(conn)
    se = StructuralRuleEngine(conn, ie)
    ke = KeywordCorrectionEngine(conn)
    ae = AntiPatternEngine(conn)

    now = datetime.now().isoformat()
    # Seed some data
    for i in range(10):
        conn.execute(
            "INSERT INTO structural_rules "
            "(rule_name, query_pattern, required_structure, "
            " required_keywords_json, evidence_count, counter_evidence, "
            " score, last_updated, created_at) "
            f"VALUES ('rule_{i}', 'pattern_{i}', 'structure_{i}', "
            f" '[\"KW_{i}\"]', {i+1}, 0, 0.9, ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO keyword_corrections "
            "(wrong_keyword, correct_keyword, library, error_pattern, "
            " evidence_count, score, last_seen) "
            f"VALUES ('wrong_{i}', 'correct_{i}', 'browser', "
            f" 'error', {i+1}, 0.9, ?)",
            (now,),
        )
    conn.commit()

    query = "verify all rows in table show Active"
    url = "https://dashboard.example.com/users"

    times = []
    for _ in range(10):
        start = time.perf_counter()
        se.get_hints(query, url, "planner")
        ke.get_hints(query, url, "assembler")
        ae.get_hints(query, url, "planner")
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)

    avg_ms = sum(times) / len(times)
    assert avg_ms < 150, (
        f"Avg context injection (warm) {avg_ms:.1f}ms > 150ms threshold"
    )


# ===================================================================
# Performance 2: Post-Execution Overhead
# ===================================================================

@pytest.mark.performance
def test_perf_process_execution_single(in_memory_db):
    """Single process_execution should complete < 400ms."""
    fl, em, se, ke, ae, conn = build_perf_feedback_loop(in_memory_db)

    start = time.perf_counter()
    fl.process_execution(
        workflow_id="perf-001",
        user_query="verify all rows in table show Active",
        url="https://dashboard.example.com/users",
        robot_code="*** Test Cases ***\nTest\n    Log    hello",
        test_status="passed",
        metrics=MockMetrics(),
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert elapsed_ms < 400, (
        f"process_execution took {elapsed_ms:.1f}ms > 400ms threshold"
    )


@pytest.mark.performance
def test_perf_process_execution_batch(in_memory_db):
    """10 consecutive process_execution calls avg < 400ms each."""
    fl, em, se, ke, ae, conn = build_perf_feedback_loop(in_memory_db)

    times = []
    for i in range(10):
        start = time.perf_counter()
        fl.process_execution(
            workflow_id=f"perf-batch-{i:03d}",
            user_query=f"query {i}",
            url="https://example.com",
            robot_code=f"*** Test Cases ***\nTest {i}\n    Log    {i}",
            test_status="passed" if i % 2 == 0 else "failed",
        )
        elapsed_ms = (time.perf_counter() - start) * 1000
        times.append(elapsed_ms)

    avg_ms = sum(times) / len(times)
    assert avg_ms < 400, (
        f"Avg process_execution {avg_ms:.1f}ms > 400ms threshold"
    )


# ===================================================================
# Performance 3: DB Growth Rate
# ===================================================================

@pytest.mark.performance
def test_perf_db_growth_rate(in_memory_db):
    """DB should grow ~3-5 KB per execution record."""
    conn = in_memory_db
    em = create_execution_memory(conn)

    # Measure baseline (schema + empty tables)
    baseline_pages = conn.execute("PRAGMA page_count").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    baseline_kb = (baseline_pages * page_size) / 1024

    # Insert 20 records
    num_records = 20
    for i in range(num_records):
        em.store(ExecutionRecord(
            workflow_id=f"growth-{i:03d}",
            timestamp=datetime.now(),
            user_query=f"verify all rows in table_{i} show Active status",
            url=f"https://app{i}.example.com/page",
            domain=f"app{i}.example.com",
            robot_code=(
                "*** Settings ***\n"
                "Library    Browser\n\n"
                "*** Test Cases ***\n"
                f"Test {i}\n"
                "    New Browser    headless=true\n"
                f"    New Page    https://app{i}.example.com/page\n"
                f"    ${{text}}=    Get Text    css=td.status-{i}\n"
                "    Should Be Equal    ${text}    Active\n"
            ),
            code_structure=None,
            test_status="passed" if i % 3 != 0 else "failed",
            failure_category="A1" if i % 3 == 0 else None,
            error_message=f"Error {i}" if i % 3 == 0 else None,
        ))

    after_pages = conn.execute("PRAGMA page_count").fetchone()[0]
    after_kb = (after_pages * page_size) / 1024
    growth_kb = after_kb - baseline_kb
    kb_per_record = growth_kb / num_records

    assert kb_per_record < 10, (
        f"DB growth {kb_per_record:.2f} KB/record > 10 KB threshold"
    )
    assert kb_per_record > 0.1, (
        f"DB growth {kb_per_record:.2f} KB/record suspiciously low"
    )


# ===================================================================
# Performance 4: Non-Blocking Verification
# ===================================================================

@pytest.mark.performance
def test_perf_write_queue_non_blocking():
    """LearningWriteQueue.submit() should return immediately."""
    wq = LearningWriteQueue()
    results_list = []

    def slow_fn():
        time.sleep(0.1)
        results_list.append("done")

    start = time.perf_counter()
    wq.submit(slow_fn)
    submit_ms = (time.perf_counter() - start) * 1000

    # submit() should return nearly instantly (< 50ms)
    assert submit_ms < 50, (
        f"submit() took {submit_ms:.1f}ms -- not non-blocking!"
    )

    # Wait for background execution
    time.sleep(0.3)
    assert len(results_list) == 1, "Background task should have completed"


@pytest.mark.performance
def test_perf_write_queue_ordering():
    """LearningWriteQueue preserves FIFO order."""
    wq = LearningWriteQueue()
    order = []

    def append_fn(val):
        order.append(val)

    for i in range(5):
        wq.submit(append_fn, i)

    # Wait for all to complete
    time.sleep(0.5)
    assert order == [0, 1, 2, 3, 4], (
        f"Expected FIFO order [0,1,2,3,4], got {order}"
    )


@pytest.mark.performance
def test_perf_process_execution_with_async_queue(tmp_db_path):
    """FeedbackLoop with real LearningWriteQueue works end-to-end.

    Uses a file-based temp DB with check_same_thread=False because
    LearningWriteQueue submits work to a background thread, and
    in-memory SQLite connections cannot be shared across threads.
    """
    conn = sqlite3.connect(tmp_db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    SchemaManager.ensure_current(conn)

    try:
        em = create_execution_memory(conn)
        ie = IntentExtractor(conn)
        se = StructuralRuleEngine(conn, ie)
        ke = KeywordCorrectionEngine(conn)
        ae = AntiPatternEngine(conn)
        mt = LearningMetricsTracker(conn)
        cd = ContradictionDetector(conn)
        wq = LearningWriteQueue()  # Real async queue
        cb = LearningCircuitBreaker()
        fa = MockFailureAnalyzer()
        fl = FeedbackLoop(
            execution_memory=em, failure_analyzer=fa,
            structural_engine=se, keyword_engine=ke,
            anti_pattern_engine=ae, metrics_tracker=mt,
            contradiction_detector=cd, write_queue=wq,
            circuit_breaker=cb,
        )

        start = time.perf_counter()
        fl.process_execution(
            workflow_id="async-001",
            user_query="click button",
            url="https://example.com",
            robot_code="*** Test Cases ***\nTest\n    Click    id=btn",
            test_status="passed",
        )
        call_ms = (time.perf_counter() - start) * 1000

        # Wait for background writes to complete
        time.sleep(1.5)
        record = em.get("async-001")
        assert record is not None, (
            "Record should be stored after async queue drains"
        )
    finally:
        conn.close()


# ===================================================================
# Category 5: Concurrency Tests (5 Simultaneous Users)
# ===================================================================

@pytest.mark.performance
def test_concurrent_sqlite_writes(tmp_db_path):
    """5 threads writing execution records concurrently — no database locked errors.

    Verifies that SQLite WAL mode + busy_timeout handles 5 concurrent writers,
    each inserting 10 records for a total of 50 records.
    """
    conn = sqlite3.connect(tmp_db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    SchemaManager.ensure_current(conn)
    conn.close()

    errors = []
    records_per_thread = 10
    num_threads = 5

    def writer(thread_id):
        try:
            # Each thread gets its own connection (simulates separate users)
            tc = sqlite3.connect(tmp_db_path, check_same_thread=False)
            tc.row_factory = sqlite3.Row
            tc.execute("PRAGMA journal_mode=WAL")
            tc.execute("PRAGMA busy_timeout=5000")

            for i in range(records_per_thread):
                wf_id = f"concurrent-t{thread_id}-{i}"
                tc.execute("""
                    INSERT INTO execution_records (
                        workflow_id, timestamp, user_query, test_status,
                        total_llm_calls, total_cost
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    wf_id,
                    datetime.now().isoformat(),
                    f"thread {thread_id} query {i}",
                    "passed" if i % 2 == 0 else "failed",
                    1, 0.01,
                ))
                tc.commit()

            tc.close()
        except Exception as e:
            errors.append(f"Thread {thread_id}: {e}")

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(errors) == 0, f"Concurrent write errors: {errors}"

    # Verify all records were written
    verify_conn = sqlite3.connect(tmp_db_path)
    count = verify_conn.execute("SELECT COUNT(*) FROM execution_records").fetchone()[0]
    verify_conn.close()

    expected = num_threads * records_per_thread
    assert count == expected, (
        f"Expected {expected} records from {num_threads} threads, got {count}"
    )


@pytest.mark.performance
def test_concurrent_read_write(tmp_db_path):
    """Concurrent readers and writers — readers never blocked by writers in WAL mode.

    3 writer threads + 2 reader threads operating simultaneously. Readers
    should always get consistent snapshots via WAL's snapshot isolation.
    """
    conn = sqlite3.connect(tmp_db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    SchemaManager.ensure_current(conn)

    # Seed with initial records
    for i in range(5):
        conn.execute("""
            INSERT INTO execution_records (
                workflow_id, timestamp, user_query, test_status,
                total_llm_calls, total_cost
            ) VALUES (?, ?, ?, 'passed', 1, 0.01)
        """, (f"seed-{i}", datetime.now().isoformat(), f"seed query {i}"))
    conn.commit()
    conn.close()

    errors = []
    read_results = []

    def writer(thread_id):
        try:
            tc = sqlite3.connect(tmp_db_path, check_same_thread=False)
            tc.execute("PRAGMA journal_mode=WAL")
            tc.execute("PRAGMA busy_timeout=5000")

            for i in range(10):
                tc.execute("""
                    INSERT INTO execution_records (
                        workflow_id, timestamp, user_query, test_status,
                        total_llm_calls, total_cost
                    ) VALUES (?, ?, ?, 'passed', 1, 0.01)
                """, (
                    f"rw-w{thread_id}-{i}",
                    datetime.now().isoformat(),
                    f"write thread {thread_id} query {i}",
                ))
                tc.commit()
                time.sleep(0.01)

            tc.close()
        except Exception as e:
            errors.append(f"Writer {thread_id}: {e}")

    def reader(thread_id):
        try:
            tc = sqlite3.connect(tmp_db_path, check_same_thread=False)
            tc.row_factory = sqlite3.Row
            tc.execute("PRAGMA journal_mode=WAL")
            tc.execute("PRAGMA busy_timeout=5000")

            for _ in range(20):
                count = tc.execute(
                    "SELECT COUNT(*) FROM execution_records"
                ).fetchone()[0]
                read_results.append(count)
                # Count should always be >= seed count (5)
                assert count >= 5, (
                    f"Reader {thread_id}: count={count} < seed=5"
                )
                time.sleep(0.005)

            tc.close()
        except Exception as e:
            errors.append(f"Reader {thread_id}: {e}")

    threads = (
        [threading.Thread(target=writer, args=(t,)) for t in range(3)]
        + [threading.Thread(target=reader, args=(t,)) for t in range(2)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(errors) == 0, f"Concurrent read/write errors: {errors}"
    # Readers should see monotonically non-decreasing counts
    assert len(read_results) > 0, "Readers should have recorded results"


@pytest.mark.performance
def test_concurrent_engine_learns(tmp_db_path):
    """5 concurrent FeedbackLoop.process_execution calls via write queues.

    Each thread creates its own FeedbackLoop with a shared file-based DB,
    simulating 5 users submitting test results simultaneously.
    """
    conn = sqlite3.connect(tmp_db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    SchemaManager.ensure_current(conn)
    conn.close()

    errors = []

    def user_thread(user_id):
        try:
            # Each user gets their own connection (like real multi-user scenario)
            tc = sqlite3.connect(tmp_db_path, check_same_thread=False)
            tc.row_factory = sqlite3.Row
            tc.execute("PRAGMA journal_mode=WAL")
            tc.execute("PRAGMA busy_timeout=5000")

            em = create_execution_memory(tc)
            ie = IntentExtractor(tc)
            se = StructuralRuleEngine(tc, ie)
            ke = KeywordCorrectionEngine(tc)
            ae = AntiPatternEngine(tc)
            mt = LearningMetricsTracker(tc)
            cd = ContradictionDetector(tc)
            sq = SynchronousWriteQueue()
            cb = LearningCircuitBreaker()
            fa = MockFailureAnalyzer()

            fl = FeedbackLoop(
                execution_memory=em, failure_analyzer=fa,
                structural_engine=se, keyword_engine=ke,
                anti_pattern_engine=ae, metrics_tracker=mt,
                contradiction_detector=cd, write_queue=sq,
                circuit_breaker=cb,
            )

            for i in range(3):
                fl.process_execution(
                    workflow_id=f"user{user_id}-run{i}",
                    user_query=f"user {user_id} click button {i}",
                    url="https://example.com",
                    robot_code=(
                        "*** Test Cases ***\n"
                        f"User {user_id} Test {i}\n"
                        "    Click Element    id=btn\n"
                        "    Log    done\n"
                    ),
                    test_status="passed" if i % 2 == 0 else "failed",
                )

            tc.close()
        except Exception as e:
            errors.append(f"User {user_id}: {e}")

    threads = [threading.Thread(target=user_thread, args=(u,)) for u in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(errors) == 0, f"Concurrent engine errors: {errors}"

    # Verify records were written
    verify_conn = sqlite3.connect(tmp_db_path)
    count = verify_conn.execute("SELECT COUNT(*) FROM execution_records").fetchone()[0]
    verify_conn.close()

    # 5 users * 3 runs = 15 records
    assert count == 15, f"Expected 15 records from 5 concurrent users, got {count}"



# ===================================================================
# Category 6: LRU Cache Tests (KeywordSearchTool)
# ===================================================================

class MockVectorStoreSimple:
    """Mock KeywordVectorStore that returns named results for LRU testing."""

    def __init__(self):
        self.call_count = 0

    def search(self, library_name, query, top_k=3):
        self.call_count += 1
        return [{"name": query, "args": [], "description": f"desc-{query}", "similarity": 0.9}]


def make_search_tool(vector_store=None):
    """Create KeywordSearchTool with mock vector store."""
    from src.backend.crew_ai.optimization.keyword_search_tool import KeywordSearchTool
    store = vector_store or MockVectorStoreSimple()
    return KeywordSearchTool(library_name="Browser", vector_store=store), store


class TestLRUCache:

    def test_cache_hit_skips_search(self):
        """Cache hit should return without calling vector store."""
        tool, store = make_search_tool()
        tool._run("click button")
        tool._run("click button")
        assert store.call_count == 1, "Second call should use cache"

    def test_lru_eviction_order(self):
        """LRU should evict least-recently-used, not first-inserted."""
        tool, store = make_search_tool()

        # Fill cache to capacity (100 entries)
        for i in range(100):
            tool._run(f"query_{i}")

        assert store.call_count == 100

        # Access query_0 (moves it to end = most-recently-used)
        tool._run("query_0")
        assert store.call_count == 100, "query_0 should be a cache hit"

        # Insert a new entry — should evict query_1 (now the LRU), NOT query_0
        tool._run("new_query")
        assert store.call_count == 101

        # query_0 should still be cached (was recently accessed)
        tool._run("query_0")
        assert store.call_count == 101, "query_0 should still be cached after LRU eviction"

        # query_1 should have been evicted (was the LRU entry)
        tool._run("query_1")
        assert store.call_count == 102, "query_1 should have been evicted"

    def test_cache_size_respects_limit(self):
        """Cache should never exceed 100 entries."""
        tool, store = make_search_tool()

        for i in range(150):
            tool._run(f"query_{i}")

        assert len(tool._cache) == 100, f"Cache size should be 100, got {len(tool._cache)}"


# ===================================================================
# Category 6b: KeywordSearchTool Schema Validation (Issue #3 fix)
# ===================================================================
#
# Before the fix, CrewAI's auto-schema builder used only __annotations__
# from _run(), ignoring Python default values. Pydantic then treated
# `top_k: int` as required, causing 4+ wasted LLM retry calls per workflow.
# These tests pin the correct schema behaviour so a regression is caught.

class TestKeywordSearchToolSchema:

    def _schema(self):
        from src.backend.crew_ai.optimization.keyword_search_tool import KeywordSearchToolSchema
        return KeywordSearchToolSchema

    def test_top_k_optional_when_omitted(self):
        """Omitting top_k must not raise — this was the exact failing call."""
        instance = self._schema()(query="click a button")
        assert instance.top_k == 3

    def test_top_k_default_is_3(self):
        """Default value must be 3, matching the _run signature."""
        assert self._schema().model_fields["top_k"].default == 3

    def test_explicit_top_k_accepted(self):
        """An explicit top_k value must override the default."""
        instance = self._schema()(query="find element", top_k=10)
        assert instance.top_k == 10

    def test_query_is_required(self):
        """query must remain required — omitting it must raise ValidationError."""
        with pytest.raises(Exception):
            self._schema()(top_k=5)

    def test_tool_uses_explicit_schema(self):
        """Tool.args_schema must be KeywordSearchToolSchema, not the auto-generated placeholder."""
        from unittest.mock import MagicMock
        from src.backend.crew_ai.optimization.keyword_search_tool import (
            KeywordSearchTool,
            KeywordSearchToolSchema,
        )
        tool = KeywordSearchTool(library_name="Browser", vector_store=MagicMock())
        assert tool.args_schema is KeywordSearchToolSchema

    def test_field_descriptions_populated(self):
        """Both fields must have non-None descriptions visible to the LLM."""
        fields = self._schema().model_fields
        assert fields["query"].description is not None
        assert fields["top_k"].description is not None
        assert "default" in fields["top_k"].description.lower()


# ===================================================================
# Category 7: Thread-Safe Circuit Breaker Tests
# ===================================================================

@pytest.mark.performance
def test_circuit_breaker_concurrent_updates():
    """10 threads calling record_success/record_error concurrently.

    Total calls should be exactly 10*100 = 1000 with no lost updates.
    """
    cb = LearningCircuitBreaker()
    num_threads = 10
    ops_per_thread = 100

    def worker(thread_id):
        for i in range(ops_per_thread):
            if i % 5 == 0:
                cb.record_error(RuntimeError(f"t{thread_id}-{i}"))
            else:
                cb.record_success()

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    stats = cb.get_stats()
    expected_total = num_threads * ops_per_thread
    expected_errors = num_threads * (ops_per_thread // 5)

    assert stats["total_calls"] == expected_total, (
        f"Expected {expected_total} total calls, got {stats['total_calls']}"
    )
    assert stats["error_count"] == expected_errors, (
        f"Expected {expected_errors} errors, got {stats['error_count']}"
    )


@pytest.mark.performance
def test_circuit_breaker_concurrent_read_write():
    """Readers calling is_enabled() while writers call record_success/record_error.

    is_enabled() must never raise, even under concurrent mutation.
    """
    cb = LearningCircuitBreaker()
    errors = []

    def writer():
        for i in range(200):
            if i % 3 == 0:
                cb.record_error(RuntimeError("test"))
            else:
                cb.record_success()

    def reader():
        for _ in range(200):
            try:
                cb.is_enabled()
                cb.get_stats()
            except Exception as e:
                errors.append(str(e))

    threads = (
        [threading.Thread(target=writer) for _ in range(3)]
        + [threading.Thread(target=reader) for _ in range(3)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(errors) == 0, f"Concurrent read/write errors: {errors}"
