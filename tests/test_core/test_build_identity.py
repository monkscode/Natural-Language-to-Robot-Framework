"""Guards for the build identity a running container reports.

Purpose: `-develop` and `-latest` are MOVING tags. Without a commit stamped into
         the image, neither a user nor a maintainer can tell which build is
         running — `docker compose images` shows only the tag it was pulled
         under. That is what made "the user is not on the latest code"
         undiagnosable, so these tests keep the stamp wired end to end:
         Dockerfile ARG -> image LABEL + env -> /health.

Tests:
  - build_info() defaults honestly instead of inventing a version
  - build_info() reports NLRF_BUILD_SHA when the image sets it
  - /health, /api/health and the executor's /health all carry it
  - every runnable Dockerfile declares the ARG and stamps it
  - build-images.yml actually passes the commit to each of those builds
"""

import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The images a user runs. base-browser is a build-time base only — it is never
# started as a service, so it carries no identity of its own.
RUNNABLE_DOCKERFILES = [
    "Dockerfile.fastapi",
    "Dockerfile.browser-service",
    "Dockerfile.test-runner",
    "Dockerfile.frontend",
]


class TestBuildInfo:
    def test_defaults_to_unknown_when_unstamped(self):
        """A local `run.sh` checkout has no stamp. Say so; do not guess."""
        from src.backend.core.build_info import build_info
        with patch.dict(os.environ, {}, clear=True):
            assert build_info() == {"commit": "unknown"}

    def test_reports_the_stamped_commit(self):
        from src.backend.core.build_info import build_info
        with patch.dict(os.environ, {"NLRF_BUILD_SHA": "f96c5c6"}, clear=True):
            assert build_info() == {"commit": "f96c5c6"}

    def test_blank_stamp_is_treated_as_absent(self):
        """An unset --build-arg reaches the image as an empty string."""
        from src.backend.core.build_info import build_info
        with patch.dict(os.environ, {"NLRF_BUILD_SHA": "   "}, clear=True):
            assert build_info() == {"commit": "unknown"}


class TestHealthCarriesTheBuild:
    @pytest.fixture(scope="class")
    def client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.backend.api.health import health_check, api_health_check

        app = FastAPI()
        app.get("/health")(health_check)
        app.get("/api/health")(api_health_check)
        with TestClient(app) as c:
            yield c

    @pytest.mark.parametrize("path", ["/health", "/api/health"])
    def test_build_is_reported(self, client, path):
        with patch("src.backend.api.health.get_active_workflow_count", return_value=0), \
             patch.dict(os.environ, {"NLRF_BUILD_SHA": "abc1234"}, clear=False):
            resp = client.get(path)
        assert resp.status_code == 200
        assert resp.json()["build"] == {"commit": "abc1234"}

    def test_executor_health_reports_it_too(self):
        """The executor is a separate container and can be a different build."""
        from fastapi.testclient import TestClient
        from src.backend.runner_exec import app as runner_app

        with patch.object(runner_app, "get_docker_client"), \
             patch.object(runner_app, "_start_image_warmup"), \
             patch.dict(os.environ, {"NLRF_BUILD_SHA": "abc1234"}, clear=False):
            with TestClient(runner_app.app) as c:
                body = c.get("/health").json()
        assert body["build"] == {"commit": "abc1234"}


class TestTheStampIsWiredIntoTheBuild:
    """Unit-testing build_info() proves nothing if no image ever sets the var."""

    @pytest.mark.parametrize("dockerfile", RUNNABLE_DOCKERFILES)
    def test_dockerfile_accepts_and_stamps_the_commit(self, dockerfile):
        text = (REPO_ROOT / dockerfile).read_text(encoding="utf-8")
        assert re.search(r"^ARG\s+GIT_SHA", text, re.M), \
            f"{dockerfile} does not accept a GIT_SHA build-arg"
        assert re.search(r"^LABEL\s+org\.opencontainers\.image\.revision=", text, re.M), \
            f"{dockerfile} does not label the image with its revision"

    @pytest.mark.parametrize("dockerfile", ["Dockerfile.fastapi"])
    def test_backend_image_exports_it_to_the_process(self, dockerfile):
        """LABEL is for `docker inspect`; /health needs it in the environment."""
        text = (REPO_ROOT / dockerfile).read_text(encoding="utf-8")
        assert re.search(r"^ENV\s+NLRF_BUILD_SHA=", text, re.M), \
            f"{dockerfile} does not export NLRF_BUILD_SHA"

    @pytest.mark.parametrize("dockerfile", RUNNABLE_DOCKERFILES)
    def test_ci_passes_the_commit_to_every_build(self, dockerfile):
        """A Dockerfile that accepts GIT_SHA but is never given one is inert."""
        workflow = (REPO_ROOT / ".github" / "workflows" / "build-images.yml").read_text(
            encoding="utf-8")
        # The build step names its Dockerfile, then its build-args. Take the
        # slice from this file's step to the next one and require the arg there.
        marker = f"file: ./{dockerfile}"
        assert marker in workflow, f"{dockerfile} has no build step in build-images.yml"
        start = workflow.index(marker)
        nxt = workflow.find("file: ./Dockerfile", start + len(marker))
        section = workflow[start:] if nxt == -1 else workflow[start:nxt]
        assert "GIT_SHA=${{ github.sha }}" in section, \
            f"build-images.yml does not pass GIT_SHA to {dockerfile}"
