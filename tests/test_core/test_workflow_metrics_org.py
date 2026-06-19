"""workflow_metrics gains org_id (written + backfilled from the run).

Insert a metric for a workflow_id whose test_runs row has org_id; assert
record_workflow(..., org_id=...) persists it and backfill_org_ids() fills NULLs.

Referenced by: tests/test_core/
Depends on: src/backend/core/workflow_metrics.py, src/backend/core/run_registry.py
"""
import uuid
from datetime import datetime

import psycopg
import pytest

from src.backend.core.config import PG_CONNECT_TIMEOUT_S, settings
from src.backend.core.models.workflow_metrics_models import WorkflowMetrics

_SCHEMA = "wf_metrics_org_test"


@pytest.fixture(scope="module")
def _shared(request):
    """Isolated schema shared across the module; torn down after all tests."""
    from src.backend.core.workflow_metrics import WorkflowMetricsCollector
    from src.backend.core.run_registry import RunRegistry

    try:
        admin = psycopg.connect(
            settings.DATABASE_URL, autocommit=True,
            connect_timeout=PG_CONNECT_TIMEOUT_S,
        )
    except Exception as exc:
        pytest.skip(f"Postgres unavailable: {exc}")

    admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    admin.execute(f"SET search_path TO {_SCHEMA}")
    dsn = settings.DATABASE_URL + f"?options=-c%20search_path%3D{_SCHEMA}"

    collector = WorkflowMetricsCollector(dsn=dsn)
    registry = RunRegistry(dsn=dsn)

    yield collector, registry, admin, dsn

    collector.close()
    registry.close()
    admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    admin.close()


@pytest.fixture(autouse=True)
def _truncate(_shared):
    """Truncate both tables before each test for isolation."""
    _collector, _registry, admin, _dsn = _shared
    admin.execute("TRUNCATE workflow_metrics")
    admin.execute("TRUNCATE test_runs")


def _make_metric(workflow_id: str) -> WorkflowMetrics:
    return WorkflowMetrics(
        workflow_id=workflow_id,
        url="http://example.com",
        total_llm_calls=1,
        total_cost=0.01,
        execution_time=1.0,
        timestamp=datetime.now(),
    )


class TestRecordWorkflowWithOrgId:
    """record_workflow persists org_id when supplied."""

    def test_org_id_persisted(self, _shared):
        collector, _registry, admin, _dsn = _shared
        wf_id = str(uuid.uuid4())
        org_id = "org-" + str(uuid.uuid4())

        collector.record_workflow(_make_metric(wf_id), org_id=org_id)

        rows = admin.execute(
            "SELECT org_id FROM workflow_metrics WHERE workflow_id = %s",
            (wf_id,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == org_id

    def test_org_id_none_when_not_supplied(self, _shared):
        collector, _registry, admin, _dsn = _shared
        wf_id = str(uuid.uuid4())

        collector.record_workflow(_make_metric(wf_id))

        rows = admin.execute(
            "SELECT org_id FROM workflow_metrics WHERE workflow_id = %s",
            (wf_id,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] is None


class TestBackfillOrgIds:
    """backfill_org_ids() fills NULL org_id from test_runs.org_id via workflow_id."""

    def test_backfill_fills_null(self, _shared):
        collector, registry, admin, _dsn = _shared
        wf_id = str(uuid.uuid4())
        org_id = "org-" + str(uuid.uuid4())

        # Insert a test_runs row with org_id
        registry.record_start(
            wf_id,
            user={"user_id": "user-1", "email": "u@example.com", "org_id": org_id},
            user_query="test query",
            status="generating",
        )

        # Insert a workflow_metrics row without org_id
        collector.record_workflow(_make_metric(wf_id))

        # Verify it is NULL before backfill
        rows = admin.execute(
            "SELECT org_id FROM workflow_metrics WHERE workflow_id = %s",
            (wf_id,),
        ).fetchall()
        assert rows[0][0] is None

        count = collector.backfill_org_ids()
        assert count == 1

        rows = admin.execute(
            "SELECT org_id FROM workflow_metrics WHERE workflow_id = %s",
            (wf_id,),
        ).fetchall()
        assert rows[0][0] == org_id

    def test_backfill_idempotent(self, _shared):
        """Calling backfill_org_ids() twice does not double-count."""
        collector, registry, admin, _dsn = _shared
        wf_id = str(uuid.uuid4())
        org_id = "org-" + str(uuid.uuid4())

        registry.record_start(
            wf_id,
            user={"user_id": "user-1", "email": "u@example.com", "org_id": org_id},
            user_query="test",
            status="generating",
        )
        collector.record_workflow(_make_metric(wf_id))

        first = collector.backfill_org_ids()
        second = collector.backfill_org_ids()
        assert first == 1
        assert second == 0  # already filled

    def test_backfill_skips_already_filled(self, _shared):
        """Rows that already have org_id are left untouched."""
        collector, registry, admin, _dsn = _shared
        wf_id = str(uuid.uuid4())
        org_id = "org-existing"

        registry.record_start(
            wf_id,
            user={"user_id": "user-1", "email": "u@example.com", "org_id": "org-from-run"},
            user_query="test",
            status="generating",
        )
        collector.record_workflow(_make_metric(wf_id), org_id=org_id)

        count = collector.backfill_org_ids()
        assert count == 0

        rows = admin.execute(
            "SELECT org_id FROM workflow_metrics WHERE workflow_id = %s",
            (wf_id,),
        ).fetchall()
        assert rows[0][0] == org_id  # unchanged

    def test_backfill_no_matching_run_returns_zero(self, _shared):
        """Metric with no test_runs row → backfill returns 0."""
        collector, _registry, admin, _dsn = _shared
        wf_id = str(uuid.uuid4())

        collector.record_workflow(_make_metric(wf_id))
        count = collector.backfill_org_ids()
        assert count == 0
