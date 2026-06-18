"""Unit tests for the shared fastembed embedder (optimization/embedding.py).

The module holds process-wide state (_embedder, _failed_at); every test that
touches it snapshots and restores both globals so the real embedder cached by
other suites (pgvector tests) is never clobbered.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from src.backend.crew_ai.optimization import embedding


@pytest.fixture
def _clean_embedder_state():
    """Run the test against a reset embedding module; restore state after."""
    saved = (embedding._embedder, embedding._failed_at)
    embedding._embedder = None
    embedding._failed_at = None
    yield
    embedding._embedder, embedding._failed_at = saved


def test_get_embedder_loads_once_and_is_shared():
    emb = embedding.get_embedder()
    if emb is None:
        pytest.skip("fastembed model unavailable")
    assert embedding.get_embedder() is emb


def test_embed_to_literal_is_a_pgvector_literal():
    if embedding.get_embedder() is None:
        pytest.skip("fastembed model unavailable")
    lit = embedding.embed_to_literal("click the login button")
    assert lit is not None
    assert lit.startswith("[") and lit.endswith("]")
    values = lit[1:-1].split(",")
    assert len(values) == embedding.EMBEDDING_DIM
    assert all(float(v) == float(v) for v in values)  # parseable, not NaN


def test_failed_init_is_cached_then_retried_after_cooldown(_clean_embedder_state):
    real = MagicMock(name="embedder")
    boom_then_ok = MagicMock(side_effect=[RuntimeError("download failed"), real])
    with patch("fastembed.TextEmbedding", boom_then_ok):
        assert embedding.get_embedder() is None        # first attempt fails
        assert embedding._failed_at is not None
        assert embedding.get_embedder() is None        # inside cooldown: no retry
        assert boom_then_ok.call_count == 1
        embedding._failed_at -= embedding._RETRY_COOLDOWN_S + 1  # age the failure
        assert embedding.get_embedder() is real        # retried and recovered
        assert embedding._failed_at is None


def test_embed_to_literal_none_while_unavailable(_clean_embedder_state):
    embedding._failed_at = time.monotonic()  # fresh failure -> cooldown cache
    assert embedding.embed_to_literal("anything") is None


def test_embed_to_literal_none_when_embed_raises(_clean_embedder_state):
    broken = MagicMock()
    broken.embed.side_effect = RuntimeError("onnx session died")
    embedding._embedder = broken
    assert embedding.embed_to_literal("anything") is None
