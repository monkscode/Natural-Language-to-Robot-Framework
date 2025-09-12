import pytest
from unittest.mock import patch, MagicMock
from src.backend.crew_ai.agents import RobotAgents
from src.backend.crew_ai.tasks import RobotTasks
from crewai import Task
from crewai.task import TaskOutput

@patch('crewai.Task.execute_sync')
def test_identify_elements_task_with_advanced_prompt(mock_execute_sync):
    # 1. Arrange
    # The mock will be called when task.execute_sync() is called
    mock_execute_sync.return_value = TaskOutput(
        description="Mocked task description",
        raw="{\"locator\": \"xpath=//button[contains(text(), 'Submit')]\"}",
        agent="Mocked Agent"
    )

    agents = RobotAgents(model_provider="local", model_name="llama3")
    tasks = RobotTasks()
    element_identifier_agent = agents.element_identifier_agent()

    sample_context = [
        {
            "step_description": "Click the final submit button",
            "element_description": "The submit button which has text that might change slightly, like 'Submit Application' or 'Submit Form'",
            "value": "",
            "keyword": "Click Element"
        }
    ]

    # 2. Act
    task = tasks.identify_elements_task(element_identifier_agent)
    task.context = sample_context
    result = task.execute_sync()

    # 3. Assert
    assert result is not None
    assert result.raw == "{\"locator\": \"xpath=//button[contains(text(), 'Submit')]\"}"
    mock_execute_sync.assert_called_once()
