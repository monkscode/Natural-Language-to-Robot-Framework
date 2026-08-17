"""Guards over setup_logging's two callers-visible contracts.

Two processes call setup_logging with different needs, and each one's
requirement is invisible from the other's vantage point:

  * main.py wants a file. logs/application.log is what the 429-count
    procedure and the LLM-trace guide tell you to grep.
  * runner_exec/app.py must NOT have one. Both containers bind-mount ./logs
    (docker-compose.yml) and run.sh starts both processes in the same working
    directory, so a second RotatingFileHandler on application.log means two
    processes rotating one file — lost records, possible truncation. Alloy
    scrapes container stdout, not the file, so the executor loses nothing by
    not writing one.

The /health filter is here rather than in either app because both processes
are polled by an identical 30s Docker healthcheck, and the noise is measured
on the API — not on the executor that prompted the work.

Referenced by: nothing — pytest entry point.
Depends on: src/backend/config/logging_config.py.
"""
import logging
import logging.handlers
import sys
from pathlib import Path

import pytest
import structlog

from src.backend.config.logging_config import (
    HealthCheckAccessFilter,
    setup_logging,
)
from src.backend.core.secret_redaction import SecretRedactingFilter

ACCESS_LOGGER = "uvicorn.access"
# uvicorn's access format: '%s - "%s %s HTTP/%s" %d' with
# (client_addr, method, path_with_query, http_version, status).
ACCESS_FORMAT = '%s - "%s %s HTTP/%s" %d'


@pytest.fixture(autouse=True)
def restore_global_logging():
    """setup_logging mutates process-global state; put it all back.

    Without this, the first test to run leaves root with a stdout-only
    handler and every later test in the session inherits it.
    """
    root = logging.getLogger()
    access = logging.getLogger(ACCESS_LOGGER)
    saved = (root.handlers[:], root.level, access.filters[:], structlog.get_config())
    yield
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    access.filters[:] = saved[2]
    structlog.configure(**saved[3])


def _access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        ACCESS_LOGGER, logging.INFO, __file__, 1, ACCESS_FORMAT,
        ("127.0.0.1:52000", "GET", path, "1.1", 200), None,
    )


def _survives(record: logging.LogRecord) -> bool:
    return all(f.filter(record) for f in logging.getLogger(ACCESS_LOGGER).filters)


def _file_handlers() -> list[logging.handlers.RotatingFileHandler]:
    return [h for h in logging.getLogger().handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)]


def _stdout_handlers() -> list[logging.StreamHandler]:
    # RotatingFileHandler subclasses StreamHandler, so the stream identity
    # check is what separates the two — not the type.
    return [h for h in logging.getLogger().handlers
            if isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) is sys.stdout]


def test_stdout_only_mode_opens_no_file():
    """log_dir=None is the executor's mode: one stdout handler, no file.

    A file handler here would be a second writer on the API's rotating
    application.log.
    """
    setup_logging(log_dir=None)

    assert _file_handlers() == [], (
        "log_dir=None still installed a file handler; the executor would "
        "share application.log with the API process")
    assert len(_stdout_handlers()) == 1, (
        "expected exactly one stdout handler; a second one double-emits "
        "every record into the Docker log Alloy scrapes")
    assert logging.getLogger().isEnabledFor(logging.INFO), (
        "root is not at INFO, so every logging.info('[WARMUP] ...') is still "
        "discarded — the bug this whole change exists to fix")


def test_stdout_only_mode_creates_no_log_directory(tmp_path, monkeypatch):
    """Nothing on disk. The directory itself is a side effect worth not having."""
    monkeypatch.chdir(tmp_path)

    setup_logging(log_dir=None)

    assert not (tmp_path / "logs").exists()


def test_file_mode_is_unchanged(tmp_path):
    """The API's contract: application.log plus a console copy.

    Guarded because the executor's needs are what changed this function, and
    silently dropping the API's file would break the documented grep
    procedures without failing anything else.
    """
    setup_logging(log_dir=str(tmp_path))

    files = _file_handlers()
    assert len(files) == 1
    assert Path(files[0].baseFilename) == tmp_path / "application.log"
    assert len(_stdout_handlers()) == 1, "the console copy is missing"


def test_health_polls_are_dropped_and_real_requests_survive():
    """Docker polls /health every 30s on both processes.

    Over the five days Loki held, the uvicorn access channel carried 6,408
    lines; nothing polls /health but the healthcheck, whose own rate is 1,440
    a day. Those lines carry no information and crowd out the ones that do.
    """
    setup_logging(log_dir=None)

    assert not _survives(_access_record("/health"))
    assert not _survives(_access_record("/health?probe=1")), (
        "the query string defeated the match; a health poll with any query "
        "would be logged")
    assert _survives(_access_record("/execute"))
    assert _survives(_access_record("/healthz")), (
        "matched on prefix rather than on the whole path — an unrelated route "
        "starting with /health would be silently dropped")


def test_a_record_the_filter_does_not_recognise_is_kept():
    """Drop only what is positively identified as a health poll.

    uvicorn's access format is not a stable contract across versions. If it
    changes shape, the correct failure is noisier logs, never silent loss.
    """
    setup_logging(log_dir=None)

    unknown_shape = logging.LogRecord(
        ACCESS_LOGGER, logging.INFO, __file__, 1, "startup complete", None, None)
    assert _survives(unknown_shape)

    wrong_arity = logging.LogRecord(
        ACCESS_LOGGER, logging.INFO, __file__, 1, "%s %s", ("GET", "/health"), None)
    assert _survives(wrong_arity)


def test_repeat_calls_do_not_stack_filters():
    """setup_logging is documented as idempotent and both filters must honour it.

    uvicorn.access is a singleton that survives our reconfiguration, so a
    filter appended per call accumulates for the life of the process.
    """
    setup_logging(log_dir=None)
    setup_logging(log_dir=None)

    access = logging.getLogger(ACCESS_LOGGER)
    health = [f for f in access.filters if isinstance(f, HealthCheckAccessFilter)]
    assert len(health) == 1


def test_secret_redaction_still_reaches_the_only_handler():
    """Stdout-only must not cost the executor its credential filter.

    _install_secret_redaction walks root's handlers, so a change to how those
    handlers are built can silently leave it filtering nothing.
    """
    setup_logging(log_dir=None)

    for handler in logging.getLogger().handlers:
        assert any(isinstance(f, SecretRedactingFilter) for f in handler.filters), (
            f"{handler!r} carries no SecretRedactingFilter; a registry auth "
            f"token in a pull error would print verbatim")
