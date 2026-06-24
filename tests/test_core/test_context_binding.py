"""Phase 3: org_id/user_id bind into logs (structlog contextvars) and the OTel span/baggage."""

import structlog

from src.backend.config.logging_config import bind_workflow_context
from src.backend.core.observability import create_workflow_span


def test_bind_workflow_context_binds_org_and_user():
    bind_workflow_context("wf-1", org_id="org-A", user_id="user-1")
    ctx = structlog.contextvars.get_contextvars()
    assert ctx["workflow_id"] == "wf-1"
    assert ctx["org_id"] == "org-A"
    assert ctx["user_id"] == "user-1"
    structlog.contextvars.clear_contextvars()


def test_bind_workflow_context_omits_identity_when_none():
    bind_workflow_context("wf-2")
    ctx = structlog.contextvars.get_contextvars()
    assert ctx["workflow_id"] == "wf-2"
    assert "org_id" not in ctx and "user_id" not in ctx
    structlog.contextvars.clear_contextvars()


def test_create_workflow_span_sets_identity_baggage():
    from opentelemetry import baggage
    with create_workflow_span("wf-3", "login", "gemini", "gemini-2.5-flash",
                              org_id="org-A", user_id="user-1"):
        assert baggage.get_baggage("workflow.org_id") == "org-A"
        assert baggage.get_baggage("workflow.user_id") == "user-1"
