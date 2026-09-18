"""A placeholder failure must not become an anti-pattern (E5b).

A placeholder means discovery never found the element, so the generated code is
not a pattern to avoid — teaching the model to avoid its own placeholder is noise,
and placeholders are the single largest failure class in the corpus (69 of 251).

Referenced by: crew_ai/optimization/anti_pattern_engine.py
Depends on: unittest.mock
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

from src.backend.crew_ai.optimization.anti_pattern_engine import AntiPatternEngine
from src.backend.crew_ai.optimization.execution_memory import ExecutionRecord


def _record(**kwargs) -> ExecutionRecord:
    base = dict(
        workflow_id="wf-1",
        timestamp=datetime.now(timezone.utc),
        user_query="click the sign in button",
        test_status="failed",
        failure_category="C1",
        error_message="TimeoutError: locator.click: waiting for locator('//PLACEHOLDER_FOR_x')",
        robot_code="*** Test Cases ***\nT\n    Click    x",
        org_id="org-1",
    )
    base.update(kwargs)
    return ExecutionRecord(**base)


def test_placeholder_failure_is_not_stored_as_an_anti_pattern():
    engine = AntiPatternEngine(MagicMock())
    engine._find_similar_anti_pattern = MagicMock()

    engine.learn(_record(failure_specific_type="placeholder_never_resolved"))

    engine._find_similar_anti_pattern.assert_not_called()


def test_a_real_locator_failure_is_still_stored():
    engine = AntiPatternEngine(MagicMock())
    engine._find_similar_anti_pattern = MagicMock(return_value=None)

    engine.learn(_record(failure_specific_type="element_never_resolved"))

    engine._find_similar_anti_pattern.assert_called_once()


def test_a_record_without_the_type_is_still_stored():
    """Records built before E5b, or with no analysis, carry None — not a skip."""
    engine = AntiPatternEngine(MagicMock())
    engine._find_similar_anti_pattern = MagicMock(return_value=None)

    engine.learn(_record())

    engine._find_similar_anti_pattern.assert_called_once()
