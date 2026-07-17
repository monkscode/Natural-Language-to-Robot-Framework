"""Shared fastembed text embedder for the Postgres/pgvector learning stack.

One process-wide lazily-loaded ONNX model (all-MiniLM-L6-v2, 384-dim) — the same
model ChromaDB used by default, so vectors are identical (verified: cosine
1.00000 per text). No torch / sentence-transformers. Used by the keyword-pattern
store AND the learning store (PostgresExecutionMemory borrows this singleton
since Task 31 — one model per process, one cold load).

Returns vectors as pgvector text literals ('[v1,v2,...]') so they insert into a
`vector(384)` column via the text->vector cast and need no extra psycopg adapter.
"""

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

_embedder = None
_lock = threading.Lock()
_failed_at: float | None = None  # time.monotonic() of the last failed init
# Text -> literal memo (Task 31). The same user query is embedded several
# times per workflow (similarity filters + pattern search); vectors are
# deterministic per model instance, so recomputing is pure waste (~25-140ms
# ONNX inference each). Keyed against the embedder identity: a swapped
# instance (tests inject fakes; a reload after failure) invalidates the memo.
_memo: dict = {}
_memo_embedder = None
_MEMO_MAX = 256
# Same cooldown as PostgresExecutionMemory._CHROMA_RETRY_COOLDOWN_S: a failed
# init can mean an ~80MB model download attempt, so without the cooldown every
# embed call would retry it (serialized under the lock — blocking callers).
_RETRY_COOLDOWN_S = 300


def _cache_dir() -> str:
    from src.backend.crew_ai.optimization.learning_config import LEARNING_CONFIG
    base = os.path.dirname(LEARNING_CONFIG["CHROMADB_DIR"]) or "data"
    return os.path.join(base, "fastembed_cache")


def get_embedder():
    """Return the shared fastembed TextEmbedding, loading it once.

    Returns None while embedding is unavailable; a failed init is retried at
    most every _RETRY_COOLDOWN_S seconds.
    """
    global _embedder, _failed_at
    if _embedder is not None:
        return _embedder
    if _failed_at is not None and time.monotonic() - _failed_at < _RETRY_COOLDOWN_S:
        return None
    with _lock:
        if _embedder is not None:
            return _embedder
        if _failed_at is not None and time.monotonic() - _failed_at < _RETRY_COOLDOWN_S:
            return None
        try:
            from fastembed import TextEmbedding
            _embedder = TextEmbedding(model_name=EMBED_MODEL, cache_dir=_cache_dir())
            _failed_at = None
        except Exception as e:
            _failed_at = time.monotonic()
            logger.error(
                "[EMBED] fastembed init failed: %s — retrying in %ds", e, _RETRY_COOLDOWN_S
            )
            return None
    return _embedder


def embed_to_literal(text: str) -> str | None:
    """Embed one string to a pgvector literal '[...]', or None if embedding is unavailable.

    Memoized per embedder instance; failures are never cached, so a transient
    ONNX error does not poison later calls for the same text.
    """
    global _memo, _memo_embedder
    emb = get_embedder()
    if emb is None:
        return None
    if _memo_embedder is not emb:
        _memo = {}
        _memo_embedder = emb
    hit = _memo.get(text)
    if hit is not None:
        return hit
    try:
        vec = next(iter(emb.embed([text])))
    except Exception as e:
        logger.warning("[EMBED] embedding failed (non-blocking): %s", e)
        return None
    lit = "[" + ",".join("%.7g" % float(x) for x in vec) + "]"
    if len(_memo) >= _MEMO_MAX:
        _memo.clear()
    _memo[text] = lit
    return lit
