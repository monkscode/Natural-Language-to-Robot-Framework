"""Org-scoped metrics dashboards: org-admin sees only their org's metrics; member gets 403.

Scenario:
- User A and user B register (each becomes org-admin of their personal org).
- A workflow_metrics row is inserted under org A's org_id.
- GET /api/workflow-metrics/ with A's token: must include A's metric.
- GET /api/workflow-metrics/ with B's token: must return 200 but NOT include A's metric.
- GET /api/workflow-metrics/ with a plain org-member token: must return 403.
- GET /api/workflow-metrics/aggregate with org-member token: must return 403.
- GET /api/workflow-metrics/summary with org-member token: must return 403.

The route paths are /api/workflow-metrics/ (prefix="/api" in main.py +
prefix="/workflow-metrics" in the router).

Referenced by: src/backend/api/workflow_metrics_endpoints.py,
               src/backend/core/workflow_metrics.py.
Depends on: tests/test_auth/conftest.py (auth_isolated_schema).
"""

import json
import uuid

import psycopg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]

_METRICS_SCHEMA = "metrics_org_scope_test"


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from src.backend.main import app
    return TestClient(app)


@pytest.fixture(scope="module")
def metrics_schema():
    """Create an isolated workflow_metrics schema and seed one metric row for org A."""
    from src.backend.core.config import settings
    from src.backend.core.workflow_metrics import WorkflowMetricsCollector

    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    admin.execute(f"DROP SCHEMA IF EXISTS {_METRICS_SCHEMA} CASCADE")
    admin.execute(f"CREATE SCHEMA {_METRICS_SCHEMA}")
    sep = "&" if "?" in settings.DATABASE_URL else "?"
    dsn = settings.DATABASE_URL + f"{sep}options=-c%20search_path%3D{_METRICS_SCHEMA}"
    # Use WorkflowMetricsCollector to create the schema in our isolated schema
    collector = WorkflowMetricsCollector(dsn=dsn)
    yield {"dsn": dsn, "collector": collector}
    collector.close()
    admin.execute(f"DROP SCHEMA IF EXISTS {_METRICS_SCHEMA} CASCADE")
    admin.close()


def _register(client, email):
    r = client.post("/auth/register", json={"email": email, "password": "S3cretpw!"})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _member_token(org_id: str) -> str:
    """Mint a JWT for a synthetic org_member without touching the DB."""
    from src.backend.auth.jwt_utils import create_access_token
    return create_access_token({
        "id": str(uuid.uuid4()),
        "email": f"member-{uuid.uuid4().hex[:6]}@e.com",
        "role": "user",
        "display_name": "Member",
        "org_id": org_id,
        "org_role": "org_member",
    })


class TestMetricsOrgScope:
    """Integration tests for org-scoped workflow metrics dashboard access."""

    @pytest.fixture(scope="class")
    def setup(self, client, metrics_schema):
        """Register two users and insert a metric row belonging to org A."""
        from src.backend.auth.jwt_utils import decode_token
        from src.backend.core.models import WorkflowMetrics
        from datetime import datetime

        tok_a = _register(client, f"ma-{uuid.uuid4().hex[:8]}@e.com")
        tok_b = _register(client, f"mb-{uuid.uuid4().hex[:8]}@e.com")
        a = decode_token(tok_a)
        b = decode_token(tok_b)

        org_a = a["org_id"]

        # Insert a metric row that belongs to org A
        workflow_id = str(uuid.uuid4())
        metric = WorkflowMetrics(
            workflow_id=workflow_id,
            url="https://example.com",
            total_llm_calls=3,
            total_cost=0.005,
            execution_time=1.2,
            timestamp=datetime.now(),
        )
        # Record it with org_a's org_id using the isolated collector
        metrics_schema["collector"].record_workflow(metric, org_id=org_a)

        yield {
            "tok_a": tok_a,
            "tok_b": tok_b,
            "org_a": org_a,
            "org_b": b["org_id"],
            "workflow_id": workflow_id,
            "metrics_dsn": metrics_schema["dsn"],
            "collector": metrics_schema["collector"],
        }

    # ------------------------------------------------------------------
    # Org-member 403 on all dashboard read routes
    # ------------------------------------------------------------------

    def test_org_member_gets_403_list(self, client, setup):
        """A plain org-member is denied access to the metrics list (403)."""
        from unittest.mock import patch

        member_tok = _member_token(setup["org_a"])

        with patch(
            "src.backend.api.workflow_metrics_endpoints.get_workflow_metrics_collector",
            return_value=setup["collector"],
        ):
            resp = client.get(
                "/api/workflow-metrics/",
                headers={"Authorization": f"Bearer {member_tok}"},
            )
        assert resp.status_code == 403

    def test_org_member_gets_403_aggregate(self, client, setup):
        """A plain org-member is denied access to aggregate metrics (403)."""
        from unittest.mock import patch

        member_tok = _member_token(setup["org_a"])

        with patch(
            "src.backend.api.workflow_metrics_endpoints.get_workflow_metrics_collector",
            return_value=setup["collector"],
        ):
            resp = client.get(
                "/api/workflow-metrics/aggregate",
                headers={"Authorization": f"Bearer {member_tok}"},
            )
        assert resp.status_code == 403

    def test_org_member_gets_403_summary(self, client, setup):
        """A plain org-member is denied access to metrics summary (403)."""
        from unittest.mock import patch

        member_tok = _member_token(setup["org_a"])

        with patch(
            "src.backend.api.workflow_metrics_endpoints.get_workflow_metrics_collector",
            return_value=setup["collector"],
        ):
            resp = client.get(
                "/api/workflow-metrics/summary",
                headers={"Authorization": f"Bearer {member_tok}"},
            )
        assert resp.status_code == 403

    # ------------------------------------------------------------------
    # Org-admin A sees their own org's metrics
    # ------------------------------------------------------------------

    def test_org_admin_a_sees_own_metrics(self, client, setup):
        """Org-admin A gets 200 and their metric row is present."""
        from unittest.mock import patch

        with patch(
            "src.backend.api.workflow_metrics_endpoints.get_workflow_metrics_collector",
            return_value=setup["collector"],
        ):
            resp = client.get(
                "/api/workflow-metrics/",
                headers={"Authorization": f"Bearer {setup['tok_a']}"},
            )
        assert resp.status_code == 200
        wf_ids = [m["workflow_id"] for m in resp.json()]
        assert setup["workflow_id"] in wf_ids, f"A's metric not found; got wf_ids={wf_ids}"

    # ------------------------------------------------------------------
    # Org-admin B cannot see org A's metrics (cross-org isolation)
    # ------------------------------------------------------------------

    def test_org_admin_b_excluded_from_a_metrics(self, client, setup):
        """Org-admin B gets 200 but cannot see org A's metric row."""
        from unittest.mock import patch

        with patch(
            "src.backend.api.workflow_metrics_endpoints.get_workflow_metrics_collector",
            return_value=setup["collector"],
        ):
            resp = client.get(
                "/api/workflow-metrics/",
                headers={"Authorization": f"Bearer {setup['tok_b']}"},
            )
        assert resp.status_code == 200
        wf_ids = [m["workflow_id"] for m in resp.json()]
        assert setup["workflow_id"] not in wf_ids, (
            f"B must NOT see A's metric; got wf_ids={wf_ids}"
        )

    def test_org_admin_b_aggregate_excludes_a_metrics(self, client, setup):
        """Org-admin B's aggregate sees 0 workflows (only org B's data, which is empty)."""
        from unittest.mock import patch

        with patch(
            "src.backend.api.workflow_metrics_endpoints.get_workflow_metrics_collector",
            return_value=setup["collector"],
        ):
            resp = client.get(
                "/api/workflow-metrics/aggregate",
                headers={"Authorization": f"Bearer {setup['tok_b']}"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_workflows"] == 0, (
            f"B must see 0 workflows in aggregate (no org B data); got {body}"
        )

    def test_org_admin_a_aggregate_includes_own_metrics(self, client, setup):
        """Org-admin A's aggregate reflects their org's metric row (positive control)."""
        from unittest.mock import patch

        with patch(
            "src.backend.api.workflow_metrics_endpoints.get_workflow_metrics_collector",
            return_value=setup["collector"],
        ):
            resp = client.get(
                "/api/workflow-metrics/aggregate",
                headers={"Authorization": f"Bearer {setup['tok_a']}"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_workflows"] >= 1, (
            f"A must see at least 1 workflow in aggregate; got {body}"
        )
