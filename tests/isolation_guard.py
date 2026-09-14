"""Whole-session isolation of the suite from the real artifact staging
directory, the real logs directory, and the runner-exec service.

The defects (measured 2026-09-14):
- artifact_store.STAGING_ROOT is a module constant naming the repo's
  robot_tests/, and nothing redirected it. A test whose re-run reached the
  execution stream left a real run directory there on every run, and whenever
  run.sh's runner-exec was listening on 127.0.0.1:4998 the suite executed it
  in a real container.
- A full local gate wrote application.log, a temp_metrics/<uuid>.json and both
  crewai logs into the repo's logs/, which the running API container writes
  too. Each gate added two fake slowapi "ratelimit" lines to application.log,
  which the 429 count matches, and a test process rotated the live file.

Two halves:
- redirect_staging() and redirect_logs() point every copy of those paths at
  per-session temp directories; application.log follows LOG_DIR, which
  tests/conftest.py sets at import. tests/conftest.py calls isolate_session()
  from a session fixture.
- StagingGuard is a sys.addaudithook hook, installed when this module is first
  imported, so it sees writes whichever module computed the path and connects
  whichever HTTP client made them. It REFUSES — and records — any write or
  delete under either real root and any connect to the runner's port: refusing
  keeps a live runner from executing anything and a test from deleting a real
  run or rotating a live log, and recording lets pytest_sessionfinish fail a
  run whose tests all passed. The refusals are OSErrors, so the code under
  test sees what it would see if the directory were unwritable or the runner
  were down.

Not covered:
- Writes made INSIDE a container. A test that started a real one would mount
  the host path docker_service resolves from nlrf-fastapi's mounts, not the
  redirected root. No test starts one today.
- Writes through a handle opened before this module was imported: the hook
  sees an open, not the writes after it. A `-p` plugin that imports
  src.backend.main ahead of tests/conftest.py opens the real application.log
  that way, before LOG_DIR is set.
- Other processes. The live API and browser-service containers keep writing
  the real logs/, so its sizes and mtimes say nothing about the suite.

Stdlib and pytest only at import: a `-p` plugin can import this module before
tests/conftest.py has redirected DATABASE_URL, so it must not import src.backend.

Referenced by: tests/conftest.py, tests/test_infra/test_staging_isolation_guard.py,
tests/test_infra/test_logs_isolation.py.
Depends on: nothing at import time; redirect_staging() imports
src.backend.core.artifact_store and src.backend.services.docker_service,
redirect_logs() src.backend.core.temp_metrics_storage and
src.backend.crew_ai.callbacks/crew, and isolate_session() src.backend.core.config.
"""

import errno
import ipaddress
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

import pytest

# <repo-root>/robot_tests — the directory artifact_store.STAGING_ROOT names in
# production. Computed here rather than imported, for the reason above.
REPO_STAGING_ROOT = Path(__file__).resolve().parent.parent / "robot_tests"

# <repo-root>/logs — where application.log, both crewai logs and the temp
# metrics store land unless redirected. All four are relative to the working
# directory, which is the repo root for the suite.
REPO_LOGS_ROOT = REPO_STAGING_ROOT.parent / "logs"

# core/config.py's default. Used until isolate_session() reads settings.
DEFAULT_RUNNER_URL = "http://127.0.0.1:4998"

_OPEN_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
# Events whose first argument is the path written to or deleted.
_PATH_EVENTS = frozenset({"os.mkdir", "os.rmdir", "os.remove", "os.chmod", "os.truncate", "shutil.rmtree"})
# Events that write their second argument and only read their first.
_DESTINATION_EVENTS = frozenset({"shutil.copyfile", "shutil.copytree", "os.link", "os.symlink"})
# Events that remove their first argument and create their second.
_MOVE_EVENTS = frozenset({"os.rename", "shutil.move"})
_WATCHED_EVENTS = _PATH_EVENTS | _DESTINATION_EVENTS | _MOVE_EVENTS | {"open", "socket.connect"}
_WELL_KNOWN_PORTS = frozenset({80, 443})
_LONG_PATH_PREFIX = "\\\\?\\"
_REFUSED = "refused by the test isolation guard (tests/isolation_guard.py)"


def redirect_staging(staging: Path) -> None:
    """Point every copy of the artifact staging root at *staging*.

    Rebinding artifact_store.STAGING_ROOT alone is not enough, because three
    other names hold the old value:
      - artifact_store._store, a store already built from the old root;
      - docker_service.ROBOT_TESTS_DIR and HOST_ROBOT_TESTS_DIR, copied when
        docker_service is imported. It is imported here rather than patched
        only when present, because a later import would read
        HOST_ROBOT_TESTS_DIR from the environment, not from the root;
      - bench.run_bench.STAGING_ROOT, copied at import. Only when already
        imported: a later import copies the rebound constant.
    """
    from src.backend.core import artifact_store
    from src.backend.services import docker_service

    artifact_store.STAGING_ROOT = staging
    artifact_store._store = None
    docker_service.ROBOT_TESTS_DIR = str(staging)
    docker_service.HOST_ROBOT_TESTS_DIR = str(staging)
    run_bench = sys.modules.get("bench.run_bench")
    if run_bench is not None:
        run_bench.STAGING_ROOT = staging


def redirect_logs(logs: Path) -> None:
    """Point every log path the suite can reach, other than application.log,
    into *logs*.

    application.log follows LOG_DIR (src/backend/main.py), which
    tests/conftest.py sets at import: main.py opens it at import time, before
    any fixture could run. LOG_DIR moves nothing else, so three more names are
    rebound here:
      - temp_metrics_storage._temp_storage, the singleton the browser tool
        writes through and app startup cleans — the cleanup deletes files
        older than 24 h, so a boot test could delete real ones;
      - callbacks.CREWAI_STEP_LOG_FILE and crew.CREWAI_LOG_FILE, both read at
        call time.
    Importing crew costs nothing extra: redirect_staging's docker_service
    import already loads it, through src/backend/services/__init__.py.
    """
    from src.backend.core import temp_metrics_storage
    from src.backend.crew_ai import callbacks, crew

    temp_metrics_storage._temp_storage = temp_metrics_storage.TempMetricsStorage(str(logs / "temp_metrics"))
    callbacks.CREWAI_STEP_LOG_FILE = str(logs / "crewai_steps.log")
    crew.CREWAI_LOG_FILE = str(logs / "crewai.log.txt")


class StagingGuard:
    """Refuses and records what a test must never do: write under a real
    shared directory — the staging root, the logs dir — or connect to
    runner-exec."""

    def __init__(self, roots: Iterable[Path], runner_url: str = DEFAULT_RUNNER_URL):
        self.roots = tuple(roots)
        # (normalised root, the same with a trailing separator), per root.
        self._roots = tuple((norm, norm + os.sep) for norm in
                            (os.path.normcase(os.path.abspath(root)) for root in self.roots))
        # (test id, audit event, refused path or host:port), in refusal order.
        self.violations: list[tuple[str, str, str]] = []
        self.set_runner_url(runner_url)

    def set_runner_url(self, url: str) -> None:
        parts = urlsplit(url)
        self.runner_port = parts.port or (443 if parts.scheme == "https" else 80)
        # A runner on 80 or 443 shares its port with everything else a test
        # might reach, so there only a loopback connect can be the runner.
        self._runner_on_loopback_only = self.runner_port in _WELL_KNOWN_PORTS

    def hook(self, event: str, args: tuple) -> None:
        if event not in _WATCHED_EVENTS:
            return
        try:
            refusal = self._refusal(event, args)
        except Exception:  # noqa: BLE001 — this hook sees every open in the process; a parse failure must never break one
            return
        if refusal is None:
            return
        self.violations.append(
            (os.environ.get("PYTEST_CURRENT_TEST", "<outside a test>"), event, refusal.filename))
        raise refusal

    def _refusal(self, event: str, args: tuple) -> OSError | None:
        if event == "socket.connect":
            return self._connect_refusal(args[1])
        if event == "open":
            path, mode, flags = args
            writing = (any(c in mode for c in "wax+") if isinstance(mode, str)
                       else isinstance(flags, int) and bool(flags & _OPEN_WRITE_FLAGS))
            target = path if writing else None
        elif event in _PATH_EVENTS:
            target = args[0]
        elif event in _DESTINATION_EVENTS:
            target = args[1]
        else:  # _MOVE_EVENTS
            target = args[0] if self._under_root(args[0]) else args[1]
        if target is None or not self._under_root(target):
            return None
        return PermissionError(errno.EACCES, _REFUSED, os.fsdecode(target))

    def _under_root(self, path) -> bool:
        # A file descriptor (os.fchmod, open(fd)) makes fsdecode raise
        # TypeError, which hook() treats as "not ours": an fd names no path.
        text = os.fsdecode(path)
        if text.startswith(_LONG_PATH_PREFIX):
            text = text[len(_LONG_PATH_PREFIX):]
        normalised = os.path.normcase(os.path.abspath(text))
        return any(normalised == root or normalised.startswith(prefix) for root, prefix in self._roots)

    def _connect_refusal(self, address) -> OSError | None:
        if not (isinstance(address, tuple) and len(address) >= 2 and address[1] == self.runner_port):
            return None
        host = address[0]
        if self._runner_on_loopback_only and not _is_loopback(host):
            return None
        return ConnectionRefusedError(errno.ECONNREFUSED, _REFUSED, f"{host}:{address[1]}")

    def report_and_fail(self, session) -> None:
        """List every refusal and fail a session that would otherwise have
        passed. A worse status (failed, interrupted, internal error) is kept."""
        if not self.violations:
            return
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        roots = ", ".join(str(root) for root in self.roots)
        lines = [
            f"{len(self.violations)} operation(s) reached outside the test session and were refused.",
            f"Tests must not write under {roots}, or connect to runner-exec "
            f"(port {self.runner_port}); see tests/isolation_guard.py.",
        ]
        lines += [f"  {test}: {event} {target}" for test, event, target in self.violations]
        if reporter is None:
            for line in lines:
                print(line, file=sys.stderr)
        else:
            reporter.write_sep("=", "test isolation guard", red=True)
            for line in lines:
                reporter.write_line(line, red=True)
        if session.exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


def _is_loopback(host: str) -> bool:
    """socket.connect always receives a resolved, numeric address. An
    IPv4-mapped one (::ffff:127.0.0.1) counts as loopback on both Pythons in
    use (3.13.7 locally, 3.12.13 in the image)."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


GUARD = StagingGuard((REPO_STAGING_ROOT, REPO_LOGS_ROOT))
sys.addaudithook(GUARD.hook)


def isolate_session(staging: Path, logs: Path) -> None:
    """Redirect the staging root and the log paths, and aim the guard at the
    runner the settings name — .env can move it off the default port, and
    that is the one the client would call."""
    from src.backend.core.config import settings

    redirect_staging(staging)
    redirect_logs(logs)
    GUARD.set_runner_url(settings.RUNNER_EXEC_URL)


def pytest_sessionfinish(session, exitstatus):
    GUARD.report_and_fail(session)
