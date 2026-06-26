"""Phase 3: run_agentic_workflow binds org_id AND user_id at the generation entry."""

from unittest.mock import patch


def test_generation_binds_org_and_user():
    from src.backend.services import workflow_service as ws
    with patch.object(ws, "bind_workflow_context") as mock_bind:
        gen = ws.run_agentic_workflow(
            "login as admin", "gemini", "gemini-2.5-flash",
            org_id="org-A", user_id="user-1",
        )
        next(gen)  # advances past bind_workflow_context (called before the first yield)
        gen.close()
    assert mock_bind.called
    kwargs = mock_bind.call_args.kwargs
    assert kwargs.get("org_id") == "org-A"
    assert kwargs.get("user_id") == "user-1"
