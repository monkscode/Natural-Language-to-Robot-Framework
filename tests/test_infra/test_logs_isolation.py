"""Regression tests for the pytest-session isolation of logs/.

The defect (measured 2026-09-14): a full local gate wrote four paths under the
repo's logs/, the directory the running API container writes too:
application.log (1,229 records, two of them fake slowapi "ratelimit" lines the
429 count matches, plus one rollover of the live file), a
temp_metrics/<uuid>.json, crewai.log.txt and crewai_steps.log.

The redirect is pinned here: every log path the suite can reach points into
the session's LOG_DIR, outside the repo. So is the per-test clear of
structlog's context: a workflow_id one test left bound made every later
browser-tool test write a temp-metrics file. The guard that refuses whatever
still reaches <repo>/logs is pinned in test_staging_isolation_guard.py.

Referenced by: none (leaf test module).
Depends on: tests/conftest.py (the LOG_DIR pin, _isolated_staging_root,
_clear_structlog_contextvars), tests/isolation_guard.py (redirect_logs).
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _in_repo(path) -> bool:
    return Path(path).resolve().is_relative_to(REPO_ROOT)


def _session_log_dir() -> Path:
    return Path(os.environ["LOG_DIR"]).resolve()


def test_the_session_log_dir_is_outside_the_repo():
    """main.py opens application.log under LOG_DIR when it is imported, and
    test modules that import it at module level do so during collection."""
    log_dir = os.environ.get("LOG_DIR")

    assert log_dir, "tests/conftest.py must set LOG_DIR before anything imports src.backend.main"
    assert not _in_repo(log_dir)


def test_the_temp_metrics_store_writes_into_the_session_log_dir():
    """The browser tool writes here, and app startup deletes files older than
    24 h here — so an unredirected boot test could delete real ones."""
    from src.backend.core.temp_metrics_storage import get_temp_metrics_storage

    storage_dir = get_temp_metrics_storage().storage_dir

    assert not _in_repo(storage_dir)
    assert Path(storage_dir).resolve() == _session_log_dir() / "temp_metrics"


def test_the_crewai_step_log_is_in_the_session_log_dir():
    from src.backend.crew_ai import callbacks

    assert not _in_repo(callbacks.CREWAI_STEP_LOG_FILE)
    assert Path(callbacks.CREWAI_STEP_LOG_FILE).resolve() == _session_log_dir() / "crewai_steps.log"


def test_the_crewai_run_log_is_in_the_session_log_dir():
    from src.backend.crew_ai import crew

    assert not _in_repo(crew.CREWAI_LOG_FILE)
    assert Path(crew.CREWAI_LOG_FILE).resolve() == _session_log_dir() / "crewai.log.txt"


# ---------------------------------------------------------------------------
# What tests/conftest.py does at import can only be observed from a fresh
# pytest process that loads it: by the time any test here runs, it has run.
# ---------------------------------------------------------------------------

def _run_session_with_conftest(tmp_path, body, env):
    test_file = tmp_path / "test_inner.py"
    test_file.write_text(textwrap.dedent(body), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.conftest", "-p", "no:cacheprovider",
         "-q", "--rootdir", str(tmp_path), str(test_file)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180,
    )


def test_an_exported_log_dir_does_not_reach_the_session(tmp_path):
    """Forced, not setdefault: a shell that exported the real logs dir must not
    point the session at it."""
    exported = str(REPO_ROOT / "logs")
    result = _run_session_with_conftest(tmp_path, f"""
        import os

        def test_the_session_log_dir_is_not_the_exported_one():
            assert os.environ["LOG_DIR"] != {exported!r}
    """, dict(os.environ, LOG_DIR=exported))

    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr


def test_a_later_test_starts_with_an_empty_structlog_context(tmp_path):
    """run_agentic_workflow binds a fresh workflow_id in the calling thread and
    nothing clears it: production runs each workflow on its own thread, a test
    runs it on the main one. The browser tool reads workflow_id from that
    context, so every later test that called the tool wrote a temp-metrics file."""
    result = _run_session_with_conftest(tmp_path, """
        import structlog

        def test_a_binds_a_workflow_id():
            structlog.contextvars.bind_contextvars(workflow_id="wf-leak")

        def test_b_starts_with_nothing_bound():
            assert structlog.contextvars.get_contextvars() == {}
    """, dict(os.environ))

    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
