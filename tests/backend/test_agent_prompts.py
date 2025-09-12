import pytest
from unittest.mock import patch, MagicMock
from src.backend.crew_ai.agents import RobotAgents
from src.backend.crew_ai.tasks import RobotTasks
from crewai import Task
from crewai.task import TaskOutput
from tests.backend.test_prompts import (
    IDENTIFY_ELEMENTS_TASK_MOCKED_OUTPUT,
    PLAN_STEPS_TASK_MOCKED_OUTPUT,
)

@patch('crewai.Task.execute_sync')
def test_plan_steps_task_with_advanced_prompt(mock_execute_sync):
    # 1. Arrange
    mock_execute_sync.return_value = TaskOutput(
        description="Mocked task description",
        raw=PLAN_STEPS_TASK_MOCKED_OUTPUT,
        agent="Mocked Agent"
    )

    agents = RobotAgents(model_provider="local", model_name="llama3")
    tasks = RobotTasks()
    planner_agent = agents.step_planner_agent()

    # 2. Act
    task = tasks.plan_steps_task(planner_agent, "Log in to the application with username 'myuser' and password 'mypassword'")
    result = task.execute_sync()

    # 3. Assert
    assert result is not None
    import json
    parsed_result = json.loads(result.raw)
    assert len(parsed_result) == 4
    assert parsed_result[0]['keyword'] == 'Open Browser'
    assert parsed_result[1]['keyword'] == 'Input Text'
    assert parsed_result[1]['value'] == 'myuser'
    assert parsed_result[2]['keyword'] == 'Input Text'
    assert parsed_result[2]['value'] == 'mypassword'
    assert parsed_result[3]['keyword'] == 'Click Element'
    mock_execute_sync.assert_called_once()


@patch('crewai.Task.execute_sync')
def test_identify_elements_task_with_advanced_prompt(mock_execute_sync):
    # 1. Arrange
    # The mock will be called when task.execute_sync() is called
    mock_execute_sync.return_value = TaskOutput(
        description="Mocked task description",
        raw=IDENTIFY_ELEMENTS_TASK_MOCKED_OUTPUT,
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
    assert result.raw == IDENTIFY_ELEMENTS_TASK_MOCKED_OUTPUT
    mock_execute_sync.assert_called_once()
