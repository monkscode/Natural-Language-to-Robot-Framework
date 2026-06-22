"""Postgres-backed fixtures for the learning-endpoint API tests (Phase 4).

The learning dashboard endpoints became Postgres-only at slice 4.5 (native jsonb
KPI queries), so these tests run against a real PostgreSQL schema instead of the
SQLite ExecutionMemory. Mirrors tests/test_optimization/conftest.py: one
session-scoped PostgresExecutionMemory on an isolated schema, truncated per test.

Re-exports auth_isolated_schema so integration tests in this package (e.g.
test_history_org_scope.py) can opt in via usefixtures.
"""

from tests.test_auth.conftest import auth_isolated_schema  # noqa: F401

from unittest.mock import patch

import pytest

_API_PG_SCHEMA = "learning_api_test"


@pytest.fixture(autouse=True)
def _auth_not_enforced():
    """These tests exercise endpoint logic, not auth — disable JWT enforcement
    and stub require_admin's per-request DB re-validation (the crafted test
    tokens carry non-UUID ids and no users row exists for them; without the
    stub the re-check 401s and opens a real auth pool the bare-router apps
    never close).

    The guard security matrix (401/403 per flag, role, and DB state) is
    covered by tests/test_auth/test_guards.py.
    """
    from src.backend.auth import jwt_utils
    from src.backend.core.config import settings
    with patch.object(settings, "AUTH_ENFORCED", False), \
         patch.object(
             jwt_utils._admin_repo, "get_by_id",
             side_effect=lambda uid: {
                 "id": uid, "email": "admin@test.local",
                 "role": "admin", "is_active": True,
             },
         ):
        yield
_API_PG_TABLES = (
    "execution_records", "intent_patterns", "structural_rules", "keyword_corrections",
    "anti_patterns", "learning_stats", "learning_metrics", "nl_feedback_corrections",
    "trigger_events", "hint_audit", "hint_review_sessions", "hint_review_recommendations",
    "hint_review_pages", "hint_workflow_trace", "learning_anchors", "execution_embeddings",
)


@pytest.fixture(scope="session")
def _api_pg_admin():
    import psycopg
    from src.backend.core.config import settings
    conn = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def _api_pg_em(_api_pg_admin):
    """One PostgresExecutionMemory bound to an isolated test schema for the
    whole session; each test truncates it for a clean slate."""
    from src.backend.core.config import settings
    from src.backend.crew_ai.optimization.postgres_execution_memory import PostgresExecutionMemory

    _api_pg_admin.execute(f"DROP SCHEMA IF EXISTS {_API_PG_SCHEMA} CASCADE")
    _api_pg_admin.execute(f"CREATE SCHEMA {_API_PG_SCHEMA}")
    # public is on the path so the pgvector `vector` type (installed in public) resolves.
    dsn = settings.DATABASE_URL + f"?options=-c%20search_path%3D{_API_PG_SCHEMA},public"
    em = PostgresExecutionMemory(dsn=dsn)
    em._chroma_client = PostgresExecutionMemory._CHROMADB_INIT_FAILED
    yield em, dsn
    em.close()
    _api_pg_admin.execute(f"DROP SCHEMA IF EXISTS {_API_PG_SCHEMA} CASCADE")


@pytest.fixture
def api_pg_em(_api_pg_em, _api_pg_admin):
    """Clean PostgresExecutionMemory + its DSN per test (ChromaDB disabled)."""
    em, dsn = _api_pg_em
    try:
        em._writer_conn.rollback()  # clear any aborted txn from a prior test
    except Exception:
        pass
    truncate = (
        "TRUNCATE "
        + ", ".join(f"{_API_PG_SCHEMA}.{t}" for t in _API_PG_TABLES)
        + " RESTART IDENTITY CASCADE"
    )
    import psycopg
    try:
        _api_pg_admin.execute(truncate)
    except psycopg.errors.UndefinedTable:
        from src.backend.crew_ai.optimization import pg_schema
        raw = psycopg.connect(dsn, autocommit=True)
        try:
            pg_schema.ensure_schema(raw)
        finally:
            raw.close()
        _api_pg_admin.execute(truncate)
    from src.backend.crew_ai.optimization.postgres_execution_memory import PostgresExecutionMemory
    em._chroma_client = PostgresExecutionMemory._CHROMADB_INIT_FAILED
    saved_attrs = dict(em.__dict__)
    yield em, dsn
    em.__dict__.clear()
    em.__dict__.update(saved_attrs)


# ---------------------------------------------------------------------------
# Task 10 — learning_api_isolated, promote_client, seeded_hint_id
# ---------------------------------------------------------------------------

class _PromoteClient:
    """Thin wrapper: TestClient + admin_token attribute."""

    def __init__(self, client, admin_token: str):
        self._client = client
        self.admin_token = admin_token

    def __getattr__(self, name):
        return getattr(self._client, name)


@pytest.fixture
def learning_api_isolated(api_pg_em):
    """Inject an isolated FeedbackLoop singleton + patch _admin_conn for Task 10.

    Mirrors the auth_isolated_schema restore-before-close teardown pattern:
    the module global is restored FIRST so that any in-flight get_feedback_loop()
    call after teardown starts sees the original (or None) immediately; the
    isolated mock is not "closed" (it holds no real resources — the underlying
    PostgresExecutionMemory is managed by the session-scoped _api_pg_em fixture).
    """
    from unittest.mock import MagicMock, patch as _patch

    em, dsn = api_pg_em

    mock_fb = MagicMock()
    mock_fb.execution_memory = em
    mock_fb.nl_engine = MagicMock()
    mock_fb.write_queue = MagicMock()
    mock_fb.metrics_tracker = MagicMock()
    mock_fb.metrics_tracker.get_effectiveness_report.return_value = {}

    import src.backend.crew_ai.optimization.learning_registry as _lr_mod
    saved_fb = _lr_mod._feedback_loop_instance
    _lr_mod._feedback_loop_instance = mock_fb

    def _test_admin_conn():
        from src.backend.crew_ai.optimization import pg_compat
        return pg_compat.connect(dsn)

    patcher = _patch(
        "src.backend.api.learning_endpoints._admin_conn",
        side_effect=_test_admin_conn,
    )
    patcher.start()
    try:
        yield mock_fb
    finally:
        # Restore-before-close: the singleton must point at the original BEFORE
        # we stop the patch, so any concurrent get_feedback_loop() doesn't race.
        _lr_mod._feedback_loop_instance = saved_fb
        patcher.stop()


@pytest.fixture
def promote_client(learning_api_isolated):
    """TestClient wired to the learning router with an attached admin_token."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.backend.api.learning_endpoints import router
    from src.backend.auth.jwt_utils import create_access_token

    token = create_access_token({
        "id": "00000000-0000-0000-0000-000000000099",
        "email": "admin@test.local",
        "role": "admin",
        "display_name": "Test Admin",
        "org_id": None,
        "org_role": None,
    })

    app = FastAPI()
    app.include_router(router, prefix="/api/learning")

    with TestClient(app) as client:
        yield _PromoteClient(client, token)


# ---------------------------------------------------------------------------
# Task 12 — dash_client (org-scoped read + platform-admin-only telemetry/mutations)
# ---------------------------------------------------------------------------

_DASH_ORG_A = "org-a-task12"
_DASH_ORG_B = "org-b-task12"
_DASH_ORG_A_ADMIN_UID = "00000000-0000-0000-0000-000000000010"
_DASH_MEMBER_UID = "00000000-0000-0000-0000-000000000012"
_DASH_PLATFORM_ADMIN_UID = "00000000-0000-0000-0000-000000000099"


class _DashClient:
    """TestClient wrapper: exposes org tokens, seed helper, and org constants."""

    def __init__(self, client, org_a: str, org_b: str,
                 org_a_admin_token: str, member_token: str, dsn: str):
        self._client = client
        self.org_a = org_a
        self.org_b = org_b
        self.org_a_admin_token = org_a_admin_token
        self.member_token = member_token
        self._dsn = dsn

    def seed_run(self, org_id: str, workflow_id: str | None = None,
                with_trace: bool = False) -> str:
        """Insert an execution_records row scoped to *org_id* and return its workflow_id.

        Uses INSERT ... RETURNING workflow_id (idiomatic Postgres; avoids the fragile
        last_insert_rowid() pattern).  test_status='passed' satisfies the chk_status
        constraint.

        with_trace=True also inserts one hint_workflow_trace row for this workflow_id
        (hint_id=0, no FK constraint on that table).  This is required for the
        cross-org trace-leak anti-false-green test: without trace data, the existing
        ``run is None and not trace`` guard would 404 correctly even without the fix,
        masking the real vulnerability.
        """
        import uuid
        from datetime import datetime, timezone
        from src.backend.crew_ai.optimization import pg_compat

        if workflow_id is None:
            workflow_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        conn = pg_compat.connect(self._dsn)
        try:
            row = conn.execute(
                "INSERT INTO execution_records "
                "(workflow_id, timestamp, user_query, test_status, org_id) "
                "VALUES (?, ?, 'seed query', 'passed', ?) "
                "RETURNING workflow_id",
                (workflow_id, now, org_id),
            ).fetchone()
            if with_trace:
                # hint_id=0 — no FK constraint on hint_workflow_trace by design.
                # Exposes the trace without a real hint row.
                conn.execute(
                    "INSERT INTO hint_workflow_trace "
                    "(workflow_id, hint_id, available, injected, created_at) "
                    "VALUES (?, 0, 1, 1, ?)",
                    (workflow_id, now),
                )
            conn.commit()
            return row[0]
        finally:
            conn.close()

    def seed_hint(self, org_id: str, text: str) -> int:
        """Insert a hint scoped to *org_id* and return its id.

        Uses INSERT ... RETURNING id (idiomatic Postgres; avoids the fragile
        last_insert_rowid() pattern noted in the Task-10 review).  is_shared=0
        so the org-dimension privacy assertion is not accidentally passed by a
        shared-hint bypass.
        """
        from datetime import datetime, timezone
        from src.backend.crew_ai.optimization import pg_compat

        now = datetime.now(timezone.utc).isoformat()
        conn = pg_compat.connect(self._dsn)
        try:
            row = conn.execute(
                "INSERT INTO nl_feedback_corrections "
                "(feedback_text, category, scope, evidence_count, anchor_query, "
                " is_active, conflict_flagged, is_shared, org_id, created_at, last_seen) "
                "VALUES (?, 'test', 'global', 1, 'seed anchor', 1, 0, 0, ?, ?, ?) "
                "RETURNING id",
                (text, org_id, now, now),
            ).fetchone()
            conn.commit()
            return row[0]
        finally:
            conn.close()

    def __getattr__(self, name):
        return getattr(self._client, name)


@pytest.fixture
def dash_client(learning_api_isolated, api_pg_em):
    """TestClient for Task-12 org-scope tests.

    Provides:
    - org_a / org_b  — org-id strings for seeding
    - org_a_admin_token — role="user", org_role="org_admin", org_id=org_a
    - member_token      — role="user", org_role=None, org_id=org_a (no dashboard access)
    - seed_hint(org_id, text) — inserts a hint with RETURNING id

    Re-patches _admin_repo.get_by_id so require_admin actually rejects
    non-platform-admin tokens (the autouse _auth_not_enforced fixture makes it
    always return admin=True for any UID — we need role-aware behaviour here).
    """
    from unittest.mock import patch as _patch
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.backend.api.learning_endpoints import router
    from src.backend.auth.jwt_utils import create_access_token
    from src.backend.auth import jwt_utils

    _, dsn = api_pg_em

    org_a_admin_token = create_access_token({
        "id": _DASH_ORG_A_ADMIN_UID,
        "email": "org-a-admin@test.local",
        "role": "user",
        "display_name": "Org A Admin",
        "org_id": _DASH_ORG_A,
        "org_role": "org_admin",
    })
    member_token = create_access_token({
        "id": _DASH_MEMBER_UID,
        "email": "member@test.local",
        "role": "user",
        "display_name": "Member",
        "org_id": _DASH_ORG_A,
        "org_role": None,
    })

    def _smart_get_by_id(uid: str):
        """Return a DB-row-like dict based on whether the UID is a platform admin."""
        if uid == _DASH_PLATFORM_ADMIN_UID:
            return {"id": uid, "email": "admin@test.local", "role": "admin", "is_active": True}
        # org-admins and members are NOT platform admins
        return {"id": uid, "email": "user@test.local", "role": "user", "is_active": True}

    app = FastAPI()
    app.include_router(router, prefix="/api/learning")

    # Re-patch admin repo so require_admin correctly rejects org-admin tokens.
    # The autouse _auth_not_enforced fixture patches it to always return admin —
    # this inner patch overrides it within this fixture's scope.
    with _patch.object(jwt_utils._admin_repo, "get_by_id", side_effect=_smart_get_by_id):
        with TestClient(app) as client:
            yield _DashClient(client, _DASH_ORG_A, _DASH_ORG_B,
                              org_a_admin_token, member_token, dsn)


@pytest.fixture
def seeded_hint_id(api_pg_em) -> int:
    """Insert a hint + its kind='nl' anchor (org_id non-null) into the isolated schema.

    Returns the new hint id.  The anchor has org_id='org-test-1' so the
    anti-false-green assertion in test_promote_sets_is_shared can verify that
    promote nulls it (the column starts non-null, must end NULL).
    """
    from datetime import datetime, timezone
    from src.backend.crew_ai.optimization import pg_compat

    _, dsn = api_pg_em
    now = datetime.now(timezone.utc).isoformat()

    conn = pg_compat.connect(dsn)
    try:
        conn.execute(
            "INSERT INTO nl_feedback_corrections "
            "(feedback_text, category, scope, evidence_count, anchor_query, "
            " is_active, conflict_flagged, is_shared, created_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "use xpath locators for stable element selection",
                "locator", "global", 1, "find element by xpath",
                1, 0, 0, now, now,
            ),
        )
        hint_id: int = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Insert anchor with a non-null org_id so the anti-false-green assertion
        # can detect if the promote endpoint's anchor-null UPDATE is missing.
        # Embedding is a zero vector (valid 384-dim for schema compliance; not
        # used by the promote path which only does an UPDATE, not a similarity
        # search).
        zero_vec = "[" + ",".join(["0"] * 384) + "]"
        conn.execute(
            "INSERT INTO learning_anchors "
            "(anchor_key, kind, record_id, anchor_query, embedding, org_id) "
            "VALUES (?, ?, ?, ?, ?::vector, ?)",
            (f"nl:{hint_id}", "nl", hint_id, "find element by xpath", zero_vec, "org-test-1"),
        )
        conn.commit()
    finally:
        conn.close()

    return hint_id
