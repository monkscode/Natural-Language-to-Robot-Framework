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

    def test_tom_select_fields_accepted(self):
        """dropdown_framework and select_id must be accepted so the Pydantic
        model does not strip TomSelect metadata before it reaches the Code
        Assembler."""
        elem = IdentifiedElement(
            step_description="Select Rate Group",
            keyword="Select Options By",
            locator="css=#pricelist_id-ts-control",
            found=True,
            element_type="dropdown",
            dropdown_framework="tom-select",
            select_id="pricelist_id",
            value="label    default",
        )
        assert elem.dropdown_framework == "tom-select"
        assert elem.select_id == "pricelist_id"

    def test_tom_select_fields_default_to_none(self):
        """Elements that are not TomSelect must not require these fields."""
        elem = IdentifiedElement(
            step_description="Click login",
            keyword="Click",
            locator="id=submit-btn",
            found=True,
            element_type="button",
        )
        assert elem.dropdown_framework is None
        assert elem.select_id is None

    def test_empty_dropdown_framework_accepted(self):
        """browser_use_tool sends an empty string for non-TomSelect elements;
        the model must accept it without coercing to None."""
        elem = IdentifiedElement(
            step_description="Select country",
            keyword="Select Options By",
            locator="id=country",
            found=True,
            element_type="select",
            dropdown_framework="",
            select_id=None,
        )
        assert elem.dropdown_framework == ""
        assert elem.select_id is None


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
