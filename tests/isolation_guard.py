"""Whole-session isolation of the suite from the real artifact staging directory.

The defect (measured 2026-09-14): artifact_store.STAGING_ROOT is a module
constant naming the repo's robot_tests/, and nothing redirected it. A test
whose re-run reached the execution stream left a real run directory there on
every run, and whenever run.sh's runner-exec was listening on 127.0.0.1:4998
the suite executed it in a real container.

redirect_staging() points every copy of that root at a per-session temp
directory; tests/conftest.py calls it from a session fixture.

Referenced by: tests/conftest.py, tests/test_infra/test_staging_isolation_guard.py.
Depends on: nothing at import time; redirect_staging() imports
src.backend.core.artifact_store and src.backend.services.docker_service.
"""

import sys
from pathlib import Path

# <repo-root>/robot_tests — the directory artifact_store.STAGING_ROOT names in
# production. Computed here rather than imported, so importing this module
# never imports src.backend (see the Postgres guard in tests/conftest.py for
# why the first src.backend import must come after its redirect).
REPO_STAGING_ROOT = Path(__file__).resolve().parent.parent / "robot_tests"


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
