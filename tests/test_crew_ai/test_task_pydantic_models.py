"""
Unit tests for Pydantic output models in src.backend.crew_ai.tasks.

Purpose: These models define the schema for LLM task outputs.  If required
         fields are missing or types change, the guardrail/parsing pipeline
         breaks silently.  These tests lock down the model contracts.

Tests:
  - PlannedStep: required fields, optional defaults
  - PlanOutput: steps is a list
  - IdentifiedElement: inherits PlannedStep, adds locator/found
  - AssemblyOutput: code field
"""

import pytest
from src.backend.crew_ai.tasks import (
    PlannedStep,
    PlanOutput,
    IdentifiedElement,
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

    def test_date_picker_field_accepted(self):
        """datepicker_framework must be accepted so the Pydantic model does
        not strip flatpickr metadata before it reaches the Code Assembler
        (Task D, G4 — same pipe as dropdown_framework/select_id)."""
        elem = IdentifiedElement(
            step_description="Set the CDR from date",
            keyword="Fill Text",
            locator="id=customer_cdr_from_date",
            found=True,
            element_type="date-picker",
            datepicker_framework="flatpickr",
            value="2026-07-01",
        )
        assert elem.datepicker_framework == "flatpickr"

    def test_date_picker_field_defaults_to_none(self):
        """Elements that are not date pickers must not require the field."""
        elem = IdentifiedElement(
            step_description="Click submit",
            keyword="Click",
            locator="id=submit-btn",
            found=True,
            element_type="button",
        )
        assert elem.datepicker_framework is None

    def test_empty_datepicker_framework_accepted(self):
        """browser_use_tool sends an empty string for non-widget elements;
        the model must accept it without coercing to None."""
        elem = IdentifiedElement(
            step_description="Fill username",
            keyword="Fill Text",
            locator="id=username",
            found=True,
            element_type="input",
            datepicker_framework="",
        )
        assert elem.datepicker_framework == ""

    def test_element_classes_field_accepted(self):
        """element_classes must be accepted so the Pydantic model does not
        strip the observed class list before it reaches the Code Assembler
        (Task G, G7 — the classes the locator engine saw on the field at
        locate time, i.e. AFTER the preceding steps ran; on ASTPP the
        empty-form Save has happened, so 'invalid' is in the list)."""
        elem = IdentifiedElement(
            step_description="Verify the Email field shows an error",
            keyword="Get Classes",
            locator='input[name="email"]',
            found=True,
            element_type="input",
            element_classes="text field medium form-control invalid",
        )
        assert elem.element_classes == "text field medium form-control invalid"

    def test_element_classes_defaults_to_none(self):
        """Steps that are not state verifications must not require it."""
        elem = IdentifiedElement(
            step_description="Click submit",
            keyword="Click",
            locator="id=submit-btn",
            found=True,
            element_type="button",
        )
        assert elem.element_classes is None

    def test_empty_element_classes_accepted(self):
        """Elements with no class attribute come back as empty string;
        the model must accept it without coercing to None."""
        elem = IdentifiedElement(
            step_description="Verify the Email field shows an error",
            keyword="Get Classes",
            locator='input[name="email"]',
            found=True,
            element_type="input",
            element_classes="",
        )
        assert elem.element_classes == ""

    def test_aria_invalid_field_accepted(self):
        """aria_invalid must be accepted so the Pydantic model does not
        strip the observed ARIA state before it reaches the Code Assembler
        (Task G aria pipe — for sites that mark invalid fields via ARIA
        instead of a CSS class, the assembler emits a Get Attribute
        assertion instead of the placeholder)."""
        elem = IdentifiedElement(
            step_description="Verify the Email field shows an error",
            keyword="Get Classes",
            locator="id=email",
            found=True,
            element_type="input",
            aria_invalid="true",
        )
        assert elem.aria_invalid == "true"

    def test_aria_invalid_defaults_to_none(self):
        """Steps that are not state verifications must not require it."""
        elem = IdentifiedElement(
            step_description="Click submit",
            keyword="Click",
            locator="id=submit-btn",
            found=True,
            element_type="button",
        )
        assert elem.aria_invalid is None

    def test_empty_aria_invalid_accepted(self):
        """Elements without the attribute come back as empty string;
        the model must accept it without coercing to None."""
        elem = IdentifiedElement(
            step_description="Verify the Email field shows an error",
            keyword="Get Classes",
            locator="id=email",
            found=True,
            element_type="input",
            aria_invalid="",
        )
        assert elem.aria_invalid == ""


class TestAssemblyOutput:
    """Tests for AssemblyOutput model."""

    def test_code_field(self):
        """AssemblyOutput stores the generated RF code."""
        a = AssemblyOutput(code="*** Test Cases ***\nTest\n    Log    Hello")
        assert "*** Test Cases ***" in a.code
