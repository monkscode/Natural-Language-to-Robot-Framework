"""Shared fastembed text embedder for the Postgres/pgvector learning stack.

One process-wide lazily-loaded ONNX model (all-MiniLM-L6-v2, 384-dim) — the same
model ChromaDB used by default, so vectors are identical (verified: cosine
1.00000 per text). No torch / sentence-transformers. Used by the keyword-pattern
store; the learning store keeps its own copy for now.

Returns vectors as pgvector text literals ('[v1,v2,...]') so they insert into a
`vector(384)` column via the text->vector cast and need no extra psycopg adapter.
"""

import logging
import os
import threading

logger = logging.getLogger(__name__)

EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

_embedder = None
_lock = threading.Lock()


def _cache_dir() -> str:
    from src.backend.crew_ai.optimization.learning_config import LEARNING_CONFIG
    base = os.path.dirname(LEARNING_CONFIG["CHROMADB_DIR"]) or "data"
    return os.path.join(base, "fastembed_cache")


def get_embedder():
    """Return the shared fastembed TextEmbedding, loading it once (or None on failure)."""
    global _embedder
    if _embedder is not None:
        return _embedder
    with _lock:
        if _embedder is None:
            try:
                from fastembed import TextEmbedding
                _embedder = TextEmbedding(model_name=EMBED_MODEL, cache_dir=_cache_dir())
            except Exception as e:
                logger.error("[EMBED] fastembed init failed: %s", e)
                return None
    return _embedder


def embed_to_literal(text: str) -> str | None:
    """Embed one string to a pgvector literal '[...]', or None if embedding is unavailable."""
    emb = get_embedder()
    if emb is None:
        return None
    try:
        vec = next(iter(emb.embed([text])))
    except Exception as e:
        logger.warning("[EMBED] embedding failed (non-blocking): %s", e)
        return None
    return "[" + ",".join("%.7g" % float(x) for x in vec) + "]"
