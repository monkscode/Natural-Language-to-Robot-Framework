"""Regression tests for the pytest-session artifact-staging isolation.

The defect (measured 2026-09-14): the artifact store's staging root is a module
constant pointing at the repo's robot_tests/, and nothing in the suite
redirected it. A test whose re-run reached the execution stream left a real
run directory there on every run — 640 had accumulated — and whenever
run.sh's runner-exec was listening on 127.0.0.1:4998, the suite executed that
directory in a real container.

Two halves are pinned here: the redirect (every copy of the root points at a
temp dir) and the guard (anything that still reaches the real root or the
runner is refused, recorded, and fails the session). The guard refuses the
real logs/ directory the same way; the logs redirect is pinned in
test_logs_isolation.py.

Referenced by: none (leaf test module).
Depends on: tests/isolation_guard.py, tests/conftest.py (_isolated_staging_root,
pytest_configure).
"""

import errno
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests import isolation_guard
from tests.isolation_guard import REPO_STAGING_ROOT, StagingGuard

# <repo-root>/logs, derived here rather than read from the guard.
REAL_LOGS_DIR = Path(__file__).resolve().parents[2] / "logs"


def test_the_guard_names_the_directory_production_stages_into():
    """artifact_store computes the root from its own location. If either file
    moves, the guard must not silently watch a directory nothing writes to."""
    from src.backend.core import artifact_store

    assert Path(artifact_store.__file__).resolve().parents[3] / "robot_tests" == REPO_STAGING_ROOT


def test_the_session_stages_into_a_temp_dir_not_the_repo():
    from src.backend.core import artifact_store
    from src.backend.services import docker_service

    assert artifact_store.STAGING_ROOT != REPO_STAGING_ROOT
    assert artifact_store.get_artifact_store().staging_root == artifact_store.STAGING_ROOT
    assert docker_service.ROBOT_TESTS_DIR == str(artifact_store.STAGING_ROOT)
    assert docker_service.HOST_ROBOT_TESTS_DIR == str(artifact_store.STAGING_ROOT)


class TestRedirectStaging:
    """redirect_staging must rebind EVERY copy of the root: docker_service and
    bench.run_bench copy it at import, so rebinding the constant alone leaves
    them pointing at the repo."""

    @pytest.fixture
    def restore_session_root(self):
        from src.backend.core import artifact_store

        session_root = artifact_store.STAGING_ROOT
        yield
        isolation_guard.redirect_staging(session_root)

    def test_it_rebinds_the_constant_the_cached_store_and_both_import_time_copies(
            self, tmp_path, restore_session_root):
        import bench.run_bench
        from src.backend.core import artifact_store
        from src.backend.services import docker_service

        artifact_store._store = object()  # a store built from the old root

        isolation_guard.redirect_staging(tmp_path)

        assert artifact_store.STAGING_ROOT == tmp_path
        assert artifact_store._store is None
        assert docker_service.ROBOT_TESTS_DIR == str(tmp_path)
        assert docker_service.HOST_ROBOT_TESTS_DIR == str(tmp_path)
        assert bench.run_bench.STAGING_ROOT == tmp_path


# ---------------------------------------------------------------------------
# The guard, on an instance that is NOT installed: what it refuses.
# ---------------------------------------------------------------------------

class TestWhatTheGuardRefuses:
    @pytest.fixture
    def root(self, tmp_path):
        return tmp_path / "robot_tests"

    @pytest.fixture
    def guard(self, root):
        return StagingGuard([root], "http://127.0.0.1:4998")

    @staticmethod
    def outcome(guard, event, *args):
        """The error the hook raised for this event, or None if it let it through."""
        try:
            guard.hook(event, args)
        except OSError as e:
            return e
        return None

    @pytest.mark.parametrize("event, args", [
        ("open", ("{root}/run-1/test.robot", "w", 0)),
        ("open", ("{root}/run-1/test.robot", "r+", 0)),
        ("open", ("{root}/run-1/output.xml", "ab", 0)),
        ("open", ("{root}/run-1/test.robot", None, os.O_WRONLY | os.O_CREAT)),
        ("os.mkdir", ("{root}", 0o777, -1)),
        ("os.mkdir", ("{root}/run-1", 0o777, -1)),
        ("os.chmod", ("{root}/run-1", 0o777, -1)),
        ("os.remove", ("{root}/run-1/output.xml", -1)),
        ("os.rmdir", ("{root}/run-1", -1)),
        ("shutil.rmtree", ("{root}/run-1", None)),
        ("os.truncate", ("{root}/run-1/log.html", 0)),
        ("os.rename", ("{root}/run-1", "{outside}/moved", -1, -1)),
        ("os.rename", ("{outside}/x", "{root}/run-1", -1, -1)),
        ("shutil.move", ("{root}/run-1", "{outside}/moved")),
        ("shutil.copyfile", ("{outside}/x", "{root}/run-1/test.robot")),
        ("shutil.copytree", ("{outside}/x", "{root}/run-1")),
        ("os.symlink", ("{outside}/x", "{root}/run-1", -1)),
        ("os.link", ("{outside}/x", "{root}/run-1/test.robot", -1, -1, True)),
    ])
    def test_a_write_or_delete_under_the_root_is_refused(self, guard, root, tmp_path, event, args):
        real_args = tuple(a.format(root=root, outside=tmp_path) if isinstance(a, str) else a for a in args)

        err = self.outcome(guard, event, *real_args)

        assert isinstance(err, PermissionError), f"{event} {real_args} was let through"
        assert err.errno == errno.EACCES

    def test_a_bytes_path_is_judged_like_a_str_path(self, guard, root):
        assert isinstance(self.outcome(guard, "open", os.fsencode(root / "run-1" / "t"), "w", 0), PermissionError)

    @pytest.mark.skipif(sys.platform != "win32", reason="the \\\\?\\ long-path prefix exists only on Windows")
    def test_a_windows_long_path_is_judged_like_a_plain_one(self, guard, root):
        assert isinstance(self.outcome(guard, "open", "\\\\?\\" + str(root / "run-1" / "t"), "w", 0), PermissionError)

    @pytest.mark.parametrize("event, args", [
        ("open", ("{root}/run-1/test.robot", "r", 0)),
        ("open", ("{root}/run-1/output.xml", "rb", 0)),
        ("open", ("{root}/run-1/test.robot", None, os.O_RDONLY)),
        ("open", ("{outside}/robot_tests_old/test.robot", "w", 0)),
        ("open", ("{outside}/logs/application.log", "a", 0)),
        ("open", (7, "w", 0)),
        ("shutil.copyfile", ("{root}/run-1/test.robot", "{outside}/copy.robot")),
        ("os.mkdir", ("{outside}/robot_tests_old", 0o777, -1)),
        ("compile", (b"x = 1", "<string>")),
        ("open", ("{root}/run-1/test.robot",)),
    ])
    def test_reads_other_paths_other_events_and_malformed_args_pass(self, guard, root, tmp_path, event, args):
        """A read of the real root is harmless, a sibling that merely starts
        with the same name is not the root, and the hook sees every open in the
        process — so an event it cannot parse must pass, never raise."""
        real_args = tuple(a.format(root=root, outside=tmp_path) if isinstance(a, str) else a for a in args)

        assert self.outcome(guard, event, *real_args) is None
        assert guard.violations == []

    @pytest.mark.parametrize("address", [("127.0.0.1", 4998), ("::1", 4998, 0, 0), ("10.0.0.5", 4998)])
    def test_a_connect_to_the_runner_port_is_refused_on_any_host(self, guard, address):
        err = self.outcome(guard, "socket.connect", None, address)

        assert isinstance(err, ConnectionRefusedError)

    @pytest.mark.parametrize("address", [("127.0.0.1", 5432), ("127.0.0.1", 4999), "/var/run/docker.sock"])
    def test_a_connect_anywhere_else_passes(self, guard, address):
        assert self.outcome(guard, "socket.connect", None, address) is None

    def test_the_runner_port_follows_the_configured_url(self, guard):
        guard.set_runner_url("http://127.0.0.1:4988")

        assert isinstance(self.outcome(guard, "socket.connect", None, ("127.0.0.1", 4988)), ConnectionRefusedError)
        assert self.outcome(guard, "socket.connect", None, ("127.0.0.1", 4998)) is None

    def test_a_runner_on_a_well_known_port_is_refused_only_on_loopback(self, guard):
        """Port 80 is shared with everything else a test might reach, so there
        only a loopback connect can be the runner."""
        guard.set_runner_url("http://runner-exec")

        assert isinstance(self.outcome(guard, "socket.connect", None, ("127.0.0.1", 80)), ConnectionRefusedError)
        assert isinstance(self.outcome(guard, "socket.connect", None, ("::ffff:127.0.0.1", 80, 0, 0)),
                          ConnectionRefusedError)
        assert self.outcome(guard, "socket.connect", None, ("93.184.216.34", 80)) is None

    def test_each_refusal_is_recorded_with_the_test_that_made_it(self, guard, root):
        target = str(root / "run-1" / "test.robot")

        with patch.dict(os.environ, {"PYTEST_CURRENT_TEST": "tests/x.py::test_y (call)"}):
            self.outcome(guard, "open", target, "w", 0)
            self.outcome(guard, "socket.connect", None, ("127.0.0.1", 4998))

        assert guard.violations == [
            ("tests/x.py::test_y (call)", "open", target),
            ("tests/x.py::test_y (call)", "socket.connect", "127.0.0.1:4998"),
        ]


class TestTheGuardWithTwoRoots:
    """The session guard watches the staging root AND the logs dir: a write
    under either is refused, and neither root may shadow the other."""

    @pytest.fixture
    def staging(self, tmp_path):
        return tmp_path / "robot_tests"

    @pytest.fixture
    def logs(self, tmp_path):
        return tmp_path / "logs"

    @pytest.fixture
    def guard(self, staging, logs):
        return StagingGuard([staging, logs])

    @pytest.mark.parametrize("event, args", [
        ("os.mkdir", ("{logs}", 0o777, -1)),
        ("open", ("{logs}/application.log", "a", 0)),
        ("os.rename", ("{logs}/application.log", "{logs}/application.log.1", -1, -1)),
        ("os.remove", ("{logs}/application.log.5", -1)),
        ("os.mkdir", ("{logs}/temp_metrics", 0o777, -1)),
        ("open", ("{logs}/temp_metrics/wf-1.json", "w", 0)),
        ("open", ("{logs}/crewai_steps.log", "a", 0)),
        ("open", ("{staging}/run-1/test.robot", "w", 0)),
    ])
    def test_a_write_under_either_root_is_refused(self, guard, staging, logs, event, args):
        real_args = tuple(a.format(staging=staging, logs=logs) if isinstance(a, str) else a for a in args)

        assert isinstance(TestWhatTheGuardRefuses.outcome(guard, event, *real_args), PermissionError)

    @pytest.mark.parametrize("event, args", [
        ("open", ("{logs}/application.log", "r", 0)),
        ("os.mkdir", ("{outside}/logs_old", 0o777, -1)),
        ("open", ("{outside}/session/application.log", "a", 0)),
    ])
    def test_a_read_or_a_path_beside_the_logs_root_passes(self, guard, logs, tmp_path, event, args):
        real_args = tuple(a.format(logs=logs, outside=tmp_path) if isinstance(a, str) else a for a in args)

        assert TestWhatTheGuardRefuses.outcome(guard, event, *real_args) is None
        assert guard.violations == []


class TestReportAndFail:
    @staticmethod
    def session(exitstatus):
        lines = []
        reporter = SimpleNamespace(write_line=lambda line, **_kw: lines.append(line),
                                   write_sep=lambda sep, title, **_kw: lines.append(title))
        config = SimpleNamespace(pluginmanager=SimpleNamespace(get_plugin=lambda name: reporter))
        return SimpleNamespace(exitstatus=exitstatus, config=config), lines

    @staticmethod
    def guard_with_one_violation(tmp_path):
        guard = StagingGuard([tmp_path / "robot_tests"])
        guard.violations.append(("tests/x.py::test_y (call)", "open", "C:/repo/robot_tests/r/test.robot"))
        return guard

    def test_a_clean_session_is_left_alone(self, tmp_path):
        session, lines = self.session(pytest.ExitCode.OK)

        StagingGuard([tmp_path / "robot_tests"]).report_and_fail(session)

        assert session.exitstatus == pytest.ExitCode.OK
        assert lines == []

    @pytest.mark.parametrize("before", [pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED])
    def test_a_violation_fails_a_session_that_would_have_passed(self, tmp_path, before):
        session, lines = self.session(before)

        self.guard_with_one_violation(tmp_path).report_and_fail(session)

        assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
        report = "\n".join(lines)
        assert "tests/x.py::test_y (call)" in report
        assert "C:/repo/robot_tests/r/test.robot" in report

    @pytest.mark.parametrize("before", [pytest.ExitCode.TESTS_FAILED, pytest.ExitCode.INTERRUPTED])
    def test_a_violation_is_reported_without_masking_a_worse_exit_status(self, tmp_path, before):
        session, lines = self.session(before)

        self.guard_with_one_violation(tmp_path).report_and_fail(session)

        assert session.exitstatus == before
        assert lines

    def test_the_report_names_every_refused_root(self, tmp_path):
        """A connect names neither root, so only the header can put them in the report."""
        staging, logs = tmp_path / "robot_tests", tmp_path / "logs"
        guard = StagingGuard([staging, logs])
        guard.violations.append(("tests/x.py::test_y (call)", "socket.connect", "127.0.0.1:4998"))
        session, lines = self.session(pytest.ExitCode.OK)

        guard.report_and_fail(session)

        report = "\n".join(lines)
        assert str(staging) in report
        assert str(logs) in report


# ---------------------------------------------------------------------------
# The guard as installed in THIS session. sys.audit raises the event without
# touching the disk or the network, so these prove the hook is live at no risk.
# ---------------------------------------------------------------------------

@pytest.fixture
def session_violations():
    """Hands back the session guard's violation list and removes whatever a
    test added, so a deliberate self-test does not fail the whole run."""
    before = len(isolation_guard.GUARD.violations)
    yield isolation_guard.GUARD.violations
    del isolation_guard.GUARD.violations[before:]


def test_this_session_refuses_a_write_under_the_real_root(session_violations):
    target = str(REPO_STAGING_ROOT / "__guard_selftest__" / "test.robot")
    before = len(session_violations)

    with pytest.raises(PermissionError):
        sys.audit("open", target, "w", 0)

    assert session_violations[before:] and session_violations[before][2] == target


def test_this_session_refuses_a_write_under_the_real_logs_dir(session_violations):
    """Unredirected, application.log, both crewai logs and temp_metrics land
    here — the directory the running API container writes too."""
    target = str(REAL_LOGS_DIR / "__guard_selftest__.log")
    before = len(session_violations)

    with pytest.raises(PermissionError):
        sys.audit("open", target, "a", 0)

    assert session_violations[before][2] == target


def test_this_session_refuses_the_configured_runner(session_violations):
    from urllib.parse import urlsplit

    from src.backend.core.config import settings

    with pytest.raises(ConnectionRefusedError):
        sys.audit("socket.connect", None, ("127.0.0.1", urlsplit(settings.RUNNER_EXEC_URL).port))


def test_the_session_takes_the_runner_url_from_settings(tmp_path):
    """isolate_session must read RUNNER_EXEC_URL from settings: .env can name a
    runner on another port, and that is the one the client would call."""
    from src.backend.core import artifact_store
    from src.backend.core.config import settings

    session_root = artifact_store.STAGING_ROOT
    try:
        with patch.object(settings, "RUNNER_EXEC_URL", "http://127.0.0.1:4988"):
            isolation_guard.isolate_session(tmp_path, Path(os.environ["LOG_DIR"]))
        probe = StagingGuard([tmp_path])
        probe.set_runner_url("http://127.0.0.1:4988")
        assert isolation_guard.GUARD.runner_port == probe.runner_port == 4988
        assert artifact_store.STAGING_ROOT == tmp_path
    finally:
        isolation_guard.GUARD.set_runner_url(settings.RUNNER_EXEC_URL)
        isolation_guard.redirect_staging(session_root)


def test_the_guard_is_registered_so_it_can_fail_the_session(request):
    assert request.config.pluginmanager.is_registered(isolation_guard)


# ---------------------------------------------------------------------------
# End to end: a real pytest process, with the guard loaded as a plugin, must
# exit non-zero when a test that PASSED reached the real root — and zero when
# nothing did. The event is raised with sys.audit, so no file is written.
# ---------------------------------------------------------------------------

def _run_inner_session(tmp_path, body):
    test_file = tmp_path / "test_inner.py"
    test_file.write_text(textwrap.dedent(body), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.isolation_guard", "-p", "no:cacheprovider",
         "-q", "--rootdir", str(tmp_path), str(test_file)],
        cwd=REPO_STAGING_ROOT.parent, capture_output=True, text=True, timeout=120,
    )


@pytest.mark.parametrize("target", [
    str(REPO_STAGING_ROOT / "__guard_e2e__" / "test.robot"),
    str(REAL_LOGS_DIR / "__guard_e2e__.log"),
], ids=["staging", "logs"])
def test_a_passing_session_that_reached_the_real_root_exits_failed(tmp_path, target):
    result = _run_inner_session(tmp_path, f"""
        import sys

        def test_swallows_the_refusal_and_passes():
            try:
                sys.audit("open", {target!r}, "w", 0)
            except PermissionError:
                pass
    """)

    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert target in result.stdout


def test_a_passing_session_that_stayed_inside_its_temp_dir_exits_ok(tmp_path):
    target = str(tmp_path / "robot_tests" / "test.robot")
    result = _run_inner_session(tmp_path, f"""
        import sys

        def test_writes_somewhere_harmless():
            sys.audit("open", {target!r}, "w", 0)
    """)

    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
