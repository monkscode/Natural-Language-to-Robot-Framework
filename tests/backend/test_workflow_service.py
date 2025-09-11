import pytest
from unittest.mock import patch, MagicMock
from src.backend.services.workflow_service import run_agentic_workflow, stream_generate_and_run

@patch('os.getenv')
@patch('src.backend.services.workflow_service.run_crew')
def test_run_agentic_workflow_success(mock_run_crew, mock_getenv):
    mock_getenv.return_value = "dummy_key"
    mock_crew_results = MagicMock()
    mock_crew_results.tasks = [MagicMock(), MagicMock(), MagicMock(output=MagicMock(raw='```robotframework\nTest Code\n```')), MagicMock(output=MagicMock(raw='{"valid": true}'))]
    mock_run_crew.return_value = (None, mock_crew_results)

    events = list(run_agentic_workflow("test query", "online", "gemini-1.5-pro-latest"))

    assert len(events) == 2
    assert events[0]['status'] == 'running'
    assert events[1]['status'] == 'complete'
    assert events[1]['robot_code'] == 'Test Code'

@patch('os.getenv')
@patch('src.backend.services.workflow_service.run_crew')
def test_run_agentic_workflow_validation_fails(mock_run_crew, mock_getenv):
    mock_getenv.return_value = "dummy_key"
    mock_crew_results = MagicMock()
    mock_crew_results.tasks = [MagicMock(), MagicMock(), MagicMock(output=MagicMock(raw='Test Code')), MagicMock(output=MagicMock(raw='{"valid": false, "reason": "Syntax error"}'))]
    mock_run_crew.return_value = (None, mock_crew_results)

    events = list(run_agentic_workflow("test query", "online", "gemini-1.5-pro-latest"))

    assert len(events) == 2
    assert events[1]['status'] == 'error'
    assert 'validation failed' in events[1]['message']

@pytest.mark.asyncio
async def test_stream_generate_and_run():
    # This is a more complex test and would require more setup to test properly.
    # For now, we will just test the basic flow.
    with patch('src.backend.services.workflow_service.run_workflow_in_thread'), \
         patch('src.backend.services.workflow_service.get_docker_client'), \
         patch('src.backend.services.workflow_service.build_image'), \
         patch('src.backend.services.workflow_service.run_test_in_container'):

        events = [event async for event in stream_generate_and_run("test query", "online", "gemini-1.5-pro-latest")]
        # This is not a complete test, but it's a start.
        # A more complete test would require mocking the queue and the thread.
        assert len(events) > 0
