"""Unit tests for healing agents and tasks."""

import pytest
import json
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

from src.backend.crew_ai.healing_agents import HealingAgents
from src.backend.crew_ai.healing_tasks import HealingTasks
from src.backend.core.models.healing_models import (
    FailureContext, FailureType, LocatorStrategy, HealingStatus
)


class TestHealingAgents:
    """Test cases for healing agents."""
    
    @pytest.fixture
    def healing_agents(self):
        """Create HealingAgents instance for testing."""
        return HealingAgents("local", "test-model")
    
    def test_failure_analysis_agent_creation(self, healing_agents):
        """Test that failure analysis agent is created correctly."""
        agent = healing_agents.failure_analysis_agent()
        
        assert agent.role == "Test Failure Analysis Specialist"
        assert "analyze test failures" in agent.goal.lower()
        assert "selenium webdriver" in agent.backstory.lower()
        assert agent.verbose is True
        assert agent.allow_delegation is False
    
    def test_locator_generation_agent_creation(self, healing_agents):
        """Test that locator generation agent is created correctly."""
        agent = healing_agents.locator_generation_agent()
        
        assert agent.role == "Advanced Web Element Locator Specialist"
        assert "alternative locators" in agent.goal.lower()
        assert "element fingerprinting" in agent.backstory.lower()
        assert agent.verbose is True
        assert agent.allow_delegation is False
    
    def test_locator_validation_agent_creation(self, healing_agents):
        """Test that locator validation agent is created correctly."""
        agent = healing_agents.locator_validation_agent()
        
        assert agent.role == "Live Locator Validation Specialist"
        assert "validate alternative locators" in agent.goal.lower()
        assert "selenium webdriver" in agent.backstory.lower()
        assert agent.verbose is True
        assert agent.allow_delegation is False


class TestHealingTasks:
    """Test cases for healing tasks."""
    
    @pytest.fixture
    def healing_tasks(self):
        """Create HealingTasks instance for testing."""
        return HealingTasks()
    
    @pytest.fixture
    def mock_agent(self):
        """Create real agent for testing (CrewAI doesn't work with mocks)."""
        from src.backend.crew_ai.healing_agents import HealingAgents
        healing_agents = HealingAgents("local", "test-model")
        return healing_agents.failure_analysis_agent()
    
    @pytest.fixture
    def sample_failure_context(self):
        """Create sample failure context for testing."""
        return {
            'test_file': 'test_login.robot',
            'test_case': 'Valid Login Test',
            'failing_step': 'Click Element    id=login-button',
            'original_locator': 'id=login-button',
            'target_url': 'https://example.com/login',
            'exception_type': 'NoSuchElementException',
            'exception_message': 'Unable to locate element: {"method":"id","selector":"login-button"}'
        }
    
    def test_analyze_failure_task_creation(self, healing_tasks, mock_agent, sample_failure_context):
        """Test that failure analysis task is created correctly."""
        task = healing_tasks.analyze_failure_task(mock_agent, sample_failure_context)
        
        assert task.agent == mock_agent
        assert "analyze the provided test failure" in task.description.lower()
        assert sample_failure_context['test_file'] in task.description
        assert sample_failure_context['original_locator'] in task.description
        assert "json object" in task.expected_output.lower()
    
    def test_generate_alternative_locators_task_creation(self, healing_tasks, mock_agent):
        """Test that locator generation task is created correctly."""
        failure_analysis = {
            "is_healable": True,
            "failure_type": "element_not_found",
            "element_type": "button",
            "action_intent": "click"
        }
        
        task = healing_tasks.generate_alternative_locators_task(mock_agent, failure_analysis)
        
        assert task.agent == mock_agent
        assert "generate multiple alternative locators" in task.description.lower()
        assert "priority order" in task.description.lower()
        assert "robot framework syntax" in task.description.lower()
        assert "json object" in task.expected_output.lower()
    
    def test_validate_locators_task_creation(self, healing_tasks, mock_agent):
        """Test that locator validation task is created correctly."""
        locator_candidates = [
            {
                "locator": "css=button.login-btn",
                "strategy": "css",
                "confidence": 0.8
            },
            {
                "locator": "xpath=//button[contains(text(), 'Login')]",
                "strategy": "xpath", 
                "confidence": 0.7
            }
        ]
        
        validation_context = {
            "target_url": "https://example.com/login",
            "element_type": "button",
            "action_intent": "click"
        }
        
        task = healing_tasks.validate_locators_task(mock_agent, locator_candidates, validation_context)
        
        assert task.agent == mock_agent
        assert "validate the provided alternative locators" in task.description.lower()
        assert "live browser session" in task.description.lower()
        assert validation_context['target_url'] in task.description
        assert "json object" in task.expected_output.lower()
    
    def test_healing_orchestration_task_creation(self, healing_tasks, mock_agent):
        """Test that healing orchestration task is created correctly."""
        healing_session_data = {
            "session_id": "test-session-123",
            "failure_context": {
                "test_file": "test_login.robot",
                "original_locator": "id=login-button"
            }
        }
        
        task = healing_tasks.healing_orchestration_task(mock_agent, healing_session_data)
        
        assert task.agent == mock_agent
        assert "orchestrate the complete healing workflow" in task.description.lower()
        assert "phase 1: failure analysis" in task.description.lower()
        assert "phase 2: locator generation" in task.description.lower()
        assert "phase 3: validation" in task.description.lower()
        assert "json object" in task.expected_output.lower()


class TestAgentPromptEngineering:
    """Test cases for agent prompt engineering and response parsing."""
    
    @pytest.fixture
    def healing_agents(self):
        """Create HealingAgents instance for testing."""
        return HealingAgents("local", "test-model")
    
    @pytest.fixture
    def healing_tasks(self):
        """Create HealingTasks instance for testing."""
        return HealingTasks()
    
    def test_failure_analysis_prompt_structure(self, healing_tasks, healing_agents):
        """Test that failure analysis prompt has correct structure."""
        agent = healing_agents.failure_analysis_agent()
        failure_context = {
            'test_file': 'test.robot',
            'exception_type': 'NoSuchElementException',
            'original_locator': 'id=test-button'
        }
        
        task = healing_tasks.analyze_failure_task(agent, failure_context)
        description = task.description
        
        # Check for key sections
        assert "ANALYSIS CRITERIA" in description
        assert "Healable Failure Types" in description
        assert "Non-Healable Failure Types" in description
        assert "CONTEXT EXTRACTION" in description
        assert "OUTPUT FORMAT" in description
        
        # Check for specific failure types
        assert "NoSuchElementException" in description
        assert "ElementNotInteractableException" in description
        assert "TimeoutException" in description
        
        # Check for JSON structure requirements
        assert '"is_healable": boolean' in description
        assert '"failure_type":' in description
        assert '"confidence":' in description
    
    def test_locator_generation_prompt_structure(self, healing_tasks, healing_agents):
        """Test that locator generation prompt has correct structure."""
        agent = healing_agents.locator_generation_agent()
        failure_analysis = {"is_healable": True, "element_type": "button"}
        
        task = healing_tasks.generate_alternative_locators_task(agent, failure_analysis)
        description = task.description
        
        # Check for key sections
        assert "LOCATOR GENERATION STRATEGY" in description
        assert "Priority Order" in description
        assert "Generation Rules" in description
        assert "Robot Framework Syntax" in description
        assert "OUTPUT FORMAT" in description
        
        # Check for locator strategies
        assert "ID-based locators" in description
        assert "CSS selectors" in description
        assert "XPath expressions" in description
        
        # Check for Robot Framework syntax examples
        assert "id=element_id" in description
        assert "css=" in description
        assert "xpath=" in description
    
    def test_validation_prompt_structure(self, healing_tasks, healing_agents):
        """Test that validation prompt has correct structure."""
        agent = healing_agents.locator_validation_agent()
        locator_candidates = [{"locator": "id=test", "strategy": "id"}]
        validation_context = {"target_url": "https://example.com"}
        
        task = healing_tasks.validate_locators_task(agent, locator_candidates, validation_context)
        description = task.description
        
        # Check for key sections
        assert "VALIDATION CRITERIA" in description
        assert "Element Existence" in description
        assert "Element Properties" in description
        assert "Interactability" in description
        assert "VALIDATION PROCESS" in description
        assert "OUTPUT FORMAT" in description
        
        # Check for validation criteria
        assert "Does the locator find exactly one element" in description
        assert "Is the element visible" in description
        assert "Is the element clickable" in description
    
    @pytest.mark.parametrize("response_text,expected_valid", [
        ('{"is_healable": true, "failure_type": "element_not_found"}', True),
        ('{"is_healable": false, "failure_type": "other"}', True),
        ('Invalid JSON response', False),
        ('{"missing_required_field": true}', False),
    ])
    def test_failure_analysis_response_parsing(self, response_text, expected_valid):
        """Test parsing of failure analysis responses."""
        try:
            response = json.loads(response_text)
            is_valid = (
                isinstance(response.get('is_healable'), bool) and
                'failure_type' in response
            )
        except (json.JSONDecodeError, AttributeError):
            is_valid = False
        
        assert is_valid == expected_valid
    
    @pytest.mark.parametrize("response_text,expected_valid", [
        ('{"alternatives": [{"locator": "id=test", "strategy": "id", "confidence": 0.8}]}', True),
        ('{"alternatives": []}', True),
        ('{"alternatives": [{"locator": "id=test"}]}', False),  # Missing required fields
        ('Invalid JSON', False),
    ])
    def test_locator_generation_response_parsing(self, response_text, expected_valid):
        """Test parsing of locator generation responses."""
        try:
            response = json.loads(response_text)
            is_valid = (
                'alternatives' in response and
                isinstance(response['alternatives'], list)
            )
            
            # Check if alternatives have required fields
            if is_valid and response['alternatives']:
                for alt in response['alternatives']:
                    if not all(key in alt for key in ['locator', 'strategy', 'confidence']):
                        is_valid = False
                        break
                        
        except (json.JSONDecodeError, AttributeError):
            is_valid = False
        
        assert is_valid == expected_valid
    
    @pytest.mark.parametrize("response_text,expected_valid", [
        ('{"validation_results": [], "best_candidate": null, "validation_summary": {"total_tested": 0}}', True),
        ('{"validation_results": [{"locator": "id=test", "is_valid": true}], "best_candidate": {"locator": "id=test"}}', False),  # Missing validation_summary
        ('Invalid JSON', False),
    ])
    def test_validation_response_parsing(self, response_text, expected_valid):
        """Test parsing of validation responses."""
        try:
            response = json.loads(response_text)
            is_valid = (
                'validation_results' in response and
                'best_candidate' in response and
                'validation_summary' in response
            )
        except (json.JSONDecodeError, AttributeError):
            is_valid = False
        
        assert is_valid == expected_valid


class TestAgentIntegration:
    """Integration tests for agent workflows."""
    
    @pytest.fixture
    def healing_agents(self):
        """Create HealingAgents instance for testing."""
        return HealingAgents("local", "test-model")
    
    @pytest.fixture
    def healing_tasks(self):
        """Create HealingTasks instance for testing."""
        return HealingTasks()
    
    def test_complete_healing_workflow_structure(self, healing_agents, healing_tasks):
        """Test that all components work together for complete workflow."""
        # Create agents
        failure_agent = healing_agents.failure_analysis_agent()
        generation_agent = healing_agents.locator_generation_agent()
        validation_agent = healing_agents.locator_validation_agent()
        
        # Create sample data
        failure_context = {
            'test_file': 'test.robot',
            'original_locator': 'id=button',
            'exception_type': 'NoSuchElementException'
        }
        
        failure_analysis = {
            'is_healable': True,
            'element_type': 'button',
            'failure_type': 'element_not_found'
        }
        
        locator_candidates = [
            {'locator': 'css=button.submit', 'strategy': 'css', 'confidence': 0.8}
        ]
        
        validation_context = {
            'target_url': 'https://example.com',
            'element_type': 'button'
        }
        
        # Create tasks
        analysis_task = healing_tasks.analyze_failure_task(failure_agent, failure_context)
        generation_task = healing_tasks.generate_alternative_locators_task(generation_agent, failure_analysis)
        validation_task = healing_tasks.validate_locators_task(validation_agent, locator_candidates, validation_context)
        
        # Verify all tasks are created successfully
        assert analysis_task is not None
        assert generation_task is not None
        assert validation_task is not None
        
        # Verify agents are assigned correctly
        assert analysis_task.agent == failure_agent
        assert generation_task.agent == generation_agent
        assert validation_task.agent == validation_agent
    
    def test_agent_llm_configuration(self, healing_agents):
        """Test that agents are configured with LLM settings."""
        # Test that agents have LLM configured
        agent = healing_agents.failure_analysis_agent()
        assert agent.llm is not None
        
        # Test that different agents use the same LLM instance
        agent1 = healing_agents.failure_analysis_agent()
        agent2 = healing_agents.locator_generation_agent()
        assert type(agent1.llm) == type(agent2.llm)
    
    def test_agent_configuration_consistency(self, healing_agents):
        """Test that all agents have consistent configuration."""
        agents = [
            healing_agents.failure_analysis_agent(),
            healing_agents.locator_generation_agent(),
            healing_agents.locator_validation_agent()
        ]
        
        for agent in agents:
            assert agent.verbose is True
            assert agent.allow_delegation is False
            assert agent.llm is not None
            assert len(agent.role) > 0
            assert len(agent.goal) > 0
            assert len(agent.backstory) > 0