"""Regression tests for the pytest-session artifact-staging isolation.

The defect (measured 2026-09-14): the artifact store's staging root is a module
constant pointing at the repo's robot_tests/, and nothing in the suite
redirected it. A test whose re-run reached the execution stream left a real
run directory there on every run — 640 had accumulated — and whenever
run.sh's runner-exec was listening on 127.0.0.1:4998, the suite executed that
directory in a real container.

Referenced by: none (leaf test module).
Depends on: tests/isolation_guard.py, tests/conftest.py (_isolated_staging_root).
"""

from pathlib import Path

import pytest

from tests import isolation_guard
from tests.isolation_guard import REPO_STAGING_ROOT


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
