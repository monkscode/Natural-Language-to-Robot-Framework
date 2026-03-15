"""
Database setup and verification script for execution_memory.db.

Usage:
    python scripts/migrate_pattern_learning.py
    python scripts/migrate_pattern_learning.py --verify-only

Steps:
1. Ensure data directory exists
2. Create/update execution_memory.db with latest schema via SchemaManager
3. Verify data integrity (tables, indexes, WAL mode)
4. Report status

Note: As of schema v4, keyword_stats (previously in separate pattern_learning.db)
is consolidated into execution_memory.db. QueryPatternMatcher now uses a shared
db_conn instead of its own database file.
"""

import sqlite3
import os
import sys

# Add project root to path so we can import schema_manager
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from src.backend.crew_ai.optimization.schema_manager import SchemaManager


# Paths
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "execution_memory.db")


def create_schema() -> bool:
    """Create or update execution_memory.db with latest schema via SchemaManager."""
    os.makedirs(DATA_DIR, exist_ok=True)

    if os.path.exists(DB_PATH):
        print(f"  {DB_PATH} exists — SchemaManager will apply pending migrations")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    try:
        version = SchemaManager.ensure_current(conn)
        print(f"  Schema created/updated — current version: v{version}")
        return True
    except Exception as e:
        print(f"  Schema creation failed: {e}")
        return False
    finally:
        conn.close()


def verify_integrity() -> bool:
    """Run integrity checks on the database."""
    if not os.path.exists(DB_PATH):
        print("  execution_memory.db does not exist")
        return False

    conn = sqlite3.connect(DB_PATH)
    all_passed = True

    # Check 1: PRAGMA integrity
    result = conn.execute("PRAGMA integrity_check").fetchone()
    ok = result and result[0] == "ok"
    print(f"  {'PASS' if ok else 'FAIL'} Integrity check: {result[0] if result else 'FAILED'}")
    if not ok:
        all_passed = False

    # Check 2: WAL mode
    journal = conn.execute("PRAGMA journal_mode").fetchone()
    ok = journal and journal[0] == "wal"
    print(f"  {'PASS' if ok else 'FAIL'} WAL mode: {journal[0] if journal else 'UNKNOWN'}")
    if not ok:
        all_passed = False

    # Check 3: Expected tables (all 10 tables from schema v1-v4)
    expected_tables = {
        "execution_records", "intent_patterns", "structural_rules",
        "keyword_corrections", "anti_patterns", "learning_stats",
        "schema_version", "nl_feedback_corrections", "learning_metrics",
        "keyword_stats",
    }
    tables = set(SchemaManager.get_table_names(conn))
    missing = expected_tables - tables
    extra = tables - expected_tables
    ok = len(missing) == 0
    print(f"  {'PASS' if ok else 'FAIL'} Tables ({len(tables)}): {sorted(tables)}")
    if missing:
        print(f"      Missing: {sorted(missing)}")
        all_passed = False
    if extra:
        print(f"      Extra (not a problem): {sorted(extra)}")

    # Check 4: Expected indexes
    expected_indexes = {
        "idx_exec_domain", "idx_exec_status", "idx_exec_failure",
        "idx_exec_timestamp", "idx_exec_structure",
        "idx_intent_score", "idx_intent_source",
        "idx_rules_score",
        "idx_anti_category", "idx_anti_domain",
        "idx_metrics_workflow", "idx_metrics_first_attempt", "idx_metrics_hints",
        "idx_nlfc_domain", "idx_nlfc_scope", "idx_nlfc_active",
        "idx_anti_score_evidence", "idx_nlfc_domain_scope",
        "idx_rules_score_evidence",
    }
    indexes = set(SchemaManager.get_index_names(conn))
    missing_idx = expected_indexes - indexes
    ok = len(missing_idx) == 0
    print(f"  {'PASS' if ok else 'FAIL'} Indexes ({len(indexes)}): {len(expected_indexes)} expected")
    if missing_idx:
        print(f"      Missing: {sorted(missing_idx)}")
        all_passed = False

    # Check 5: Schema version
    version = SchemaManager.get_current_version(conn)
    ok = version >= 4
    print(f"  {'PASS' if ok else 'FAIL'} Schema version: v{version} (expected >= 4)")
    if not ok:
        all_passed = False

    conn.close()
    return all_passed


def main():
    print("=" * 60)
    print("  Execution Memory Database Setup & Verification")
    print("=" * 60)

    if "--verify-only" in sys.argv:
        print("\n  Running VERIFY ONLY...\n")
        if verify_integrity():
            print("\n  All verification checks passed")
        else:
            print("\n  Some verification checks failed")
            sys.exit(1)
        return

    # Full setup
    print("\n  Step 1: Create/update schema...\n")
    if not create_schema():
        print("\n  Setup FAILED at schema creation")
        sys.exit(1)

    print("\n  Step 2: Verify integrity...\n")
    if verify_integrity():
        print("\n" + "=" * 60)
        print("  Setup complete!")
        print("=" * 60)
        print(f"\n  Database: {DB_PATH}")
    else:
        print("\n  Verification FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
