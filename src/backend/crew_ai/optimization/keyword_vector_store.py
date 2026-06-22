"""pgvector-backed vector store for Robot Framework keywords + query patterns.

Phase 4 consolidation: replaces the former ChromaDB keyword store (`chroma_store`).
Embeddings use fastembed all-MiniLM-L6-v2 (384-dim) — the SAME model ChromaDB used
by default, so similarity behaviour carries over unchanged. Distance is **L2**
(`vector_l2_ops`), matching the old default; callers convert to a similarity
score with `1 / (1 + distance)`.

Tables (in the consolidated DB):
- kw_keywords(library, name, args jsonb, doc, embedding)  — per-library keyword
  index, rebuilt from live library docs (regenerable cache).
- kw_query_patterns(id, user_query, keywords jsonb, created_at, embedding) —
  accumulated (user_query -> keywords) patterns from successful runs.
- kw_library_version(library, version) — for rebuild-on-version-change.

Referenced by: keyword_search_tool.py, pattern_learning.py, feedback_loop.py, crew.py.
"""

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

import psycopg
from psycopg_pool import ConnectionPool

from src.backend.config.logging_config import sanitize_for_log
from src.backend.core.config import PG_CONNECT_TIMEOUT_S, settings
from src.backend.crew_ai.optimization import embedding

logger = logging.getLogger(__name__)

_SCHEMA_DDL = (
    # SCHEMA public pins the extension deterministically — see pg_schema.py.
    "CREATE EXTENSION IF NOT EXISTS vector SCHEMA public",
    """
    CREATE TABLE IF NOT EXISTS kw_keywords (
        library   TEXT NOT NULL,
        name      TEXT NOT NULL,
        args      JSONB,
        doc       TEXT,
        embedding vector(384) NOT NULL,
        PRIMARY KEY (library, name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_kw_keywords_emb "
    "ON kw_keywords USING hnsw (embedding vector_l2_ops)",
    """
    CREATE TABLE IF NOT EXISTS kw_query_patterns (
        id         TEXT PRIMARY KEY,
        user_query TEXT NOT NULL,
        keywords   JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        embedding  vector(384) NOT NULL,
        org_id     TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_kw_patterns_emb "
    "ON kw_query_patterns USING hnsw (embedding vector_l2_ops)",
    "ALTER TABLE kw_query_patterns ADD COLUMN IF NOT EXISTS org_id TEXT",
    "CREATE INDEX IF NOT EXISTS idx_kw_patterns_org ON kw_query_patterns(org_id)",
    """
    CREATE TABLE IF NOT EXISTS kw_library_version (
        library TEXT PRIMARY KEY,
        version TEXT
    )
    """,
)


class KeywordVectorStore:
    """pgvector store for RF keywords and learned query patterns (L2 distance)."""

    def __init__(self, persist_directory: str = None, dsn: str = None):
        # persist_directory is kept for call-site compatibility (the old ChromaDB
        # signature) and ignored — the store now lives in Postgres.
        self.dsn = dsn or settings.DATABASE_URL
        setup = psycopg.connect(
            self.dsn, autocommit=True, connect_timeout=PG_CONNECT_TIMEOUT_S)
        try:
            for ddl in _SCHEMA_DDL:
                setup.execute(ddl)
        finally:
            setup.close()
        self._pool = ConnectionPool(
            conninfo=self.dsn, min_size=1, max_size=4,
            kwargs={"connect_timeout": PG_CONNECT_TIMEOUT_S}, open=True)
        logger.info("[KEYWORD_STORE] pgvector keyword store ready")

    # ------------------------------------------------------------------
    # Keyword index (per library)
    # ------------------------------------------------------------------

    def _prepare_keyword_rows(self, library_name: str, keywords: List[Dict]) -> list:
        """Embed keywords into insert-ready rows (no DB access)."""
        rows = []
        for kw in keywords:
            name = kw.get("name", "")
            if not name:
                continue
            doc = kw.get("doc", "") or ""
            vec = embedding.embed_to_literal(f"{name} {doc}")
            if vec is None:
                continue
            rows.append((library_name, name, json.dumps(kw.get("args", [])),
                         doc[:500], vec))
        return rows

    @staticmethod
    def _upsert_keyword_rows(conn, rows: list) -> None:
        """Upsert prepared rows on the caller's connection (caller commits)."""
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO kw_keywords (library, name, args, doc, embedding) "
                "VALUES (%s, %s, %s, %s, %s::vector) "
                "ON CONFLICT (library, name) DO UPDATE SET "
                "  args = EXCLUDED.args, doc = EXCLUDED.doc, "
                "  embedding = EXCLUDED.embedding",
                rows)

    def add_keywords(self, library_name: str, keywords: List[Dict]) -> None:
        """Upsert keyword rows for a library (embeds 'name doc')."""
        if not keywords:
            logger.warning("No keywords provided for %s", library_name)
            return
        rows = self._prepare_keyword_rows(library_name, keywords)
        if not rows:
            logger.warning("No embeddable keywords for %s", library_name)
            return
        try:
            with self._pool.connection() as conn:
                self._upsert_keyword_rows(conn, rows)
                conn.commit()
            logger.info("Added %d keywords to %s", len(rows), library_name)
        except Exception as e:
            logger.error("Failed to add keywords to %s: %s", library_name, e)
            raise

    def _extract_public_keywords(self, library_name: str) -> List[Dict]:
        """Pull the library's public, non-deprecated keywords from its docs."""
        from ..library_context.dynamic_context import DynamicLibraryDocumentation
        logger.info("Extracting keywords from %s...", library_name)
        doc_data = DynamicLibraryDocumentation(library_name).get_library_documentation()
        keywords = doc_data.get("keywords", [])
        public_keywords = [
            kw for kw in keywords
            if not kw["name"].startswith("_")
            and "deprecated" not in kw.get("doc", "")[:150].lower()
        ]
        logger.info("Found %d public keywords in %s", len(public_keywords), library_name)
        return public_keywords

    def ingest_library_keywords(self, library_name: str) -> None:
        """Extract all keywords from a library's docs and index them."""
        try:
            self.add_keywords(library_name, self._extract_public_keywords(library_name))
        except Exception as e:
            logger.error("Failed to ingest keywords from %s: %s", library_name, e)
            raise

    def search(self, library_name: str, query: str, top_k: int = 3) -> List[Dict]:
        """Semantic search for keywords (L2 distance; similarity = 1/(1+distance))."""
        vec = embedding.embed_to_literal(query)
        if vec is None:
            return []
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT name, args, doc, embedding <-> %s::vector AS distance "
                        "FROM kw_keywords WHERE library = %s "
                        "ORDER BY embedding <-> %s::vector LIMIT %s",
                        (vec, library_name, vec, top_k))
                    out = []
                    for name, args, doc, distance in cur.fetchall():
                        out.append({
                            "name": name,
                            "args": args if args is not None else [],
                            "description": doc or "",
                            "distance": float(distance),
                            "similarity": 1.0 / (1.0 + float(distance)),
                        })
            logger.debug("Found %d keywords for query: %s", len(out), query)
            return out
        except Exception as e:
            logger.error("Search failed for query '%s': %s", query, e)
            return []

    # ------------------------------------------------------------------
    # Version tracking + rebuild
    # ------------------------------------------------------------------

    def get_library_version(self, library_name: str) -> Optional[str]:
        try:
            from ..library_context.dynamic_context import DynamicLibraryDocumentation
            return DynamicLibraryDocumentation(library_name).get_library_documentation().get("version")
        except Exception as e:
            logger.warning("Could not get version for %s: %s", library_name, e)
            return None

    def get_collection_version(self, library_name: str) -> Optional[str]:
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT version FROM kw_library_version WHERE library = %s",
                    (library_name,)).fetchone()
            return row[0] if row else None
        except Exception as e:
            logger.warning("Could not get stored version for %s: %s", library_name, e)
            return None

    def needs_rebuild(self, library_name: str) -> bool:
        try:
            stored = self.get_collection_version(library_name)
            if stored is None:
                logger.info("No stored version for %s, rebuild needed", library_name)
                return True
            current = self.get_library_version(library_name)
            if current != stored:
                logger.info("Version mismatch for %s: %s -> %s, rebuild needed",
                            sanitize_for_log(library_name), stored,
                            sanitize_for_log(current))
                return True
            return False
        except Exception as e:
            logger.warning("Could not check rebuild status for %s: %s", library_name, e)
            return False

    def rebuild_collection(self, library_name: str) -> None:
        try:
            # Extract + embed BEFORE touching the table, so a failure there
            # leaves the existing index untouched and the transaction stays short.
            rows = self._prepare_keyword_rows(
                library_name, self._extract_public_keywords(library_name))
            version = self.get_library_version(library_name)
            # Delete + refill + version bump in ONE transaction: concurrent
            # searches keep seeing the old index (MVCC) until the commit swaps
            # it atomically, and a mid-rebuild failure rolls back to the old
            # index instead of leaving the library empty.
            with self._pool.connection() as conn:
                conn.execute("DELETE FROM kw_keywords WHERE library = %s", (library_name,))
                if rows:
                    self._upsert_keyword_rows(conn, rows)
                conn.execute(
                    "INSERT INTO kw_library_version (library, version) VALUES (%s, %s) "
                    "ON CONFLICT (library) DO UPDATE SET version = EXCLUDED.version",
                    (library_name, version))
                conn.commit()
            logger.info("Rebuilt keyword index for %s (%d keywords)", library_name, len(rows))
        except Exception as e:
            logger.error("Failed to rebuild keyword index for %s: %s", library_name, e)
            raise

    def ensure_collection_ready(self, library_name: str) -> None:
        try:
            if self.needs_rebuild(library_name):
                logger.info("Rebuilding keyword index for %s...", library_name)
                self.rebuild_collection(library_name)
            else:
                logger.debug("Keyword index for %s is up-to-date", library_name)
        except Exception as e:
            logger.error("Failed to ensure keyword index ready for %s: %s", library_name, e)
            raise

    # ------------------------------------------------------------------
    # Query patterns (used by QueryPatternMatcher)
    # ------------------------------------------------------------------

    def add_pattern(self, user_query: str, keywords: List[str],
                    org_id: str | None = None) -> Optional[str]:
        """Store one (user_query -> keywords) pattern; returns its id (or None)."""
        vec = embedding.embed_to_literal(user_query)
        if vec is None:
            return None
        pattern_id = f"pattern_{uuid.uuid4().hex}"
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    "INSERT INTO kw_query_patterns "
                    "(id, user_query, keywords, created_at, embedding, org_id) "
                    "VALUES (%s, %s, %s, %s, %s::vector, %s)",
                    (pattern_id, user_query, json.dumps(keywords),
                     datetime.now(timezone.utc), vec, org_id))
                conn.commit()
            return pattern_id
        except Exception as e:
            logger.warning("Failed to store query pattern (non-blocking): %s", e)
            return None

    def search_patterns(self, user_query: str, top_k: int = 5,
                        org_id: str | None = None) -> List[Dict]:
        """Nearest query patterns by L2 distance. Returns [{keywords, distance}].

        When org_id is set only patterns belonging to that org are returned.
        When org_id is None the search is unscoped (backward-compatible).
        """
        vec = embedding.embed_to_literal(user_query)
        if vec is None:
            return []
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    where = "WHERE org_id = %s " if org_id is not None else ""
                    params = (
                        [vec, org_id, vec, top_k]
                        if org_id is not None
                        else [vec, vec, top_k]
                    )
                    cur.execute(
                        "SELECT keywords, embedding <-> %s::vector AS distance "
                        f"FROM kw_query_patterns {where}"
                        "ORDER BY embedding <-> %s::vector LIMIT %s",
                        params)
                    return [{"keywords": kw, "distance": float(d)} for kw, d in cur.fetchall()]
        except Exception as e:
            logger.warning("Query-pattern search failed: %s", e)
            return []

    def pattern_count(self) -> int:
        try:
            with self._pool.connection() as conn:
                return conn.execute("SELECT COUNT(*) FROM kw_query_patterns").fetchone()[0]
        except Exception:
            return 0

    def close(self) -> None:
        try:
            self._pool.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Process-wide singleton — one pool + one DDL bootstrap per process, shared by
# FeedbackLoop and run_crew (mirrors trace_store.get_trace_store). Constructing
# a store per workflow would open a new ConnectionPool each run and, with no
# close() call, leak connections until Postgres hits max_connections — which
# takes auth down with it (same database).
# ---------------------------------------------------------------------------
_singleton: "KeywordVectorStore | None" = None
_singleton_lock = threading.Lock()


def get_keyword_vector_store() -> KeywordVectorStore:
    """Return the shared KeywordVectorStore, creating it on first use.

    Raises if Postgres is unreachable (the constructor fails before the pool
    is created, so a failed attempt leaks nothing). Failures are NOT cached —
    the next caller retries, so the store comes up as soon as the DB does.
    Callers treat the keyword store as optional and already catch.
    """
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = KeywordVectorStore()
    return _singleton


def close_keyword_vector_store() -> None:
    """Close the shared store's pool (app shutdown; best-effort)."""
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            _singleton.close()
            _singleton = None
