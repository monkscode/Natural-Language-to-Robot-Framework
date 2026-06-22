"""kw_query_patterns: org written; search_patterns org-scoped (real store)."""

import psycopg
import pytest

from src.backend.crew_ai.optimization import keyword_vector_store as kvs_mod


# Override the package autouse fixture so THIS module gets the real
# KeywordVectorStore, not the MagicMock the suite installs by default.
@pytest.fixture(autouse=True)
def _isolate_keyword_store():
    yield


pytestmark = pytest.mark.integration


@pytest.fixture
def kw_store():
    from src.backend.crew_ai.optimization.keyword_vector_store import KeywordVectorStore
    from src.backend.core.config import PG_CONNECT_TIMEOUT_S, settings

    sep = "&" if "?" in settings.DATABASE_URL else "?"
    dsn = settings.DATABASE_URL + f"{sep}options=-c%20search_path%3Dlearning_test,public"

    # Ensure the schema exists: it's created by _pg_test_em when the full suite
    # runs, but may be absent when this module runs standalone.
    admin = psycopg.connect(
        settings.DATABASE_URL, autocommit=True, connect_timeout=PG_CONNECT_TIMEOUT_S
    )
    try:
        admin.execute("CREATE SCHEMA IF NOT EXISTS learning_test")
    finally:
        admin.close()

    store = KeywordVectorStore(dsn=dsn)
    yield store
    store.close()


def test_search_patterns_scoped_by_org(kw_store):
    kw_store.add_pattern("login as admin", ["Fill Text", "Click"], org_id="org-A")
    kw_store.add_pattern("login as admin", ["Type Text", "Submit"], org_id="org-B")
    res_b = kw_store.search_patterns("login as admin", org_id="org-B")
    assert res_b, "org B should see its own pattern"
    flat = [k for r in res_b for k in (r["keywords"] or [])]
    assert "Fill Text" not in flat, "org B leaked org A's pattern"
