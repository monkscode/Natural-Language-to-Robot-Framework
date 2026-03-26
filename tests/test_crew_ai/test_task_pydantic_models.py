"""
Unit tests for Pydantic output models in src.backend.crew_ai.tasks.

Purpose: These models define the schema for LLM task outputs.  If required
         fields are missing or types change, the guardrail/parsing pipeline
         breaks silently.  These tests lock down the model contracts.

Tests:
  - PlannedStep: required fields, optional defaults
  - PlanOutput: steps is a list
  - IdentifiedElement: inherits PlannedStep, adds locator/found
  - ValidationOutput: valid, reason, optional errors
  - AssemblyOutput: code field
"""

import pytest
from src.backend.crew_ai.tasks import (
    PlannedStep,
    PlanOutput,
    IdentifiedElement,
    ValidationOutput,
    AssemblyOutput,
)


class TestPlannedStep:
    """Tests for PlannedStep model."""

    def test_required_fields(self):
        """step_description and keyword are required."""
        step = PlannedStep(step_description="Click login button", keyword="Click Element")
        assert step.step_description == "Click login button"
        assert step.keyword == "Click Element"

    def test_optional_defaults(self):
        """Optional fields default to None."""
        step = PlannedStep(step_description="test", keyword="Log")
        assert getattr(step, "locator", None) is None
        # The key point: construction doesn't crash with only required fields


class TestPlanOutput:
    """Tests for PlanOutput model."""

    def test_steps_list(self):
        """steps field holds a list of PlannedStep."""
        plan = PlanOutput(steps=[
            PlannedStep(step_description="Navigate", keyword="Go To"),
            PlannedStep(step_description="Click", keyword="Click Element"),
        ])
        assert len(plan.steps) == 2
        assert plan.steps[0].keyword == "Go To"


class TestIdentifiedElement:
    """Tests for IdentifiedElement model."""

    def test_inherits_planned_step(self):
        """IdentifiedElement has PlannedStep fields plus locator, found."""
        elem = IdentifiedElement(
            step_description="Click submit",
            keyword="Click Element",
            locator="id=submit-btn",
            found=True,
        )
        assert elem.step_description == "Click submit"
        assert elem.locator == "id=submit-btn"
        assert elem.found is True


class TestValidationOutput:
    """Tests for ValidationOutput model."""

    def test_valid_with_reason(self):
        """Validation output with valid=True and reason."""
        v = ValidationOutput(valid=True, reason="Code is syntactically correct")
        assert v.valid is True
        assert "correct" in v.reason


class TestAssemblyOutput:
    """Tests for AssemblyOutput model."""

    def test_code_field(self):
        """AssemblyOutput stores the generated RF code."""
        a = AssemblyOutput(code="*** Test Cases ***\nTest\n    Log    Hello")
        assert "*** Test Cases ***" in a.code
