"""main.py sends application.log to LOG_DIR, read from the process environment.

setup_logging() runs when src.backend.main is IMPORTED, before settings exists,
so LOG_DIR is an os.environ read rather than a Settings field. An import-time
call only runs again in a fresh interpreter, so each case imports main in a
child process. Its working directory is a temp dir: when LOG_DIR is unset the
child writes ./logs there, never into the repo.

Referenced by: none (leaf test module).
Depends on: src/backend/main.py, src/backend/config/logging_config.py.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_STARTUP = "Starting application"


def _import_main(cwd: Path, log_dir: str | None) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    env.pop("LOG_DIR", None)
    if log_dir is not None:
        env["LOG_DIR"] = log_dir
    return subprocess.run([sys.executable, "-c", "import src.backend.main"],
                          cwd=cwd, env=env, capture_output=True, text=True, timeout=120)


def _startup_line(log_file: Path) -> str:
    """The event of the startup record main.py writes right after setup_logging()."""
    for line in log_file.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line).get("event", "")
        except ValueError:
            continue
        if event.startswith(_STARTUP):
            return event
    raise AssertionError(f"no startup record in {log_file}")


def test_application_log_goes_to_log_dir_when_it_is_set(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    log_dir = tmp_path / "elsewhere"

    result = _import_main(cwd, str(log_dir))

    assert result.returncode == 0, result.stderr
    assert (log_dir / "application.log").is_file()
    assert not (cwd / "logs").exists()
    assert f"{log_dir}/application.log" in _startup_line(log_dir / "application.log")


@pytest.mark.parametrize("log_dir", [None, ""], ids=["unset", "empty"])
def test_application_log_falls_back_to_logs_in_the_working_dir(tmp_path, log_dir):
    """Unset or blank keeps ./logs, which is where the owner's stack and the
    containers' ./logs mount expect it."""
    result = _import_main(tmp_path, log_dir)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "logs" / "application.log").is_file()
    assert "logs/application.log" in _startup_line(tmp_path / "logs" / "application.log")
