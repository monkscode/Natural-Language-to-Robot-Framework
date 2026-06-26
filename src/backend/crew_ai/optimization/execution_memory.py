"""
Shared learning primitives — writer-thread guard, ExecutionRecord, code analysis.

This module formerly housed the SQLite + ChromaDB `ExecutionMemory` store. That
store was replaced by `PostgresExecutionMemory` (postgres_execution_memory.py)
in the Phase-4 Postgres consolidation and the class has been removed. What
remains are the storage-agnostic primitives shared by the live learning code:

- _assert_writer_thread: keeps every learning write on the LearningWriteQueue
  worker thread (single-writer model) so bypassing the queue fails loudly.
- _mark: score-sink helper for the F1/N3 retrieval observability.
- ExecutionRecord: the dataclass every store/engine exchanges.
- CodeStructureExtractor: robot-code structure detection.

Referenced by: postgres_execution_memory.py, feedback_loop.py, the learning
engines (anti_pattern / keyword_correction / nl_feedback / structural_rule),
failure_analyzer.py.
Depends on: learning_config.py (WRITER_THREAD_NAME).
"""

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from src.backend.crew_ai.optimization.learning_config import WRITER_THREAD_NAME


def _assert_writer_thread(method_name: str) -> None:
    """Guard for engine/store write methods.

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


def _mark(sink: dict | None, ids, outcome: str) -> None:
    """Record a "no real score" outcome for a batch of candidate ids in the
    opt-in score_sink (F1 / N3 observability). No-op when sink is None.

    Used for the pre-seed (no_anchor) and the fail-open returns; the scoring
    loop writes real sims inline. Outcome ∈ {no_anchor, fail_open}.
    """
    if sink is not None:
        for rid in ids:
            sink[rid] = {"sim": None, "outcome": outcome}


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

    # Org that owns this execution (Phase 1c). NULL for rows written before
    # schema v14 or for single-org deployments. The dedup path is scoped on
    # COALESCE(org_id, '') so a NULL-org row and an org-A row never merge.
    org_id: Optional[str] = None

    # Per-workflow once-guard for pass-time usage attribution (Part 2 / C1).
    # The DB column DEFAULT is 1 (pre-v13 rows read already-attributed — N4);
    # this dataclass default is intentionally 0 and must NOT be unified with the
    # DB default. _store_sqlite's INSERT writes 0 for new rows so they attribute;
    # apply_hint_attribution's atomic claim flips the winner to 1. Read-only here
    # (the Step-4 attribution gate consumes it); no engine writes it via this field.
    hint_attribution_done: int = 0


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
