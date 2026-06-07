"""
Coverage tests for structural_rule_engine.py — targeting 6 missing lines.

Tests:
- IntentExtractor._check_learned_patterns: no execution_memory → returns [] immediately
- IntentExtractor._migrate_seed_to_learned: unknown intent_name not in SEED_PATTERNS → early return
- IntentExtractor._migrate_seed_to_learned: not on WRITER_THREAD → early return (no DB write)
- IntentExtractor._migrate_seed_to_learned: no execution_memory → early return
"""

import threading

import pytest

from src.backend.crew_ai.optimization.structural_rule_engine import IntentExtractor
from src.backend.crew_ai.optimization.learning_config import WRITER_THREAD_NAME


# ---------------------------------------------------------------------------
# _check_learned_patterns — no execution_memory
# ---------------------------------------------------------------------------

class TestCheckLearnedPatternsNoEM:
    def test_no_em_returns_empty_list(self):
        """IntentExtractor with no execution_memory must return [] from _check_learned_patterns."""
        extractor = IntentExtractor(execution_memory=None)
        result = extractor._check_learned_patterns("count all rows in table")
        assert result == []

    def test_no_em_extract_intents_falls_through_to_seed(self):
        """With no _em, extract_intents skips Layer 2 and goes directly to Layer 1 seed patterns."""
        extractor = IntentExtractor(execution_memory=None)
        # "count" matches the 'counting' seed pattern
        intents = extractor.extract_intents("how many rows exist")
        assert any(i["intent"] == "counting" for i in intents)


# ---------------------------------------------------------------------------
# _migrate_seed_to_learned — unknown intent_name guard
# ---------------------------------------------------------------------------

class TestMigrateSeedUnknownIntentName:
    def test_unknown_intent_name_returns_early(self, in_memory_em):
        """Calling _migrate_seed_to_learned with a name not in SEED_PATTERNS is a no-op."""
        extractor = IntentExtractor(execution_memory=in_memory_em)
        match = {
            "intent": "nonexistent_pattern",
            "triggered_by": ["trigger"],
            "initial_evidence": 2,
        }

        # Should not raise and must write nothing to DB
        extractor._migrate_seed_to_learned("nonexistent_pattern", match)

        with in_memory_em.read_conn() as conn:
            rows = conn.execute("SELECT * FROM intent_patterns").fetchall()
        assert rows == []


# ---------------------------------------------------------------------------
# _migrate_seed_to_learned — not on writer thread guard
# ---------------------------------------------------------------------------

class TestMigrateSeedNonWriterThread:
    def test_migration_skipped_from_non_writer_thread(self, in_memory_em):
        """_migrate_seed_to_learned is a no-op when called from a non-writer thread."""
        extractor = IntentExtractor(execution_memory=in_memory_em)
        match = {
            "intent": "iteration",
            "triggered_by": ["all rows"],
            "initial_evidence": 2,
        }

        wrote_to_db = []

        def run_from_reader_thread():
            # Thread name is NOT WRITER_THREAD_NAME → migration should be skipped
            extractor._migrate_seed_to_learned("iteration", match)
            wrote_to_db.append(True)

        t = threading.Thread(target=run_from_reader_thread, name="reader-thread")
        t.start()
        t.join()

        # run_from_reader_thread completed (no exception)
        assert wrote_to_db == [True]

        # Nothing was written to intent_patterns
        with in_memory_em.read_conn() as conn:
            rows = conn.execute("SELECT * FROM intent_patterns").fetchall()
        assert rows == []

    def test_migration_proceeds_on_writer_thread(self, in_memory_em):
        """Calling _migrate_seed_to_learned from the writer thread DOES write to DB."""
        # The conftest autouse fixture renames the current thread to WRITER_THREAD_NAME,
        # so this test runs on the writer thread by default.
        extractor = IntentExtractor(execution_memory=in_memory_em)
        match = {
            "intent": "iteration",
            "triggered_by": ["all rows"],
            "initial_evidence": 2,
        }

        extractor._migrate_seed_to_learned("iteration", match)

        with in_memory_em.read_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM intent_patterns WHERE intent_name = 'iteration'"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["source"] == "seed"


# ---------------------------------------------------------------------------
# _migrate_seed_to_learned — no execution_memory guard
# ---------------------------------------------------------------------------

class TestMigrateSeedNoEM:
    def test_no_em_returns_early_without_error(self):
        """_migrate_seed_to_learned with no _em is a silent no-op."""
        extractor = IntentExtractor(execution_memory=None)
        match = {
            "intent": "iteration",
            "triggered_by": ["all rows"],
            "initial_evidence": 2,
        }
        # Must not raise regardless of the calling thread's name
        extractor._migrate_seed_to_learned("iteration", match)
