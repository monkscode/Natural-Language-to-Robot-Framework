#!/usr/bin/env python3
"""
Demo script showing how the healing agents work together.

This script demonstrates the healing agent system by simulating a failure
analysis, locator generation, and validation workflow.
"""

import json
from datetime import datetime
from pathlib import Path
import sys

# Add the backend to the path
sys.path.append(str(Path(__file__).parent.parent.parent / "src" / "backend"))

try:
    from crew_ai.healing_agents import HealingAgents
    from crew_ai.healing_tasks import HealingTasks
    from core.models.healing_models import FailureContext, FailureType
except ImportError:
    print("⚠️  Could not import backend services. This demo requires the full application environment.")
    print("Run this demo from the project root with the virtual environment activated.")
    sys.exit(1)


def main():
    """Demonstrate the healing agent workflow."""
    print("🔧 Healing Agents Demo")
    print("=" * 50)
    
    # Initialize agents and tasks
    print("Initializing healing agents...")
    healing_agents = HealingAgents("local", "llama2")  # Use local model for demo
    healing_tasks = HealingTasks()
    
    # Create sample failure context
    failure_context = {
        'test_file': 'test_login.robot',
        'test_case': 'Valid Login Test',
        'failing_step': 'Click Element    id=login-button',
        'original_locator': 'id=login-button',
        'target_url': 'https://example.com/login',
        'exception_type': 'NoSuchElementException',
        'exception_message': 'Unable to locate element: {"method":"id","selector":"login-button"}'
    }
    
    print(f"Sample failure context:")
    print(f"  Test File: {failure_context['test_file']}")
    print(f"  Original Locator: {failure_context['original_locator']}")
    print(f"  Exception: {failure_context['exception_type']}")
    print()
    
    # Create agents
    print("Creating specialized agents...")
    failure_agent = healing_agents.failure_analysis_agent()
    generation_agent = healing_agents.locator_generation_agent()
    validation_agent = healing_agents.locator_validation_agent()
    
    print(f"✓ Failure Analysis Agent: {failure_agent.role}")
    print(f"✓ Locator Generation Agent: {generation_agent.role}")
    print(f"✓ Locator Validation Agent: {validation_agent.role}")
    print()
    
    # Create tasks
    print("Creating healing tasks...")
    analysis_task = healing_tasks.analyze_failure_task(failure_agent, failure_context)
    
    # Sample failure analysis result (in real usage, this would come from the agent)
    sample_failure_analysis = {
        "is_healable": True,
        "failure_type": "element_not_found",
        "confidence": 0.9,
        "element_type": "button",
        "action_intent": "click",
        "locator_strategy": "id",
        "failure_reason": "Element ID 'login-button' no longer exists in DOM",
        "element_context": "Login button on authentication form",
        "healing_priority": "high",
        "recommendations": ["Try CSS selector", "Look for text-based locator", "Check for class names"]
    }
    
    generation_task = healing_tasks.generate_alternative_locators_task(
        generation_agent, sample_failure_analysis
    )
    
    # Sample locator candidates (in real usage, this would come from the agent)
    sample_locator_candidates = [
        {
            "locator": "css=button.login-btn",
            "strategy": "css",
            "confidence": 0.8,
            "reasoning": "Common class name pattern for login buttons",
            "stability_score": 0.7,
            "fallback_level": "primary"
        },
        {
            "locator": "xpath=//button[contains(text(), 'Login')]",
            "strategy": "xpath",
            "confidence": 0.7,
            "reasoning": "Text-based matching is more resilient to ID changes",
            "stability_score": 0.8,
            "fallback_level": "secondary"
        },
        {
            "locator": "css=input[type='submit'][value*='Login']",
            "strategy": "css",
            "confidence": 0.6,
            "reasoning": "Alternative if button is actually an input element",
            "stability_score": 0.6,
            "fallback_level": "tertiary"
        }
    ]
    
    validation_context = {
        "target_url": failure_context['target_url'],
        "element_type": sample_failure_analysis['element_type'],
        "action_intent": sample_failure_analysis['action_intent'],
        "original_locator": failure_context['original_locator']
    }
    
    validation_task = healing_tasks.validate_locators_task(
        validation_agent, sample_locator_candidates, validation_context
    )
    
    print("✓ Failure Analysis Task created")
    print("✓ Locator Generation Task created")
    print("✓ Locator Validation Task created")
    print()
    
    # Display task descriptions (truncated for demo)
    print("Task Descriptions:")
    print("-" * 30)
    
    print("1. Failure Analysis Task:")
    analysis_desc = analysis_task.description[:200] + "..."
    print(f"   {analysis_desc}")
    print()
    
    print("2. Locator Generation Task:")
    generation_desc = generation_task.description[:200] + "..."
    print(f"   {generation_desc}")
    print()
    
    print("3. Locator Validation Task:")
    validation_desc = validation_task.description[:200] + "..."
    print(f"   {validation_desc}")
    print()
    
    # Show sample workflow results
    print("Sample Workflow Results:")
    print("-" * 30)
    
    print("1. Failure Analysis Result:")
    print(json.dumps(sample_failure_analysis, indent=2))
    print()
    
    print("2. Generated Locator Candidates:")
    for i, candidate in enumerate(sample_locator_candidates, 1):
        print(f"   {i}. {candidate['locator']} (confidence: {candidate['confidence']})")
        print(f"      Strategy: {candidate['strategy']}")
        print(f"      Reasoning: {candidate['reasoning']}")
        print()
    
    # Sample validation results
    sample_validation_results = {
        "validation_results": [
            {
                "locator": "css=button.login-btn",
                "strategy": "css",
                "is_valid": True,
                "element_found": True,
                "is_visible": True,
                "is_interactable": True,
                "matches_expected_type": True,
                "confidence_score": 0.9,
                "stability_score": 0.7,
                "error_message": None,
                "element_properties": {
                    "tag_name": "button",
                    "text_content": "Login",
                    "attributes": {"class": "login-btn", "type": "button"}
                }
            },
            {
                "locator": "xpath=//button[contains(text(), 'Login')]",
                "strategy": "xpath",
                "is_valid": True,
                "element_found": True,
                "is_visible": True,
                "is_interactable": True,
                "matches_expected_type": True,
                "confidence_score": 0.8,
                "stability_score": 0.8,
                "error_message": None,
                "element_properties": {
                    "tag_name": "button",
                    "text_content": "Login",
                    "attributes": {"class": "login-btn", "type": "button"}
                }
            }
        ],
        "best_candidate": {
            "locator": "css=button.login-btn",
            "strategy": "css",
            "confidence_score": 0.9,
            "selection_reason": "Highest confidence score and element found successfully"
        },
        "validation_summary": {
            "total_tested": 3,
            "successful_validations": 2,
            "success_rate": 0.67,
            "validation_time": 2.5
        }
    }
    
    print("3. Validation Results:")
    print(f"   Best Candidate: {sample_validation_results['best_candidate']['locator']}")
    print(f"   Confidence: {sample_validation_results['best_candidate']['confidence_score']}")
    print(f"   Success Rate: {sample_validation_results['validation_summary']['success_rate']:.0%}")
    print()
    
    print("🎉 Healing workflow completed successfully!")
    print(f"   Original locator: {failure_context['original_locator']}")
    print(f"   Healed locator: {sample_validation_results['best_candidate']['locator']}")
    print()
    
    print("Note: This demo shows the structure and workflow of the healing agents.")
    print("In actual usage, the agents would execute against real browser sessions")
    print("and generate responses using the configured LLM.")


if __name__ == "__main__":
    main()