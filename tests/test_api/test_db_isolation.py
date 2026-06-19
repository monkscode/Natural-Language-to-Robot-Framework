"""Isolation test: integration tests must not write data-plane rows to public.

Verifies that auth_isolated_schema redirects test_runs (and workflow_metrics)
writes from the app singletons to auth_test, never to public.

Referenced by: task-16 brief (CLAUDE.md invariant — tests must not write to live DB).
Depends on: tests/test_auth/conftest.py (auth_isolated_schema), core singletons.
"""

import uuid

import psycopg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


@pytest.fixture(scope="module")
def admin_public():
    """Raw connection to public (no search_path override) for assertion reads."""
    from src.backend.core.config import settings
    conn = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    yield conn
    conn.close()


def test_run_registry_singleton_writes_to_auth_test_not_public(admin_public):
    """get_run_registry() must write test_runs into auth_test, not public.

    Before the fix the singleton is built from settings.DATABASE_URL (public),
    so the row lands in public.test_runs — this assertion fails RED.
    After the fixture injects an isolated singleton, the row lands in auth_test.
    """
    from src.backend.core.run_registry import get_run_registry

    run_id = str(uuid.uuid4())
    get_run_registry().record_start(
        run_id,
        {"user_id": "test-user", "email": "isolation@e.com", "org_id": "test-org"},
        "isolation check query",
        "generating",
    )

    # Must NOT be in public.test_runs
    row_public = admin_public.execute(
        "SELECT run_id FROM public.test_runs WHERE run_id = %s",
        (run_id,),
    ).fetchone()
    assert row_public is None, (
        f"run_id {run_id} found in public.test_runs — singleton is NOT isolated"
    )

    # MUST be in auth_test.test_runs
    row_auth = admin_public.execute(
        "SELECT run_id FROM auth_test.test_runs WHERE run_id = %s",
        (run_id,),
    ).fetchone()
    assert row_auth is not None, (
        f"run_id {run_id} not found in auth_test.test_runs — isolation not wired"
    )


def test_workflow_metrics_singleton_writes_to_auth_test_not_public(admin_public):
    """get_workflow_metrics_collector() must write workflow_metrics into auth_test.

    Mirrors the test_runs check for the metrics table.
    """
    from datetime import datetime
    from src.backend.core.workflow_metrics import get_workflow_metrics_collector
    from src.backend.core.models.workflow_metrics_models import WorkflowMetrics

    wf_id = str(uuid.uuid4())
    metrics = WorkflowMetrics(
        workflow_id=wf_id,
        url="http://example.com",
        total_llm_calls=1,
        total_cost=0.0,
        execution_time=1.0,
        timestamp=datetime.now(),
    )
    get_workflow_metrics_collector().record_workflow(metrics, org_id="test-org")

    # Must NOT be in public.workflow_metrics
    row_public = admin_public.execute(
        "SELECT workflow_id FROM public.workflow_metrics WHERE workflow_id = %s",
        (wf_id,),
    ).fetchone()
    assert row_public is None, (
        f"workflow_id {wf_id} found in public.workflow_metrics — metrics singleton is NOT isolated"
    )

    # MUST be in auth_test.workflow_metrics
    row_auth = admin_public.execute(
        "SELECT workflow_id FROM auth_test.workflow_metrics WHERE workflow_id = %s",
        (wf_id,),
    ).fetchone()
    assert row_auth is not None, (
        f"workflow_id {wf_id} not found in auth_test.workflow_metrics — isolation not wired"
    )
