"""Real-store tests for the pgvector KeywordVectorStore (isolated schema).

The autouse _isolate_keyword_store fixture (conftest) replaces the CLASS with a
MagicMock so the rest of the optimization suite can never write to the live
public schema. This module binds the real class at import time (collection
happens before fixtures patch the module attribute) and runs it against a
throwaway kw_store_test schema, mirroring the auth/optimization isolation
pattern. Library-doc extraction is patched where needed — CI has no
robotframework installed, and these tests exercise the store, not libdoc
parsing.

The singleton accessor tests intentionally rely on the conftest patch: there,
get_keyword_vector_store() constructs the MagicMock class, so the caching and
close logic runs for real without touching Postgres.
"""

import pytest
from unittest.mock import MagicMock, patch

from src.backend.core.config import PG_CONNECT_TIMEOUT_S, settings
from src.backend.crew_ai.optimization import keyword_vector_store as kvs_mod
from src.backend.crew_ai.optimization.keyword_vector_store import (  # real class
    KeywordVectorStore,
    close_keyword_vector_store,
    get_keyword_vector_store,
)

pytestmark = pytest.mark.integration

_SCHEMA = "kw_store_test"

_KEYWORDS = [
    {"name": "Click Button", "args": ["locator"], "doc": "Clicks a button on the page."},
    {"name": "Fill Text", "args": ["locator", "text"], "doc": "Types text into an input field."},
    {"name": "Open Browser", "args": ["url"], "doc": "Opens a new browser session at the url."},
]


@pytest.fixture(scope="module")
def store():
    import psycopg

    try:
        admin = psycopg.connect(
            settings.DATABASE_URL, autocommit=True,
            connect_timeout=PG_CONNECT_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — connect failure = skip, not fail
        pytest.skip(f"Postgres unavailable: {exc}")
    if kvs_mod.embedding.get_embedder() is None:
        admin.close()
        pytest.skip("fastembed model unavailable")

    s = None
    try:
        admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        admin.execute(f"CREATE SCHEMA {_SCHEMA}")
        sep = "&" if "?" in settings.DATABASE_URL else "?"
        dsn = (
            settings.DATABASE_URL
            + f"{sep}options=-c%20search_path%3D{_SCHEMA},public"
        )
        s = KeywordVectorStore(dsn=dsn)
        # Seed one read-only library; mutating tests use their own library names.
        s.add_keywords("Browser", _KEYWORDS)
        yield s
    finally:
        if s is not None:
            s.close()
        admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        admin.close()


# ---------------------------------------------------------------------------
# Keyword index: add + search
# ---------------------------------------------------------------------------

def test_search_returns_semantic_matches(store):
    hits = store.search("Browser", "click the submit button", top_k=2)
    assert hits, "expected at least one semantic hit"
    assert hits[0]["name"] == "Click Button"
    for h in hits:
        assert h["args"] and isinstance(h["args"], list)  # jsonb round-trip
        assert h["distance"] >= 0.0
        assert 0.0 < h["similarity"] <= 1.0
        assert h["similarity"] == pytest.approx(1.0 / (1.0 + h["distance"]))


def test_search_unknown_library_returns_empty(store):
    assert store.search("NoSuchLibrary", "click something") == []


def test_search_returns_empty_when_embedding_unavailable(store):
    with patch.object(kvs_mod.embedding, "embed_to_literal", return_value=None):
        assert store.search("Browser", "click the button") == []


def test_add_keywords_empty_or_nameless_is_noop(store):
    store.add_keywords("EmptyLib", [])
    store.add_keywords("EmptyLib", [{"name": "", "doc": "nameless is skipped"}])
    assert store.search("EmptyLib", "anything") == []


def test_add_keywords_upserts_on_conflict(store):
    lib = "uplib"
    store.add_keywords(lib, [{"name": "Go To", "args": ["url"], "doc": "Navigates."}])
    store.add_keywords(lib, [{"name": "Go To", "args": ["url"], "doc": "Navigates the page to url."}])
    hits = store.search(lib, "navigate to a url", top_k=10)
    assert len(hits) == 1  # upsert, not duplicate
    assert hits[0]["description"] == "Navigates the page to url."


# ---------------------------------------------------------------------------
# Version tracking + rebuild
# ---------------------------------------------------------------------------

def test_needs_rebuild_true_when_no_stored_version(store):
    assert store.get_collection_version("vlib") is None
    assert store.needs_rebuild("vlib") is True


def test_rebuild_collection_swaps_index_and_records_version(store):
    lib = "vlib"
    with patch.object(store, "_extract_public_keywords", return_value=_KEYWORDS), \
         patch.object(store, "get_library_version", return_value="1.0"):
        store.rebuild_collection(lib)
        assert store.get_collection_version(lib) == "1.0"
        assert store.needs_rebuild(lib) is False  # stored == current
    with patch.object(store, "get_library_version", return_value="2.0"):
        assert store.needs_rebuild(lib) is True  # version drift
    assert store.search(lib, "click the button")[0]["name"] == "Click Button"


def test_rebuild_failure_leaves_existing_index_untouched(store):
    lib = "vlib"  # populated by the previous test's rebuild
    with patch.object(
        store, "_extract_public_keywords", side_effect=RuntimeError("no libdoc")
    ):
        with pytest.raises(RuntimeError):
            store.rebuild_collection(lib)
    assert store.search(lib, "click the button"), "old index must survive a failed rebuild"


def test_ensure_collection_ready_rebuilds_only_when_needed(store):
    with patch.object(store, "needs_rebuild", return_value=False), \
         patch.object(store, "rebuild_collection") as rebuild:
        store.ensure_collection_ready("Browser")
        rebuild.assert_not_called()
    with patch.object(store, "needs_rebuild", return_value=True), \
         patch.object(store, "rebuild_collection") as rebuild:
        store.ensure_collection_ready("Browser")
        rebuild.assert_called_once_with("Browser")


def test_ingest_library_keywords_uses_extracted_docs(store):
    lib = "inglib"
    with patch.object(store, "_extract_public_keywords", return_value=_KEYWORDS[:1]):
        store.ingest_library_keywords(lib)
    assert store.search(lib, "click the button")[0]["name"] == "Click Button"


# ---------------------------------------------------------------------------
# Query patterns
# ---------------------------------------------------------------------------

def test_add_and_search_patterns_roundtrip(store):
    before = store.pattern_count()
    pid = store.add_pattern("search for python on google", ["Open Browser", "Fill Text"])
    assert pid and pid.startswith("pattern_")
    assert store.pattern_count() == before + 1
    results = store.search_patterns("google search for python", top_k=3)
    assert results
    assert results[0]["keywords"] == ["Open Browser", "Fill Text"]  # jsonb round-trip
    assert isinstance(results[0]["distance"], float)


def test_pattern_helpers_degrade_when_embedding_unavailable(store):
    with patch.object(kvs_mod.embedding, "embed_to_literal", return_value=None):
        assert store.add_pattern("q", ["K"]) is None
        assert store.search_patterns("q") == []


# ---------------------------------------------------------------------------
# Library-doc extraction (DynamicLibraryDocumentation mocked — no robotframework)
# ---------------------------------------------------------------------------

_DYN_DOC = "src.backend.crew_ai.library_context.dynamic_context.DynamicLibraryDocumentation"


def test_extract_public_keywords_filters_private_and_deprecated(store):
    doc = MagicMock()
    doc.return_value.get_library_documentation.return_value = {
        "keywords": [
            {"name": "Click Button", "doc": "Clicks."},
            {"name": "_internal_helper", "doc": "Private."},
            {"name": "Old Click", "doc": "*DEPRECATED* use Click Button."},
            {"name": "Fill Text", "doc": ""},
        ]
    }
    with patch(_DYN_DOC, doc):
        out = store._extract_public_keywords("Browser")
    assert [k["name"] for k in out] == ["Click Button", "Fill Text"]


def test_get_library_version_reads_docs_and_degrades_to_none(store):
    doc = MagicMock()
    doc.return_value.get_library_documentation.return_value = {"version": "18.3.0"}
    with patch(_DYN_DOC, doc):
        assert store.get_library_version("Browser") == "18.3.0"
    with patch(_DYN_DOC, side_effect=ImportError("robotframework not installed")):
        assert store.get_library_version("Browser") is None


def test_ingest_failure_propagates(store):
    with patch.object(store, "_extract_public_keywords",
                      side_effect=RuntimeError("libdoc broke")):
        with pytest.raises(RuntimeError, match="libdoc broke"):
            store.ingest_library_keywords("Browser")


# ---------------------------------------------------------------------------
# Degradation under DB / embedding failures
# ---------------------------------------------------------------------------

def _broken_pool():
    pool = MagicMock()
    pool.connection.side_effect = RuntimeError("pool closed")
    return pool


def test_add_keywords_db_failure_raises(store):
    with patch.object(store, "_pool", _broken_pool()):
        with pytest.raises(RuntimeError, match="pool closed"):
            store.add_keywords("Browser", _KEYWORDS[:1])


def test_add_keywords_skips_keywords_that_fail_to_embed(store):
    with patch.object(kvs_mod.embedding, "embed_to_literal", return_value=None):
        store.add_keywords("NoEmbedLib", _KEYWORDS)  # all skipped -> noop, no raise
    assert store.search("NoEmbedLib", "anything") == []


def test_read_paths_degrade_when_db_is_down(store):
    """Search/version/count are advisory — a DB outage must degrade, not raise."""
    with patch.object(store, "_pool", _broken_pool()):
        assert store.search("Browser", "click the button") == []
        assert store.get_collection_version("Browser") is None
        assert store.add_pattern("a query", ["Click"]) is None
        assert store.search_patterns("a query") == []
        assert store.pattern_count() == 0


def test_needs_rebuild_false_when_check_itself_fails(store):
    with patch.object(store, "get_collection_version",
                      side_effect=RuntimeError("db down")):
        assert store.needs_rebuild("Browser") is False


def test_ensure_collection_ready_propagates_rebuild_failure(store):
    with patch.object(store, "needs_rebuild", return_value=True), \
         patch.object(store, "rebuild_collection",
                      side_effect=RuntimeError("rebuild broke")):
        with pytest.raises(RuntimeError, match="rebuild broke"):
            store.ensure_collection_ready("Browser")


def test_close_swallows_pool_failure():
    s = KeywordVectorStore.__new__(KeywordVectorStore)  # no DB bootstrap
    s._pool = MagicMock()
    s._pool.close.side_effect = RuntimeError("already closed")
    s.close()  # must not raise


# ---------------------------------------------------------------------------
# Process-wide singleton accessor (class is the conftest MagicMock here, so the
# real accessor logic runs without touching Postgres)
# ---------------------------------------------------------------------------

def test_singleton_is_cached_and_close_resets():
    cls = kvs_mod.KeywordVectorStore  # the conftest's per-test MagicMock class
    first = get_keyword_vector_store()
    assert get_keyword_vector_store() is first
    assert cls.call_count == 1  # cached: constructed exactly once
    close_keyword_vector_store()
    first.close.assert_called_once()
    assert kvs_mod._singleton is None  # reset so the next getter rebuilds
    get_keyword_vector_store()
    assert cls.call_count == 2  # rebuilt after close
    close_keyword_vector_store()
    close_keyword_vector_store()  # idempotent when nothing is open
