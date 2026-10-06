"""
Unit tests for src.backend.services.dryrun_service — the deterministic
robot --dryrun gate that replaced the CrewAI LLM validator agent.

Per project rules: run from repo root; unittest.mock.patch (not monkeypatch);
never write to robot_tests/ — use tmp_path. Docker + the Crew are mocked.

Covered (§5):
  - _parse_dryrun_output_xml — the 3-signal failure rule (exit / stat-fail / <errors>)
  - run_dryrun_in_container — writes only to {run_id}/dryrun/, names the container
    robot-test-dryrun-{run_id}, force-removes a stale container, infra failure on
    missing output.xml
  - validate_and_repair — graceful degrade (Docker down → unverified, no raise),
    repair loop bounded by MAX_DRYRUN_FIXES, empty/whitespace skip (no container),
    no-progress short-circuit, soft delivery on failure, repair-usage accumulation,
    and that learning is NEVER touched
  - extract_and_normalize_robot_code — parity with the strategies/normalization it
    was refactored out of
"""

import logging
import os
import xml.etree.ElementTree as ET
from unittest.mock import patch, MagicMock

import pytest

import src.backend.services.dryrun_service as ds
from src.backend.services import dryrun_service


# ---------------------------------------------------------------------------
# output.xml fixtures (mirror the S1 spike structures)
# ---------------------------------------------------------------------------

_CLEAN_XML = (
    '<robot><suite name="S"><test name="T"><status status="PASS"/></test>'
    '<status status="PASS"/></suite>'
    '<statistics><total><stat pass="1" fail="0">All Tests</stat></total></statistics>'
    '<errors/></robot>'
)

_UNKNOWN_KW_XML = (
    '<robot><suite name="S"><test name="T">'
    '<kw name="Cilck"><msg level="FAIL">No keyword with name \'Cilck\' found. '
    'Did you mean: Browser.Click</msg><status status="FAIL"/></kw>'
    '<status status="FAIL"/></test><status status="FAIL"/></suite>'
    '<statistics><total><stat pass="0" fail="1">All Tests</stat></total></statistics>'
    '<errors/></robot>'
)

_BAD_IMPORT_XML = (  # exit 0, fail 0 — error ONLY in <errors>
    '<robot><suite name="S"><test name="T"><status status="PASS"/></test>'
    '<status status="PASS"/></suite>'
    '<statistics><total><stat pass="1" fail="0">All Tests</stat></total></statistics>'
    '<errors><msg level="ERROR">Importing library \'Bowser\' failed: ModuleNotFoundError</msg>'
    '</errors></robot>'
)


def _write_xml(tmp_path, content) -> str:
    p = tmp_path / "output.xml"
    p.write_text(content, encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# _parse_dryrun_output_xml — the 3-signal failure rule
# ---------------------------------------------------------------------------

class TestParseDryrunOutputXml:
    def test_clean_passes(self, tmp_path):
        passed, errors = ds._parse_dryrun_output_xml(_write_xml(tmp_path, _CLEAN_XML), 0)
        assert passed is True
        assert errors == ""

    def test_unknown_keyword_fails_with_suggestion(self, tmp_path):
        passed, errors = ds._parse_dryrun_output_xml(_write_xml(tmp_path, _UNKNOWN_KW_XML), 1)
        assert passed is False
        assert "Did you mean: Browser.Click" in errors

    def test_bad_import_fails_even_with_exit_zero(self, tmp_path):
        # The critical case: exit 0 AND fail 0, error ONLY in <errors>.
        passed, errors = ds._parse_dryrun_output_xml(_write_xml(tmp_path, _BAD_IMPORT_XML), 0)
        assert passed is False
        assert "Importing library 'Bowser' failed" in errors

    def test_nonzero_exit_fails_even_when_xml_looks_clean(self, tmp_path):
        passed, _ = ds._parse_dryrun_output_xml(_write_xml(tmp_path, _CLEAN_XML), 1)
        assert passed is False

    def test_unreadable_xml_raises(self, tmp_path):
        p = tmp_path / "output.xml"
        p.write_text("<robot><not-closed", encoding="utf-8")
        with pytest.raises(RuntimeError):
            ds._parse_dryrun_output_xml(str(p), 0)


# ---------------------------------------------------------------------------
# run_dryrun_in_container — file isolation, container name, cleanup, infra failure
# ---------------------------------------------------------------------------

class TestRunDryrunInContainer:
    def _client(self):
        client = MagicMock()
        # stale-container lookup: simulate "not found"
        import docker
        client.containers.get.side_effect = docker.errors.NotFound("nope")
        container = MagicMock()
        container.id = "cid"
        container.wait.return_value = {"StatusCode": 0}
        client.containers.run.return_value = container
        client.api.logs.return_value = b"dryrun stdout"
        return client, container

    def test_writes_only_to_dryrun_subdir_and_names_container(self, tmp_path):
        client, container = self._client()
        run_id = "11111111-1111-1111-1111-111111111111"

        from src.backend.core.artifact_store import LocalArtifactStore
        with patch.object(ds, "get_artifact_store", return_value=LocalArtifactStore(tmp_path)), \
             patch.object(ds, "resolve_host_robot_tests_dir", return_value=str(tmp_path)), \
             patch.object(ds, "normalize_docker_mount_source", side_effect=lambda p: p):
            # The container "produces" output.xml under {run_id}/dryrun/
            def _fake_run(**cfg):
                xml_dir = os.path.join(str(tmp_path), run_id, "dryrun")
                os.makedirs(xml_dir, exist_ok=True)
                with open(os.path.join(xml_dir, "output.xml"), "w", encoding="utf-8") as f:
                    f.write(_CLEAN_XML)
                return container
            client.containers.run.side_effect = _fake_run

            result = ds.run_dryrun_in_container(client, run_id, "*** Settings ***\nLibrary    Browser\n")

        assert result["passed"] is True
        # test file + output.xml live under {run_id}/dryrun/ ONLY — never {run_id}/output.xml
        assert os.path.exists(os.path.join(str(tmp_path), run_id, "dryrun", "dryrun.robot"))
        assert not os.path.exists(os.path.join(str(tmp_path), run_id, "output.xml"))
        # container named robot-test-dryrun-{run_id}
        cfg = client.containers.run.call_args.kwargs
        assert cfg["name"] == f"robot-test-dryrun-{run_id}"
        # container removed (cleanup)
        container.remove.assert_called()

    def test_stale_container_force_removed(self, tmp_path):
        client, container = self._client()
        stale = MagicMock()
        client.containers.get.side_effect = None
        client.containers.get.return_value = stale
        run_id = "22222222-2222-2222-2222-222222222222"

        from src.backend.core.artifact_store import LocalArtifactStore
        with patch.object(ds, "get_artifact_store", return_value=LocalArtifactStore(tmp_path)), \
             patch.object(ds, "resolve_host_robot_tests_dir", return_value=str(tmp_path)), \
             patch.object(ds, "normalize_docker_mount_source", side_effect=lambda p: p):
            def _fake_run(**cfg):
                xml_dir = os.path.join(str(tmp_path), run_id, "dryrun")
                os.makedirs(xml_dir, exist_ok=True)
                open(os.path.join(xml_dir, "output.xml"), "w", encoding="utf-8").write(_CLEAN_XML)
                return container
            client.containers.run.side_effect = _fake_run

            ds.run_dryrun_in_container(client, run_id, "code")

        stale.remove.assert_called_once_with(force=True)

    def test_missing_output_xml_raises_infra_failure(self, tmp_path):
        client, container = self._client()
        run_id = "33333333-3333-3333-3333-333333333333"
        # container.run does NOT create output.xml → infra failure
        from src.backend.core.artifact_store import LocalArtifactStore
        with patch.object(ds, "get_artifact_store", return_value=LocalArtifactStore(tmp_path)), \
             patch.object(ds, "resolve_host_robot_tests_dir", return_value=str(tmp_path)), \
             patch.object(ds, "normalize_docker_mount_source", side_effect=lambda p: p):
            with pytest.raises(RuntimeError, match="no output.xml"):
                ds.run_dryrun_in_container(client, run_id, "code")
        # container still cleaned up despite the raise
        container.remove.assert_called()


# ---------------------------------------------------------------------------
# validate_and_repair — orchestration, soft gate, graceful degrade, learning
# ---------------------------------------------------------------------------

class TestValidateAndRepair:
    def _settings(self, enabled=True, max_fixes=2):
        s = MagicMock()
        s.DRYRUN_ENABLED = enabled
        s.MAX_DRYRUN_FIXES = max_fixes
        s.DRYRUN_TIMEOUT = 120
        return s

    def test_disabled_skips_without_container(self):
        with patch.object(ds, "settings", self._settings(enabled=False)), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc:
            out = ds.validate_and_repair("rid", "*** Settings ***\n", "gemini", "m", None)
        assert out["dryrun_status"] == "skipped"
        assert out["code"] == "*** Settings ***\n"
        assert out["repair_usage"] == {}
        mock_rc.ensure_image.assert_not_called()  # §8.4 — no executor hop for a skip

    def test_assembled_checkpoint_pushed_at_gate_entry(self):
        """The 80% '✅ Test code assembled' checkpoint is pushed when the gate
        starts. The event-bus TaskCompletedEvent for the assembler is lost to a
        handler race (CrewAI bus runs sync handlers in a ThreadPoolExecutor),
        so the gate — which by definition runs after the crew returned — is the
        deterministic place to emit it."""
        from queue import Queue

        q = Queue()
        with patch.object(ds, "settings", self._settings()), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc:
            mock_rc.ensure_image.return_value = {"status": "ready"}
            mock_rc.dryrun.return_value = {"passed": True, "errors": "", "exit_code": 0}
            ds.validate_and_repair("rid", "code", "gemini", "m", q)

        first = q.get(timeout=1)
        assert first["progress"] == 80
        assert "Test code assembled" in first["message"]
        second = q.get(timeout=1)
        assert "Preparing verification environment" in second["message"]

    def test_assembled_checkpoint_pushed_even_when_gate_skipped(self):
        """DRYRUN_ENABLED=false still means the assembler finished — the 80%
        checkpoint must not depend on the gate actually running."""
        from queue import Queue, Empty

        q = Queue()
        with patch.object(ds, "settings", self._settings(enabled=False)), \
             patch("src.backend.services.dryrun_service.runner_exec_client"):
            out = ds.validate_and_repair("rid", "*** Settings ***\n", "gemini", "m", q)

        assert out["dryrun_status"] == "skipped"
        first = q.get(timeout=1)
        assert first["progress"] == 80
        assert "Test code assembled" in first["message"]
        with pytest.raises(Empty):
            q.get(timeout=0.1)

    @pytest.mark.parametrize("code", ["", "   \n\t  "])
    def test_empty_code_skips_without_container(self, code):
        with patch.object(ds, "settings", self._settings()), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc:
            out = ds.validate_and_repair("rid", code, "gemini", "m", None)
        assert out["dryrun_status"] == "skipped"
        mock_rc.ensure_image.assert_not_called()

    def test_docker_unavailable_degrades_to_unverified(self):
        # learn-6: ensure_image raises RunnerExecUnavailable → unverified, no raise.
        with patch.object(ds, "settings", self._settings()), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc:
            mock_rc.ensure_image.side_effect = ds.RunnerExecUnavailable("no executor")
            out = ds.validate_and_repair("rid", "*** Settings ***\nLibrary  Browser\n", "gemini", "m", None)
        assert out["dryrun_status"] == "unverified"
        assert out["code"] == "*** Settings ***\nLibrary  Browser\n"  # unchanged
        assert out["repair_usage"] == {}

    def test_passed_first_try_no_repair(self):
        with patch.object(ds, "settings", self._settings()), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc, \
             patch.object(ds, "repair_robot_code") as mock_repair:
            mock_rc.ensure_image.return_value = {"status": "ready"}
            mock_rc.dryrun.return_value = {"passed": True, "errors": "", "exit_code": 0}
            out = ds.validate_and_repair("rid", "code", "gemini", "m", None)
        assert out["dryrun_status"] == "passed"
        assert mock_rc.dryrun.call_count == 1
        mock_repair.assert_not_called()

    def test_repair_loop_bounded_by_max_fixes(self):
        # dryrun fails N+1 times → exactly N repair kickoffs → failed.
        with patch.object(ds, "settings", self._settings(max_fixes=2)), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc, \
             patch.object(ds, "repair_robot_code",
                          side_effect=lambda rid, code, errs, *a, **k: (MagicMock(), {"llm_calls": 1, "cost": 0.001}, {})) as mock_repair, \
             patch.object(ds, "extract_and_normalize_robot_code",
                          side_effect=lambda out, _c=[0]: f"code-v{(_c.__setitem__(0, _c[0]+1) or _c[0])}"):
            mock_rc.ensure_image.return_value = {"status": "ready"}
            mock_rc.dryrun.return_value = {"passed": False, "errors": "bad kw", "exit_code": 1}
            out = ds.validate_and_repair("rid", "code-v0", "gemini", "m", None)
        assert out["dryrun_status"] == "failed"
        assert mock_rc.dryrun.call_count == 3        # MAX_DRYRUN_FIXES + 1
        assert mock_repair.call_count == 2     # exactly MAX_DRYRUN_FIXES
        assert out["dryrun_errors"] == "bad kw"
        # repair usage accumulated across both attempts
        assert out["repair_usage"]["llm_calls"] == 2
        assert abs(out["repair_usage"]["cost"] - 0.002) < 1e-9

    def test_repair_then_pass(self):
        # fail once, repair, then pass on the 2nd dryrun.
        results = [
            {"passed": False, "errors": "bad kw", "exit_code": 1},
            {"passed": True, "errors": "", "exit_code": 0},
        ]
        with patch.object(ds, "settings", self._settings(max_fixes=2)), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc, \
             patch.object(ds, "repair_robot_code",
                          return_value=(MagicMock(), {"llm_calls": 1, "cost": 0.001}, {})), \
             patch.object(ds, "extract_and_normalize_robot_code", return_value="fixed code"):
            mock_rc.ensure_image.return_value = {"status": "ready"}
            mock_rc.dryrun.side_effect = results
            out = ds.validate_and_repair("rid", "broken code", "gemini", "m", None)
        assert out["dryrun_status"] == "passed"
        assert out["code"] == "fixed code"
        assert mock_rc.dryrun.call_count == 2

    def test_no_progress_short_circuit(self):
        # §8.6: repair returns byte-identical code → loop breaks early.
        with patch.object(ds, "settings", self._settings(max_fixes=3)), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc, \
             patch.object(ds, "repair_robot_code",
                          return_value=(MagicMock(), {"llm_calls": 1, "cost": 0.001}, {})) as mock_repair, \
             patch.object(ds, "extract_and_normalize_robot_code", return_value="same code"):
            mock_rc.ensure_image.return_value = {"status": "ready"}
            mock_rc.dryrun.return_value = {"passed": False, "errors": "bad kw", "exit_code": 1}
            out = ds.validate_and_repair("rid", "same code", "gemini", "m", None)
        assert out["dryrun_status"] == "failed"
        # 1 dryrun, then repair produced identical code → break before re-dryrunning.
        assert mock_rc.dryrun.call_count == 1
        assert mock_repair.call_count == 1

    def test_repair_exception_is_isolated(self):
        # §8.3: a repair crash degrades to failed with the current code, never raises.
        with patch.object(ds, "settings", self._settings(max_fixes=2)), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc, \
             patch.object(ds, "repair_robot_code", side_effect=RuntimeError("LLM down")):
            mock_rc.ensure_image.return_value = {"status": "ready"}
            mock_rc.dryrun.return_value = {"passed": False, "errors": "bad kw", "exit_code": 1}
            out = ds.validate_and_repair("rid", "code", "gemini", "m", None)
        assert out["dryrun_status"] == "failed"
        assert out["code"] == "code"  # unchanged — repair never produced new code

    def test_dryrun_infra_error_midloop_degrades_to_unverified(self):
        with patch.object(ds, "settings", self._settings()), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc:
            mock_rc.ensure_image.return_value = {"status": "ready"}
            mock_rc.dryrun.side_effect = RuntimeError("no output.xml")
            out = ds.validate_and_repair("rid", "code", "gemini", "m", None)
        assert out["dryrun_status"] == "unverified"

    def test_learning_system_never_touched(self):
        # learn-5: the gate must never call into the learning system.
        with patch.object(ds, "settings", self._settings()), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc, \
             patch("src.backend.crew_ai.optimization.learning_registry.get_feedback_loop") as mock_fl:
            mock_rc.ensure_image.return_value = {"status": "ready"}
            mock_rc.dryrun.return_value = {"passed": True, "errors": "", "exit_code": 0}
            ds.validate_and_repair("rid", "code", "gemini", "m", None)
        mock_fl.assert_not_called()

    def test_progress_pushes_are_guarded_for_none_queue(self):
        # progress_queue=None must not raise (guarded direct put).
        with patch.object(ds, "settings", self._settings(max_fixes=1)), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc:
            mock_rc.ensure_image.return_value = {"status": "ready"}
            mock_rc.dryrun.return_value = {"passed": True, "errors": "", "exit_code": 0}
            out = ds.validate_and_repair("rid", "code", "gemini", "m", None)
        assert out["dryrun_status"] == "passed"

    def test_progress_pushed_onto_queue_when_provided(self):
        from queue import Queue
        q = Queue()
        with patch.object(ds, "settings", self._settings(max_fixes=1)), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc:
            mock_rc.ensure_image.return_value = {"status": "ready"}
            mock_rc.dryrun.return_value = {"passed": True, "errors": "", "exit_code": 0}
            ds.validate_and_repair("rid", "code", "gemini", "m", q)
        msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())
        # at least the "Verifying generated test..." push (progress 88)
        assert any(m.get("progress") == 88 for m in msgs)


class TestGateCounters:
    """The gate returns its own counters instead of leaving them to be scraped.

    Before this, the only record of a repair round was the SSE progress string
    "🔧 Fixing test code..." — bench/bench_lib.py counts occurrences of it to
    compute dryrun_repairs, which makes a quality gate depend on a UI message.
    """

    def _settings(self, enabled=True, max_fixes=2):
        s = MagicMock()
        s.DRYRUN_ENABLED = enabled
        s.MAX_DRYRUN_FIXES = max_fixes
        s.DRYRUN_TIMEOUT = 120
        return s

    def _repair(self, guardrails=None):
        """A repair_robot_code stand-in returning the full 3-tuple."""
        return lambda rid, code, errs, *a, **k: (
            MagicMock(), {"llm_calls": 1, "cost": 0.001},
            guardrails if guardrails is not None else {"repair_output": 1},
        )

    def test_skipped_reports_zero_attempts(self):
        with patch.object(ds, "settings", self._settings(enabled=False)), \
             patch("src.backend.services.dryrun_service.runner_exec_client"):
            out = ds.validate_and_repair("rid", "code", "gemini", "m", None)
        assert out["dryrun_status"] == "skipped"
        assert out["dryrun_attempts"] == 0
        assert out["dryrun_repairs"] == 0
        assert out["guardrail_attempts"] == {}

    def test_unverified_before_any_dryrun_reports_zero_attempts(self):
        with patch.object(ds, "settings", self._settings()), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc:
            mock_rc.ensure_image.side_effect = ds.RunnerExecUnavailable("no executor")
            out = ds.validate_and_repair("rid", "code", "gemini", "m", None)
        assert out["dryrun_status"] == "unverified"
        assert out["dryrun_attempts"] == 0
        assert out["dryrun_repairs"] == 0

    def test_passed_first_try_counts_one_attempt_no_repair(self):
        with patch.object(ds, "settings", self._settings()), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc:
            mock_rc.ensure_image.return_value = {"status": "ready"}
            mock_rc.dryrun.return_value = {"passed": True, "errors": "", "exit_code": 0}
            out = ds.validate_and_repair("rid", "code", "gemini", "m", None)
        assert out["dryrun_status"] == "passed"
        assert out["dryrun_attempts"] == 1
        assert out["dryrun_repairs"] == 0

    def test_failed_counts_every_attempt_and_repair(self):
        with patch.object(ds, "settings", self._settings(max_fixes=2)), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc, \
             patch.object(ds, "repair_robot_code", side_effect=self._repair()), \
             patch.object(ds, "extract_and_normalize_robot_code",
                          side_effect=lambda out, _c=[0]: f"code-v{(_c.__setitem__(0, _c[0]+1) or _c[0])}"):
            mock_rc.ensure_image.return_value = {"status": "ready"}
            mock_rc.dryrun.return_value = {"passed": False, "errors": "bad kw", "exit_code": 1}
            out = ds.validate_and_repair("rid", "code-v0", "gemini", "m", None)
        assert out["dryrun_status"] == "failed"
        assert out["dryrun_attempts"] == 3   # MAX_DRYRUN_FIXES + 1
        assert out["dryrun_repairs"] == 2

    def test_attempt_that_raises_is_still_counted(self):
        """An attempt that times out was still attempted. Counting after the
        call would report 0 for a run that spawned a container and waited."""
        with patch.object(ds, "settings", self._settings()), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc:
            mock_rc.ensure_image.return_value = {"status": "ready"}
            mock_rc.dryrun.side_effect = RuntimeError("timed out")
            out = ds.validate_and_repair("rid", "code", "gemini", "m", None)
        assert out["dryrun_status"] == "unverified"
        assert out["dryrun_attempts"] == 1

    def test_repair_that_raises_is_still_counted(self):
        with patch.object(ds, "settings", self._settings(max_fixes=2)), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc, \
             patch.object(ds, "repair_robot_code", side_effect=RuntimeError("LLM down")):
            mock_rc.ensure_image.return_value = {"status": "ready"}
            mock_rc.dryrun.return_value = {"passed": False, "errors": "bad kw", "exit_code": 1}
            out = ds.validate_and_repair("rid", "code", "gemini", "m", None)
        assert out["dryrun_status"] == "failed"
        assert out["dryrun_repairs"] == 1

    def test_guardrail_attempts_come_back_from_the_repair_crew(self):
        """The repair mini-crew builds its own RobotTasks, so its guardrail
        counter is unreachable except through this return value."""
        with patch.object(ds, "settings", self._settings(max_fixes=2)), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc, \
             patch.object(ds, "repair_robot_code",
                          side_effect=self._repair(guardrails={"repair_output": 2})), \
             patch.object(ds, "extract_and_normalize_robot_code",
                          side_effect=lambda out, _c=[0]: f"code-v{(_c.__setitem__(0, _c[0]+1) or _c[0])}"):
            mock_rc.ensure_image.return_value = {"status": "ready"}
            mock_rc.dryrun.return_value = {"passed": False, "errors": "bad kw", "exit_code": 1}
            out = ds.validate_and_repair("rid", "code-v0", "gemini", "m", None)
        # Two repair rounds, each reporting 2 → summed across rounds.
        assert out["guardrail_attempts"] == {"repair_output": 4}

    def test_repair_duration_present_only_when_a_repair_ran(self):
        """Angle D: workflow_service composes crew_stage_metrics['repair'] from
        repair_usage plus this duration."""
        with patch.object(ds, "settings", self._settings()), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc:
            mock_rc.ensure_image.return_value = {"status": "ready"}
            mock_rc.dryrun.return_value = {"passed": True, "errors": "", "exit_code": 0}
            passed = ds.validate_and_repair("rid", "code", "gemini", "m", None)
        assert passed["repair_duration_s"] == 0.0

        with patch.object(ds, "settings", self._settings(max_fixes=1)), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc, \
             patch.object(ds, "repair_robot_code", side_effect=self._repair()), \
             patch.object(ds, "extract_and_normalize_robot_code", return_value="fixed"):
            mock_rc.ensure_image.return_value = {"status": "ready"}
            mock_rc.dryrun.return_value = {"passed": False, "errors": "bad", "exit_code": 1}
            repaired = ds.validate_and_repair("rid", "code", "gemini", "m", None)
        assert repaired["repair_duration_s"] >= 0.0
        assert repaired["repair_usage"]["llm_calls"] == 1


# ---------------------------------------------------------------------------
# extract_and_normalize_robot_code — parity with the inlined pipeline
# ---------------------------------------------------------------------------

class TestExtractAndNormalize:
    def _task_output(self, *, pydantic=None, json_dict=None, raw=None):
        out = MagicMock()
        out.pydantic = MagicMock(code=pydantic) if pydantic is not None else None
        out.json_dict = json_dict
        out.raw = raw
        return out

    def test_pydantic_strategy(self):
        code = "*** Settings ***\nLibrary    Browser\n*** Test Cases ***\nT\n    Log    hi"
        assert "*** Settings ***" in ds.extract_and_normalize_robot_code(self._task_output(pydantic=code))

    def test_json_dict_strategy(self):
        code = "*** Settings ***\nLibrary    Browser\n*** Test Cases ***\nT\n    Log    hi"
        out = ds.extract_and_normalize_robot_code(self._task_output(json_dict={"code": code}))
        assert out.startswith("*** Settings ***")

    def test_raw_json_strategy(self):
        import json
        code = "*** Settings ***\nLibrary    Browser\n*** Test Cases ***\nT\n    Log    hi"
        out = ds.extract_and_normalize_robot_code(self._task_output(raw=json.dumps({"code": code})))
        assert out.startswith("*** Settings ***")

    def test_raw_legacy_strategy(self):
        code = "*** Settings ***\nLibrary    Browser\n*** Test Cases ***\nT\n    Log    hi"
        assert ds.extract_and_normalize_robot_code(self._task_output(raw=code)).startswith("*** Settings ***")

    def test_escaped_newlines_normalised(self):
        raw = "*** Settings ***\\nLibrary    Browser\\n\\n*** Test Cases ***\\nT\\n    Log  hi"
        out = ds.extract_and_normalize_robot_code(self._task_output(raw=raw))
        assert "\n" in out and "\\n" not in out

    def test_redundant_css_prefix_is_stripped_by_the_pipeline(self):
        """The strip is only worth anything if it is actually wired in here.

        `css=id=searchBox` is not valid CSS — Playwright rejects it with
        `Unexpected token "=" while parsing css selector` — and the dryrun gate
        cannot catch it, because dryrun checks keyword names and arity without
        resolving selectors. Without this test, deleting the
        strip_redundant_css_prefix call from the pipeline leaves the whole
        suite green.
        """
        raw = ("*** Settings ***\nLibrary    Browser\n\n*** Test Cases ***\nT\n"
               "    Click    css=id=searchBox\n"
               "    Fill Text    css=xpath=//input[@name='q']    shoes")
        out = ds.extract_and_normalize_robot_code(self._task_output(raw=raw))
        assert "css=id=searchBox" not in out
        assert "id=searchBox" in out
        assert "css=xpath=" not in out
        assert "xpath=//input[@name='q']" in out

    def test_genuine_css_selectors_survive_the_pipeline(self):
        """The strip must not touch real CSS: an attribute selector contains
        `=` but does not start with a strategy name followed by `=`."""
        raw = ("*** Settings ***\nLibrary    Browser\n\n*** Test Cases ***\nT\n"
               "    Click    css=#searchBox\n"
               "    Click    css=input[id=foo]\n"
               "    Click    css=[data-x=y]")
        out = ds.extract_and_normalize_robot_code(self._task_output(raw=raw))
        assert "css=#searchBox" in out
        assert "css=input[id=foo]" in out
        assert "css=[data-x=y]" in out

    def test_multiple_settings_blocks_uses_last(self):
        raw = ("*** Settings ***\nLibrary    OldLib\n\n"
               "*** Settings ***\nLibrary    Browser\n\n*** Test Cases ***\nT\n    Log    hi\n")
        out = ds.extract_and_normalize_robot_code(self._task_output(raw=raw))
        assert out.count("*** Settings ***") == 1
        assert "OldLib" not in out

    def test_prefix_stripped(self):
        raw = "Here is the code:\n\n*** Settings ***\nLibrary    Browser\n*** Test Cases ***\nT\n    Log    hi"
        out = ds.extract_and_normalize_robot_code(self._task_output(raw=raw))
        assert out.startswith("***")

    def test_trailing_json_artifact_stripped(self):
        raw = "*** Settings ***\nLibrary    Browser\n*** Test Cases ***\nT\n    Log    hi" + '"}'
        out = ds.extract_and_normalize_robot_code(self._task_output(raw=raw))
        assert not out.endswith('"}')

    def test_browser_timeout_injected(self):
        raw = "*** Settings ***\nLibrary    Browser\n*** Test Cases ***\nT\n    Log    hi"
        out = ds.extract_and_normalize_robot_code(self._task_output(raw=raw))
        assert "Library    Browser    timeout=30s" in out

    def test_browser_timeout_survives_a_repair_round_trip(self):
        """This function runs on BOTH the generation path and the dryrun repair
        path, so a repair pass must not strip or double the injected timeout."""
        raw = "*** Settings ***\nLibrary    Browser\n*** Test Cases ***\nT\n    Log    hi"
        first = ds.extract_and_normalize_robot_code(self._task_output(raw=raw))
        second = ds.extract_and_normalize_robot_code(self._task_output(raw=first))
        assert second == first
        assert second.count("timeout=") == 1

    def test_visibility_check_rewritten_by_the_pipeline(self):
        """The rewrite is only worth anything if it is actually wired in here.

        Without this test, deleting the rewrite call from the pipeline leaves
        the whole suite green. The bare `#heading` also proves the `css=`
        prefixing still applies to the rewritten line.
        """
        raw = ("*** Settings ***\nLibrary    Browser\n\n*** Test Cases ***\nT\n"
               "    Click    #login\n"
               "    Get Element States    #heading    contains    visible")
        out = ds.extract_and_normalize_robot_code(self._task_output(raw=raw))
        assert "    Wait For Elements State    css=#heading    visible" in out
        assert "Get Element States" not in out

    def test_visibility_check_rewrite_survives_a_repair_round_trip(self):
        """This function runs on BOTH the generation path and the dryrun repair
        path; a repair pass re-running cleanup on its own output must leave
        the rewritten line exactly as it was."""
        raw = ("*** Settings ***\nLibrary    Browser\n\n*** Test Cases ***\nT\n"
               "    Get Element States    ${h}    not contains    visible")
        first = ds.extract_and_normalize_robot_code(self._task_output(raw=raw))
        second = ds.extract_and_normalize_robot_code(self._task_output(raw=first))
        assert "    Wait For Elements State    ${h}    hidden" in first
        assert second == first

    def test_visibility_check_on_the_last_line_survives_a_leaked_json_brace(self):
        """The model sometimes leaks `"}` after the code; the pipeline strips it.
        The rewrite must see the stripped line, or the last-line check — where
        a "verify X is visible" step usually sits — ships unchanged."""
        raw = ("*** Settings ***\nLibrary    Browser\n\n*** Test Cases ***\nT\n"
               "    Click    id=go\n"
               '    Get Element States    ${h}    contains    visible"}')
        out = ds.extract_and_normalize_robot_code(self._task_output(raw=raw))
        assert out.endswith("\n    Wait For Elements State    ${h}    visible")

    # The q08 file as the assembler writes it (bench run 0ee28976-9924-4385-90ae-6a7259235cb2),
    # with the read locator left BARE so the pipeline's css= prefixing runs first.
    _Q08_RAW = ("*** Settings ***\nLibrary    Browser\nLibrary    BuiltIn\nLibrary    Collections\n\n"
                "*** Variables ***\n${browser}    chromium\n${headless}    True\n"
                "${url}    https://the-internet.herokuapp.com/dropdown\n"
                "${dropdown_locator}    id=dropdown\n\n"
                "*** Test Cases ***\nGenerated Test\n"
                "    [Documentation]    Auto-generated test case\n"
                "    New Browser    ${browser}    headless=${headless}\n"
                "    New Context    viewport={'width': 1920, 'height': 1080}\n"
                "    New Page    ${url}\n"
                "    Select Options By    ${dropdown_locator}    label    Option 2\n"
                "    ${selected_option}=    Get Text    #dropdown\n"
                "    Should Contain    ${selected_option}    Option 2\n"
                "    Close Browser")

    def test_select_text_read_rewritten_by_the_pipeline(self):
        """The rewrite is only worth anything if it is actually wired in here.

        Without this test, deleting the rewrite call from the pipeline leaves the
        whole suite green. The bare `#dropdown` also proves the rewrite sees the
        line AFTER normalize_robot_code made it `css=#dropdown`, which is what lets
        it match the select's `id=dropdown`.
        """
        out = ds.extract_and_normalize_robot_code(self._task_output(raw=self._Q08_RAW))
        assert "    ${selected_option}=    Get Selected Options    css=#dropdown    label\n" in out
        assert "Get Text" not in out

    def test_select_text_read_rewrite_survives_a_repair_round_trip(self):
        first = ds.extract_and_normalize_robot_code(self._task_output(raw=self._Q08_RAW))
        second = ds.extract_and_normalize_robot_code(self._task_output(raw=first))
        assert "Get Selected Options" in first
        assert second == first

    def test_select_text_read_on_the_last_line_survives_a_leaked_json_brace(self):
        raw = (self._Q08_RAW.replace(
            "    ${selected_option}=    Get Text    #dropdown\n    Should Contain    ${selected_option}    Option 2\n"
            "    Close Browser", "    Get Text    #dropdown    contains    Option 2") + '"}')
        out = ds.extract_and_normalize_robot_code(self._task_output(raw=raw))
        assert out.endswith("\n    Get Selected Options    css=#dropdown    label    contains    Option 2")

    def test_typed_value_read_rewritten_by_the_pipeline(self):
        """Bench run 8adaa3c8-ab51-4f78-8f69-38f161ba8360's shape: typed, then the
        value ATTRIBUTE read back ('' — the run failed). Wired in, and stable
        across a repair round."""
        raw = ("*** Settings ***\nLibrary    Browser\n\n*** Variables ***\n"
               "${customer_city_locator}    id=customer.address.city\n\n"
               "*** Test Cases ***\nT\n"
               "    Fill Text    ${customer_city_locator}    NewYork\n"
               "    ${city_value}=    Get Attribute    ${customer_city_locator}    value\n"
               "    Should Be True    '${city_value}' == 'NewYork'")
        first = ds.extract_and_normalize_robot_code(self._task_output(raw=raw))
        second = ds.extract_and_normalize_robot_code(self._task_output(raw=first))
        assert "    ${city_value}=    Get Property    ${customer_city_locator}    value\n" in first
        assert second == first

    # Both read rewrites are optional and skip-only: one that raises must cost only its own rewrite, never the
    # paid-for generation. The rest of cleanup (the other rewrite, the Browser timeout) still runs.
    _N1_RAW = ("*** Settings ***\nLibrary    Browser\n\n*** Variables ***\n"
               "${customer_city_locator}    id=customer.address.city\n\n"
               "*** Test Cases ***\nT\n"
               "    Fill Text    ${customer_city_locator}    NewYork\n"
               "    ${city_value}=    Get Attribute    ${customer_city_locator}    value\n"
               "    Should Be True    '${city_value}' == 'NewYork'")

    @pytest.mark.parametrize("failing, raw, still_rewritten", [
        ("rewrite_select_text_reads", _N1_RAW,
         "    ${city_value}=    Get Property    ${customer_city_locator}    value\n"),
        ("rewrite_typed_value_reads", _Q08_RAW,
         "    ${selected_option}=    Get Selected Options    css=#dropdown    label\n"),
    ], ids=["select read rewrite raises", "typed value rewrite raises"])
    def test_a_read_rewrite_that_raises_leaves_the_code_to_the_rest_of_cleanup(
            self, caplog, failing, raw, still_rewritten):
        with (patch.object(ds, failing, side_effect=RuntimeError("unexpected")),
              caplog.at_level(logging.WARNING, logger="src.backend.services.dryrun_service")):
            out = ds.extract_and_normalize_robot_code(self._task_output(raw=raw))
        assert "Library    Browser    timeout=30s" in out
        assert still_rewritten in out
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert [r.getMessage() for r in warnings] == [
            f"{failing} failed; code left unchanged by it (non-blocking)"]
        assert warnings[0].exc_info is not None

    def test_selenium_suite_untouched(self):
        raw = "*** Settings ***\nLibrary    SeleniumLibrary\n*** Test Cases ***\nT\n    Log    hi"
        out = ds.extract_and_normalize_robot_code(self._task_output(raw=raw))
        assert "timeout=" not in out


# ---------------------------------------------------------------------------
# F1 change-wait — the two never-failing lines on every repair round, and
# surviving the shared cleanup pipeline
# ---------------------------------------------------------------------------

_CW_HEAD = (
    "*** Settings ***\n"
    "Library    Browser    timeout=30s\n"
    "Library    BuiltIn\n"
    "\n"
    "*** Variables ***\n"
    "${url}    https://example.com\n"
    "${btn_locator}    id=go\n"
    "${total_locator}    css=#total\n"
    "${banner}    id=banner\n"
    "\n"
    "*** Test Cases ***\n"
    "Generated Test\n"
    "    New Browser    chromium    headless=True\n"
    "    New Page    ${url}\n"
    "    Click    ${btn_locator}\n"
    "    ${total}=    Get Text    ${total_locator}\n"
)
_CW_BASE = _CW_HEAD + "    Log    ${total}\n    Close Browser\n"
_CW_WAITS = ({"read_locator": "css=#total", "action_locator": "id=go"},)
_CW_BEFORE = ("    ${total_locator_before}=    Run Keyword And Ignore Error    "
              "Get Text    ${total_locator}")
_CW_WAIT = ("    Run Keyword And Ignore Error    Wait Until Keyword Succeeds    30s    250ms"
            "    Get Text    ${total_locator}    !=    ${total_locator_before}[1]")


def _cw_inserted(code: str) -> str:
    from src.backend.crew_ai.robot_code_normalizer import insert_change_waits
    return insert_change_waits(code, _CW_WAITS)


class TestChangeWaitsOnRepair:
    """validate_and_repair puts the pair into every repaired test it re-checks and returns."""

    def _settings(self, max_fixes=2):
        s = MagicMock()
        s.DRYRUN_ENABLED = True
        s.MAX_DRYRUN_FIXES = max_fixes
        s.DRYRUN_TIMEOUT = 120
        return s

    def _gate(self, robot_code, repaired, **kwargs):
        """One failed dryrun, one repair that extracts to `repaired`, then a pass."""
        with patch.object(ds, "settings", self._settings()), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc, \
             patch.object(ds, "repair_robot_code",
                          return_value=(MagicMock(), {"llm_calls": 1, "cost": 0.001}, {})), \
             patch.object(ds, "extract_and_normalize_robot_code", return_value=repaired):
            mock_rc.ensure_image.return_value = {"status": "ready"}
            mock_rc.dryrun.side_effect = [
                {"passed": False, "errors": "bad kw", "exit_code": 1},
                {"passed": True, "errors": "", "exit_code": 0},
            ]
            out = ds.validate_and_repair("rid", robot_code, "gemini", "m", None, **kwargs)
        return out, mock_rc

    def test_a_repair_without_the_pair_gets_it_before_the_gate_re_checks_it(self):
        inserted = _cw_inserted(_CW_BASE)
        assert inserted != _CW_BASE  # control: the pair really is placed in this file
        out, mock_rc = self._gate(inserted.replace("Click", "Cilck"), _CW_BASE,
                                  change_waits=_CW_WAITS)
        assert out["dryrun_status"] == "passed"
        assert out["code"] == inserted
        assert out["code"].count(_CW_BEFORE) == 1
        assert out["code"].count(_CW_WAIT) == 1
        # the dryrun re-ran on the text WITH the pair, not on the bare repair
        assert mock_rc.dryrun.call_args_list[1].args[1] == inserted

    def test_a_repair_that_already_holds_the_pair_still_has_it_exactly_once(self):
        inserted = _cw_inserted(_CW_BASE)
        out, mock_rc = self._gate(inserted.replace("Click", "Cilck"), inserted,
                                  change_waits=_CW_WAITS)
        assert out["code"] == inserted
        assert out["code"].count(_CW_BEFORE) == 1
        assert out["code"].count(_CW_WAIT) == 1
        assert mock_rc.dryrun.call_args_list[1].args[1] == inserted

    def test_no_change_waits_leaves_the_repaired_code_exactly_as_extracted(self):
        for kwargs in ({}, {"change_waits": None}, {"change_waits": ()}):
            out, mock_rc = self._gate(_CW_BASE.replace("Click", "Cilck"), _CW_BASE, **kwargs)
            assert out["code"] == _CW_BASE, kwargs
            assert mock_rc.dryrun.call_args_list[1].args[1] == _CW_BASE, kwargs

    def test_a_repair_that_only_lacks_the_pair_is_no_progress(self):
        """The no-change check compares what the gate would re-check, so a repair
        that differs from the current code only by the missing pair stops the loop
        instead of burning a container run on identical text."""
        inserted = _cw_inserted(_CW_BASE)
        with patch.object(ds, "settings", self._settings(max_fixes=3)), \
             patch("src.backend.services.dryrun_service.runner_exec_client") as mock_rc, \
             patch.object(ds, "repair_robot_code",
                          return_value=(MagicMock(), {"llm_calls": 1, "cost": 0.001}, {})), \
             patch.object(ds, "extract_and_normalize_robot_code", return_value=_CW_BASE):
            mock_rc.ensure_image.return_value = {"status": "ready"}
            mock_rc.dryrun.return_value = {"passed": False, "errors": "bad kw", "exit_code": 1}
            out = ds.validate_and_repair("rid", inserted, "gemini", "m", None,
                                         change_waits=_CW_WAITS)
        assert out["dryrun_status"] == "failed"
        assert mock_rc.dryrun.call_count == 1
        assert out["code"] == inserted


class TestChangeWaitsSurviveTheCleanup:
    """A repair round normalizes code that already holds the pair."""

    def _task_output(self, raw):
        out = MagicMock()
        out.pydantic = None
        out.json_dict = None
        out.raw = raw
        return out

    def test_the_pair_comes_through_extract_and_normalize_byte_identical(self):
        inserted = _cw_inserted(_CW_BASE)
        assert inserted != _CW_BASE  # control: there is a pair to survive
        out = ds.extract_and_normalize_robot_code(self._task_output(inserted))
        assert out == inserted.rstrip("\n")  # the pipeline's own end-of-file trim, nothing else
        assert out.count(_CW_BEFORE + "\n") == 1
        assert out.count(_CW_WAIT + "\n") == 1

    def test_the_other_rewrites_still_fire_beside_the_pair(self):
        banner = "    Get Element States    ${banner}    contains    visible\n"
        base = _CW_HEAD + banner + "    Close Browser\n"
        with_pair = _cw_inserted(base)
        assert with_pair != base  # control: the pair is placed in this file too
        out_with = ds.extract_and_normalize_robot_code(self._task_output(with_pair))
        out_without = ds.extract_and_normalize_robot_code(self._task_output(base))
        wait_banner = "    Wait For Elements State    ${banner}    visible\n"
        # control: the visibility rewrite fires in BOTH files
        assert wait_banner in out_without and "Get Element States" not in out_without
        assert wait_banner in out_with and "Get Element States" not in out_with
        # the pair is there once, and the same file without it differs by exactly those two lines
        assert out_with.count(_CW_BEFORE + "\n") == 1
        assert out_with.count(_CW_WAIT + "\n") == 1
        assert (out_with.replace(_CW_BEFORE + "\n", "").replace(_CW_WAIT + "\n", "")
                == out_without)


# ---------------------------------------------------------------------------
# run_dryrun_in_container — security hardening (Phase 4)
# ---------------------------------------------------------------------------

class _Captured(Exception):
    pass


def test_dryrun_container_is_hardened(tmp_path):
    captured = {}
    client = MagicMock()

    def _capture(**kw):
        captured.update(kw)
        raise _Captured()
    client.containers.run.side_effect = _capture

    # run_dryrun_in_container writes the dryrun file via the artifact store BEFORE
    # containers.run — point it at tmp_path so the write is harmless.
    store = MagicMock()
    store.run_dir.return_value = tmp_path

    with patch.object(ds, "get_artifact_store", return_value=store), \
         patch.object(ds, "resolve_host_robot_tests_dir", return_value=str(tmp_path)), \
         patch.object(ds, "normalize_docker_mount_source", side_effect=lambda p: p), \
         patch.object(ds, "_force_remove_stale_container"):
        try:
            ds.run_dryrun_in_container(client, "r1", "*** Test Cases ***\n")
        except _Captured:
            pass

    assert captured["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in captured["security_opt"]
    assert captured["network_mode"] == "none"
    assert captured["mem_limit"] == "512m"
    assert captured["pids_limit"] == 128


# ---------------------------------------------------------------------------
# validate_and_repair — executor-hop contract (Task 8)
# ---------------------------------------------------------------------------

def test_validate_and_repair_unverified_when_executor_down(monkeypatch):
    monkeypatch.setattr(dryrun_service.settings, "DRYRUN_ENABLED", True)
    with patch("src.backend.services.dryrun_service.runner_exec_client") as rc:
        rc.ensure_image.side_effect = dryrun_service.RunnerExecUnavailable("down")
        out = dryrun_service.validate_and_repair(
            "abc123", "*** Test Cases ***\nT\n    Log    x\n",
            "gemini", "gemini-2.5-flash", progress_queue=None)
    assert out["dryrun_status"] == "unverified"
    assert out["code"].strip().startswith("*** Test Cases ***")


def test_validate_and_repair_passes_via_client(monkeypatch):
    monkeypatch.setattr(dryrun_service.settings, "DRYRUN_ENABLED", True)
    with patch("src.backend.services.dryrun_service.runner_exec_client") as rc:
        rc.ensure_image.return_value = {"status": "ready"}
        rc.dryrun.return_value = {"passed": True}
        out = dryrun_service.validate_and_repair(
            "abc123", "*** Test Cases ***\nT\n    Log    x\n",
            "gemini", "gemini-2.5-flash", progress_queue=None)
    assert out["dryrun_status"] == "passed"
    rc.dryrun.assert_called_once()


def test_dryrun_container_mounts_only_the_runs_dryrun_directory(tmp_path):
    """Same tenant boundary as run_test_in_container: the gate container gets
    the run's dryrun/ directory and nothing else, at the in-container path the
    robot --dryrun command already uses."""
    captured = {}
    client = MagicMock()

    def _capture(**kw):
        captured.update(kw)
        raise _Captured()
    client.containers.run.side_effect = _capture

    store = MagicMock()
    store.run_dir.return_value = tmp_path / "r1"

    with patch.object(ds, "get_artifact_store", return_value=store), \
         patch.object(ds, "resolve_host_robot_tests_dir", return_value=str(tmp_path)), \
         patch.object(ds, "normalize_docker_mount_source", side_effect=lambda p: p), \
         patch.object(ds, "_force_remove_stale_container"):
        try:
            ds.run_dryrun_in_container(client, "r1", "*** Test Cases ***\n")
        except _Captured:
            pass

    assert captured["volumes"] == {
        os.path.join(str(tmp_path), "r1", "dryrun"): {
            "bind": "/app/robot_tests/r1/dryrun", "mode": "rw"},
    }
    assert captured["command"] == [
        "robot", "--dryrun",
        "--outputdir", "/app/robot_tests/r1/dryrun",
        "/app/robot_tests/r1/dryrun/dryrun.robot",
    ]
    # The mount is only correct if the file the command names is inside it.
    # Asserting the config dict alone passes even when the two paths disagree.
    mount_source = next(iter(captured["volumes"]))
    assert os.path.exists(os.path.join(mount_source, "dryrun.robot"))
