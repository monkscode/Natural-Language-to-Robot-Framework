"""
Migrate ChromaDB collections from sentence_transformers to ONNX embeddings.

Problem:
  The learning_chromadb was created locally with sentence_transformers (all-MiniLM-L6-v2).
  Docker only has ChromaDB's default ONNX embedding. When Docker mounts the host's
  data/ directory, ChromaDB tries to load sentence_transformers from the persisted
  collection metadata and fails.

Solution:
  All ChromaDB data is derived from SQLite (execution_memory.db). This script:
  1. Reads all execution records from SQLite (the source of truth)
  2. Deletes the stale learning_chromadb/ directory
  3. Recreates it with ChromaDB's default ONNX embedding
  4. Re-inserts all execution embeddings
  5. Rebuilds query_patterns in chroma_db/ from robot_code in SQLite

Usage:
    python scripts/migrate_chromadb_to_onnx.py
    python scripts/migrate_chromadb_to_onnx.py --dry-run

Safe to run multiple times (idempotent).
"""

import os
import re
import sys
import json
import shutil
import sqlite3
import logging

# Add project root to path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Paths (match learning_config.py)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
EXECUTION_DB = os.path.join(DATA_DIR, "execution_memory.db")
LEARNING_CHROMADB_DIR = os.path.join(DATA_DIR, "learning_chromadb")
OPTIMIZATION_CHROMADB_DIR = os.path.join(PROJECT_ROOT, "chroma_db")


def extract_keywords_from_code(code: str) -> list:
    """Extract Robot Framework keywords from code. Mirrors QueryPatternMatcher logic."""
    keywords = set()
    in_test_cases = False

    for line in code.split('\n'):
        stripped = line.strip()

        if stripped.startswith('***') and 'Test Cases' in stripped:
            in_test_cases = True
            continue
        elif stripped.startswith('***'):
            in_test_cases = False
            continue

        if not in_test_cases:
            continue
        if not line.startswith('    ') and not line.startswith('\t'):
            continue
        if stripped.startswith('#') or stripped.startswith('['):
            continue

        parts = [p.strip() for p in re.split(r'    |\t', stripped) if p.strip()]
        if not parts:
            continue

        first_part = parts[0]
        if '=' in first_part and first_part.strip().startswith('${'):
            if len(parts) > 1:
                keyword = parts[1]
                if not keyword.startswith('${') and not keyword.startswith('@{'):
                    keywords.add(keyword)
        else:
            if not first_part.startswith('${') and not first_part.startswith('@{'):
                keywords.add(first_part)

    return list(keywords)


def read_execution_records(db_path: str) -> list:
    """Read all execution records from SQLite."""
    if not os.path.exists(db_path):
        logger.error(f"  Database not found: {db_path}")
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM execution_records ORDER BY timestamp").fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"  Failed to read execution_records: {e}")
        return []
    finally:
        conn.close()


def migrate_learning_chromadb(records: list, dry_run: bool = False) -> int:
    """Rebuild learning_chromadb/execution_embeddings from SQLite records."""
    import chromadb

    # Filter records that have a user_query (required for embedding)
    embeddable = [r for r in records if r.get("user_query")]
    logger.info(f"  Found {len(embeddable)} records with user_query to embed")

    if dry_run:
        logger.info("  [DRY RUN] Would delete and recreate learning_chromadb/")
        return len(embeddable)

    # Delete stale chromadb
    if os.path.exists(LEARNING_CHROMADB_DIR):
        shutil.rmtree(LEARNING_CHROMADB_DIR)
        logger.info(f"  Deleted stale: {LEARNING_CHROMADB_DIR}")

    # Create fresh with default ONNX embedding
    client = chromadb.PersistentClient(path=LEARNING_CHROMADB_DIR)
    collection = client.get_or_create_collection(
        name="execution_embeddings",
        metadata={"hnsw:space": "cosine"},
    )

    # Batch insert (ChromaDB supports batch add)
    if embeddable:
        collection.add(
            documents=[r["user_query"] for r in embeddable],
            ids=[r["workflow_id"] for r in embeddable],
            metadatas=[{
                "test_status": r.get("test_status", ""),
                "failure_category": r.get("failure_category") or "",
                "domain": r.get("domain") or "",
                "code_structure": r.get("code_structure") or "",
                "workflow_id": r["workflow_id"],
            } for r in embeddable],
        )

    logger.info(f"  Rebuilt execution_embeddings: {collection.count()} records")
    return collection.count()


def migrate_query_patterns(records: list, dry_run: bool = False) -> int:
    """Rebuild chroma_db/query_patterns from robot_code in SQLite records."""
    import chromadb
    import uuid

    # Filter records that have robot_code and passed
    codeable = [
        r for r in records
        if r.get("robot_code") and r.get("test_status") == "passed"
    ]
    logger.info(f"  Found {len(codeable)} passed records with robot_code")

    if dry_run:
        logger.info("  [DRY RUN] Would rebuild query_patterns collection")
        return len(codeable)

    # Open the optimization chromadb (don't delete the whole dir — it has other collections)
    client = chromadb.PersistentClient(path=OPTIMIZATION_CHROMADB_DIR)

    # Delete and recreate only the query_patterns collection
    try:
        client.delete_collection("query_patterns")
        logger.info("  Deleted stale query_patterns collection")
    except Exception:
        pass  # Collection didn't exist

    collection = client.get_or_create_collection(
        name="query_patterns",
        metadata={"type": "query_patterns"},
    )

    # Extract keywords from each record's robot_code and store pattern
    migrated = 0
    for r in codeable:
        keywords = extract_keywords_from_code(r["robot_code"])
        if not keywords:
            continue

        pattern_id = f"pattern_{uuid.uuid4().hex}"
        collection.add(
            documents=[r["user_query"]],
            ids=[pattern_id],
            metadatas=[{
                "keywords": json.dumps(keywords),
                "timestamp": r.get("timestamp", ""),
            }],
        )
        migrated += 1

    logger.info(f"  Rebuilt query_patterns: {collection.count()} patterns from {migrated} records")
    return collection.count()


def main():
    dry_run = "--dry-run" in sys.argv

    logger.info("=" * 60)
    logger.info("  ChromaDB Migration: sentence_transformers → ONNX")
    logger.info("=" * 60)

    if dry_run:
        logger.info("\n  Mode: DRY RUN (no changes will be made)\n")
    else:
        logger.info("")

    # Step 1: Read source of truth
    logger.info("Step 1: Reading execution records from SQLite...")
    records = read_execution_records(EXECUTION_DB)
    if not records:
        logger.error("  No records found. Nothing to migrate.")
        sys.exit(1)
    logger.info(f"  Total records: {len(records)}")
    passed = sum(1 for r in records if r.get("test_status") == "passed")
    failed = sum(1 for r in records if r.get("test_status") == "failed")
    error = sum(1 for r in records if r.get("test_status") == "error")
    logger.info(f"  Breakdown: {passed} passed, {failed} failed, {error} error")

    # Step 2: Migrate learning_chromadb (execution_embeddings)
    logger.info("\nStep 2: Migrating learning_chromadb/execution_embeddings...")
    embed_count = migrate_learning_chromadb(records, dry_run=dry_run)

    # Step 3: Migrate query_patterns in chroma_db
    logger.info("\nStep 3: Migrating chroma_db/query_patterns...")
    pattern_count = migrate_query_patterns(records, dry_run=dry_run)

    # Summary
    logger.info("\n" + "=" * 60)
    logger.info("  Migration Complete!" if not dry_run else "  Dry Run Complete!")
    logger.info("=" * 60)
    logger.info(f"  execution_embeddings: {embed_count} records")
    logger.info(f"  query_patterns:       {pattern_count} patterns")
    logger.info(f"  SQLite (untouched):   {len(records)} records")
    logger.info("")
    logger.info("  Note: keywords_browser and category_descriptions")
    logger.info("  will auto-rebuild on next application startup.")
    logger.info("")


if __name__ == "__main__":
    main()
