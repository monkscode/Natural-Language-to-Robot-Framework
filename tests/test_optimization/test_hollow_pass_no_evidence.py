"""Learning must not learn from hollow passes (GitHub issue #132).

A hollow pass is a test that passes whatever the page shows (pass_quality's
`hollow_shapes`, the same verdict as the History drawer's "Suggestion"). It is
no evidence the keywords work, so:
  - FeedbackLoop.process_execution stores the record and the metrics as before
    but skips the three engines and the pattern learner;
  - workflow_service._process_learning_attribution makes no hint judgment.
A failed/error run never calls the checker. A checker error counts as hollow.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from src.backend.crew_ai.optimization.feedback_loop import (
    ContradictionDetector,
    FeedbackLoop,
    LearningMetricsTracker,
)
from src.backend.crew_ai.optimization.learning_config import LearningCircuitBreaker

QUERY = "Open https://example.com and get the page title"
HOLLOW = (
    "*** Test Cases ***\n"
    "Generated Test\n"
    "    New Page    https://example.com\n"
)
CLEAN = (
    "*** Test Cases ***\n"
    "Generated Test\n"
    "    New Page    https://example.com\n"
    "    ${t}=    Get Title\n"
    "    Should Be Equal    ${t}    Example Domain\n"
)
_CHECKER = "src.backend.crew_ai.optimization.feedback_loop.hollow_shapes"
_FIRE = "src.backend.crew_ai.optimization.conflict_detection.fire_usage_attribution"


class _SyncQueue:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def _loop():
    breaker = MagicMock(wraps=LearningCircuitBreaker())
    fl = FeedbackLoop(
        execution_memory=MagicMock(),
        failure_analyzer=MagicMock(),
        structural_engine=MagicMock(),
        keyword_engine=MagicMock(),
        anti_pattern_engine=MagicMock(),
        pattern_learner=MagicMock(),
        metrics_tracker=MagicMock(spec=LearningMetricsTracker),
        contradiction_detector=MagicMock(spec=ContradictionDetector),
        write_queue=_SyncQueue(),
        circuit_breaker=breaker,
    )
    # The constructor submits two start-up jobs to the synchronous queue.
    fl.execution_memory.reset_mock()
    return fl


def _run(fl, code, status="passed"):
    fl.process_execution(
        workflow_id="wf-132", user_query=QUERY, url="https://example.com",
        robot_code=code, test_status=status,
    )


def _learners(fl):
    return [fl.structural_engine.learn, fl.keyword_engine.learn,
            fl.anti_pattern_engine.learn, fl.pattern_learner.learn_from_execution]


class TestProcessExecution:
    def test_hollow_pass_is_stored_and_counted_but_not_learned(self, caplog):
        fl = _loop()
        with caplog.at_level(logging.INFO):
            _run(fl, HOLLOW)
        fl.execution_memory.store.assert_called_once()
        for learner in _learners(fl):
            learner.assert_not_called()
        fl.metrics_tracker.record_execution.assert_called_once()
        assert fl.metrics_tracker.record_execution.call_args.kwargs["test_passed"] is True
        fl.execution_memory.update_daily_stats.assert_called_once_with("passed")
        fl.circuit_breaker.record_error.assert_not_called()
        fl.circuit_breaker.record_success.assert_called_once()
        lines = [r.getMessage() for r in caplog.records if "hollow pass" in r.getMessage()]
        assert len(lines) == 1
        assert "wf-132" in lines[0]
        assert "not learned" in lines[0]
        assert "[LEARNING]" in lines[0]

    def test_clean_pass_is_learned(self):
        fl = _loop()
        _run(fl, CLEAN)
        fl.execution_memory.store.assert_called_once()
        for learner in _learners(fl):
            learner.assert_called_once()

    def test_checker_error_counts_as_hollow(self, caplog):
        fl = _loop()
        with patch(_CHECKER, side_effect=RuntimeError("boom")), \
                caplog.at_level(logging.WARNING):
            _run(fl, CLEAN)
        fl.execution_memory.store.assert_called_once()
        for learner in _learners(fl):
            learner.assert_not_called()
        fl.circuit_breaker.record_error.assert_not_called()
        # The exception type is logged; its message (it could quote the code) is not.
        assert any(r.levelno == logging.WARNING and "[LEARNING]" in r.getMessage()
                   and "RuntimeError" in r.getMessage() for r in caplog.records)
        assert not any("boom" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("status", ["failed", "error"])
    def test_failed_run_never_calls_the_checker(self, status):
        fl = _loop()
        with patch(_CHECKER) as checker:
            _run(fl, HOLLOW, status=status)
        checker.assert_not_called()
        for learner in _learners(fl)[:3]:
            learner.assert_called_once()
        fl.pattern_learner.learn_from_execution.assert_not_called()  # passed-only, as before


class TestAttribution:
    @staticmethod
    def _attribute(code, status="passed", ids="[5, 12]", pre=None):
        from src.backend.services.workflow_service import _process_learning_attribution
        _process_learning_attribution(
            "attr-132", QUERY, code, {"test_status": status}, pre, ids)

    @patch(_FIRE)
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_hollow_pass_makes_no_judgment(self, mock_get_fl, mock_fire, caplog):
        mock_get_fl.return_value = MagicMock()
        with caplog.at_level(logging.INFO):
            self._attribute(HOLLOW)
        mock_fire.assert_not_called()
        assert any("hollow pass" in r.getMessage() and "[LEARNING]" in r.getMessage()
                   for r in caplog.records)

    @patch(_FIRE)
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_clean_pass_fires_once(self, mock_get_fl, mock_fire):
        mock_get_fl.return_value = MagicMock()
        self._attribute(CLEAN)
        mock_fire.assert_called_once()

    @patch(_FIRE)
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_checker_error_makes_no_judgment(self, mock_get_fl, mock_fire):
        mock_get_fl.return_value = MagicMock()
        with patch(_CHECKER, side_effect=RuntimeError("boom")):
            self._attribute(CLEAN)
        mock_fire.assert_not_called()

    @patch(_FIRE)
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_failed_run_never_calls_the_checker(self, mock_get_fl, mock_fire):
        mock_get_fl.return_value = MagicMock()
        with patch(_CHECKER) as checker:
            self._attribute(HOLLOW, status="failed")
        checker.assert_not_called()
        mock_fire.assert_not_called()

    @patch(_FIRE)
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_run_without_hints_never_calls_the_checker(self, mock_get_fl, mock_fire):
        mock_get_fl.return_value = MagicMock()
        with patch(_CHECKER) as checker:
            self._attribute(HOLLOW, ids="[]")
        checker.assert_not_called()
        mock_fire.assert_not_called()

    @patch(_FIRE)
    @patch("src.backend.services.workflow_service.get_feedback_loop")
    def test_failed_then_hollow_then_clean_run_id_is_judged_standalone_not_case_b(
            self, mock_get_fl, mock_fire):
        """Same run id, failed -> hollow -> clean. The hollow re-run judges nothing but
        flips the stored record to `passed` (postgres_execution_memory.py:249-251), so the
        later clean pass still gets its judgment, standalone: the failed-then-fixed
        (Case B) judgment of that run id is lost. Accepted (controller's ruling)."""
        mock_get_fl.return_value = MagicMock()
        pre = MagicMock()
        pre.test_status = "failed"
        pre.robot_code = "old"
        pre.hint_attribution_done = 0
        self._attribute(HOLLOW, pre=pre)
        mock_fire.assert_not_called()
        # What production hands over after the hollow re-run: the record now reads passed.
        pre.test_status = "passed"
        self._attribute(CLEAN, pre=pre)
        mock_fire.assert_called_once()
        assert mock_fire.call_args.kwargs["case_b"] is False
