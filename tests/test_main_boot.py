"""Boot smoke test for src.backend.main — the full app wiring.

main.py is process wiring: middleware order, router registration, startup
guards. None of it executes in the unit suites (they mount bare routers), so a
broken import, a router that fails to register, or a hole in the /reports auth
middleware would only surface when the real server boots — i.e. in front of
users. Booting the real app under TestClient catches that class of failure in
CI.

Importing main pulls the whole crewai/litellm chain (slow, once per session)
and startup touches Postgres best-effort (init_auth_db / learning health are
non-blocking by design). The learning feedback loop is patched to None at
shutdown so the test never constructs the live learning stack.
"""

from unittest.mock import patch

import pytest

from src.backend.core.config import settings

pytestmark = pytest.mark.integration

_TEST_SECRET = "b2Yt7Rq4Kx9Vn3Ls6Wd0Hf5Zj8Mc1Pg4Tb7Yn2Qs5Xk8Dv1Ru4Gh7Lm0Ap3Ce6"


@pytest.fixture(scope="module", autouse=True)
def _configured_jwt_secret():
    """Boot with a configured secret so startup neither aborts nor provisions a
    development secret into the working tree."""
    with patch.object(settings, "JWT_SECRET_KEY", _TEST_SECRET):
        yield


@pytest.fixture(scope="module")
def app_client():
    from fastapi.testclient import TestClient

    from src.backend import main
    from src.backend.crew_ai.optimization import learning_registry

    # Shutdown drains the feedback loop; returning None keeps the test from
    # building the real learning stack against the live schema.
    with patch.object(learning_registry, "get_feedback_loop", return_value=None):
        with TestClient(main.app) as client:
            yield client


def test_health_endpoints_respond(app_client):
    resp = app_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"
    assert app_client.get("/api/health").status_code == 200


def test_reports_are_auth_gated(app_client):
    """log.html records credentials typed during tests — /reports must never
    be publicly readable when auth is enforced."""
    with patch.object(settings, "AUTH_ENFORCED", True):
        denied = app_client.get("/reports/some-run/log.html")
        assert denied.status_code == 401
        # A garbage token is 401 even though the path would 404.
        bad = app_client.get(
            "/reports/some-run/log.html",
            headers={"Authorization": "Bearer not-a-jwt"},
        )
        assert bad.status_code == 401
        # A valid USER token passes authentication, but report OWNERSHIP
        # fails closed: an unknown/unattributed run is 403, never a 404 probe.
        # Every request now re-validates the token against the users table
        # (revocation check), so the caller must resolve to an active row —
        # mock it, mirroring the admin branch below.
        from src.backend.auth import jwt_utils
        from src.backend.auth.jwt_utils import create_access_token
        token = create_access_token(
            {"id": "00000000-0000-0000-0000-000000000000", "email": "t@t.t",
             "role": "user", "display_name": "t"})
        with patch.object(
            jwt_utils._admin_repo, "get_by_id",
            return_value={"id": "00000000-0000-0000-0000-000000000000",
                          "email": "t@t.t", "role": "user",
                          "is_active": True, "token_version": 0, "status": "active"},
        ):
            denied_owner = app_client.get(
                "/reports/no-such-run/log.html",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert denied_owner.status_code == 403
        # A validated admin clears the ownership gate; the 404 then comes from
        # the route's file resolver (no such run/file) — proving auth let it
        # through rather than denying.
        admin_token = create_access_token(
            {"id": "00000000-0000-0000-0000-000000000001", "email": "a@t.t",
             "role": "admin", "display_name": "a"})
        with patch.object(
            jwt_utils._admin_repo, "get_by_id",
            return_value={"id": "x", "email": "a@t.t", "role": "admin",
                          "is_active": True, "status": "active"},
        ):
            ok = app_client.get(
                "/reports/no-such-run/log.html",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
        assert ok.status_code == 404


def test_admin_dashboards_reject_anonymous_requests(app_client):
    """require_admin is strict even with the AUTH_ENFORCED escape hatch off."""
    with patch.object(settings, "AUTH_ENFORCED", False):
        for path in ("/api/workflow-metrics/summary", "/api/learning/health"):
            resp = app_client.get(path)
            assert resp.status_code == 401, f"{path} must not be open"


def test_auth_router_is_mounted(app_client):
    # Unknown credentials -> 401 proves the route exists and runs its logic
    # (an unmounted router would 404).
    resp = app_client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": "wrong-pass"})
    assert resp.status_code == 401


def test_startup_and_shutdown_survive_every_collaborator_failing():
    """Postgres down, temp-metrics cleanup broken, every shutdown hook
    raising — the app must still boot, serve, and exit cleanly. A pending
    learning queue must still be drained on shutdown."""
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from src.backend import main
    from src.backend.core import temp_metrics_storage
    from src.backend.crew_ai.optimization import keyword_vector_store, learning_registry

    loop = MagicMock()
    loop.execution_memory.close.side_effect = RuntimeError("store already closed")
    with patch.object(main, "init_auth_db", side_effect=RuntimeError("pg down")), \
         patch.object(temp_metrics_storage, "get_temp_metrics_storage",
                      side_effect=RuntimeError("disk error")), \
         patch.object(main, "_check_learning_health"), \
         patch.object(learning_registry, "get_feedback_loop", return_value=loop), \
         patch.object(keyword_vector_store, "close_keyword_vector_store",
                      side_effect=RuntimeError("store gone")), \
         patch.object(main, "close_pool", side_effect=RuntimeError("pool gone")):
        with TestClient(main.app) as client:
            assert client.get("/health").status_code == 200
    loop.write_queue.shutdown.assert_called_once()
    loop.execution_memory.close.assert_called_once()


def test_learning_health_check_degrades_and_never_blocks_boot():
    from unittest.mock import MagicMock

    from src.backend import main
    from src.backend.crew_ai.optimization import pg_schema

    # Disabled: informational log only.
    with patch.object(settings, "OPTIMIZATION_ENABLED", False):
        main._check_learning_health()
    # Postgres unreachable: warning, no raise.
    with patch.object(settings, "OPTIMIZATION_ENABLED", True), \
         patch("psycopg.connect", side_effect=RuntimeError("connection refused")):
        main._check_learning_health()
    # Reachable but pgvector missing: warning, no raise.
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = None  # no 'vector' extension
    with patch.object(settings, "OPTIMIZATION_ENABLED", True), \
         patch("psycopg.connect", return_value=conn), \
         patch.object(pg_schema, "ensure_schema"):
        main._check_learning_health()
    conn.close.assert_called_once()  # connection released even on degraded path


def test_startup_refuses_placeholder_jwt_secret_in_production():
    """A production deployment must abort on the placeholder: every minted token
    would be forgeable. Development provisions a real secret instead."""
    from fastapi.testclient import TestClient

    from src.backend import main

    with patch.object(settings, "JWT_SECRET_KEY", "change-me-in-production"), \
         patch.object(settings, "ENVIRONMENT", "production"):
        with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
            with TestClient(main.app):
                pass
