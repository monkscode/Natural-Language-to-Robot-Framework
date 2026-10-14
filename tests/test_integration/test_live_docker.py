"""
Live integration tests for Docker service (Tier 2).

Purpose: Verify Docker daemon connectivity and image management when Docker
         Desktop is running.  TestRunMountIsolation is the exception: it runs
         real Robot containers, because the property it guards — which host
         directory a run can reach — only exists once Docker resolves the mount.

Requires:
  - Docker Desktop running on the host machine

Run with:
  pytest tests/test_integration/test_live_docker.py -m integration -v

Skip in CI (no Docker daemon) via:
  pytest -m "not integration"
"""

import pytest

pytestmark = pytest.mark.integration


class TestDockerConnectivity:
    """Verify Docker daemon is reachable and client can be constructed."""

    def test_docker_client_can_be_created(self):
        """get_docker_client() returns a live Docker client without error."""
        from src.backend.services.docker_service import get_docker_client
        client = get_docker_client()
        assert client is not None

    def test_docker_ping_succeeds(self):
        """Docker daemon responds to a ping."""
        from src.backend.services.docker_service import get_docker_client
        client = get_docker_client()
        result = client.ping()
        assert result is True

    def test_docker_version_accessible(self):
        """Docker version info is available — daemon is fully functional."""
        from src.backend.services.docker_service import get_docker_client
        client = get_docker_client()
        version = client.version()
        assert "Version" in version or "ApiVersion" in version


class TestDockerImageManagement:
    """Verify image existence checks and build_image generator contract."""

    def test_build_image_yields_status_dict(self):
        """build_image() yields at least one status dict with 'status' key."""
        from src.backend.services.docker_service import get_docker_client, build_image
        client = get_docker_client()
        # Consume the first event from the generator
        first_event = next(build_image(client))
        assert isinstance(first_event, dict)
        assert "status" in first_event

    def test_build_image_generator_is_exhaustible(self):
        """build_image() generator can be fully consumed without exception."""
        from src.backend.services.docker_service import get_docker_client, build_image
        client = get_docker_client()
        events = list(build_image(client))
        assert len(events) >= 1
        # Last event must also be a dict
        assert isinstance(events[-1], dict)

    def test_image_tag_constant_is_defined(self):
        """IMAGE_TAG constant is a non-empty string."""
        from src.backend.services.docker_service import IMAGE_TAG
        assert isinstance(IMAGE_TAG, str)
        assert len(IMAGE_TAG) > 0

    def test_remote_image_constant_is_defined(self):
        """REMOTE_IMAGE constant is a non-empty string."""
        from src.backend.services.docker_service import REMOTE_IMAGE
        assert isinstance(REMOTE_IMAGE, str)
        assert len(REMOTE_IMAGE) > 0


class TestDockerImageInspection:
    """Inspect the built image after build_image() completes."""

    def test_image_exists_after_build(self):
        """After fully consuming build_image(), the image is available locally."""
        import docker
        from src.backend.services.docker_service import get_docker_client, build_image, IMAGE_TAG
        client = get_docker_client()
        # Ensure image is present (consumes build_image if not already built)
        try:
            client.images.get(IMAGE_TAG)
            image_present = True
        except docker.errors.ImageNotFound:
            # Build it now
            list(build_image(client))
            try:
                client.images.get(IMAGE_TAG)
                image_present = True
            except docker.errors.ImageNotFound:
                image_present = False

        assert image_present, f"Image '{IMAGE_TAG}' not found after build"

    def test_image_has_tags(self):
        """The Robot test runner image has at least one tag."""
        import docker
        from src.backend.services.docker_service import get_docker_client, IMAGE_TAG
        client = get_docker_client()
        try:
            image = client.images.get(IMAGE_TAG)
            assert len(image.tags) >= 1
        except docker.errors.ImageNotFound:
            pytest.skip(f"Image '{IMAGE_TAG}' not yet built — run build_image first")


# Runs inside the container as run A. Every check targets run B's directory,
# which sits next to run A's on the host; the final Create File proves run A can
# still write its own output, and runs only if every check before it passed.
_ISOLATION_PROBE = """*** Settings ***
Library    OperatingSystem

*** Test Cases ***
Run A Cannot Reach Run B
    ${{entries}}=    List Directory    /app/robot_tests
    Should Not Contain    ${{entries}}    {other}
    Directory Should Not Exist    /app/robot_tests/{other}
    File Should Not Exist    /app/robot_tests/{other}/log.html
    File Should Not Exist    ${{CURDIR}}/../{other}/log.html
    Create File    /app/robot_tests/{own}/written_by_container.txt    ok
"""


class TestRunMountIsolation:
    """The mount is the tenant boundary, asserted on behaviour, not config.

    The unit tests pin the volumes dict; this proves what a generated test can
    actually reach once Docker resolves that mount. It points the service at
    tmp_path — never the real robot_tests staging root, and never whatever
    nlrf-fastapi's mount inspect would resolve to if the compose stack is up.
    Verified to fail against the root mount it replaced.
    """

    @pytest.fixture
    def docker_client(self):
        import docker
        from src.backend.services.docker_service import get_docker_client, IMAGE_TAG
        try:
            client = get_docker_client()
            client.images.get(IMAGE_TAG)
        except docker.errors.ImageNotFound:
            pytest.skip(f"Image '{IMAGE_TAG}' not built — run build_image first")
        # get_docker_client re-raises a failed connection as the builtin
        # ConnectionError, which is not a DockerException subclass.
        except (ConnectionError, docker.errors.DockerException) as e:
            pytest.skip(f"Docker unavailable: {e}")
        return client

    def test_a_run_cannot_reach_another_runs_directory(self, docker_client, tmp_path):
        """run B's log.html records typed passwords; run A must not see it."""
        from unittest.mock import patch
        from uuid import uuid4
        from src.backend.core.artifact_store import make_shared_dir
        from src.backend.services import docker_service

        own, other = f"iso-a-{uuid4().hex[:12]}", f"iso-b-{uuid4().hex[:12]}"
        for run in (own, other):
            make_shared_dir(tmp_path / run)
        secret = tmp_path / other / "log.html"
        secret.write_text("typed password: hunter2", encoding="utf-8")
        (tmp_path / own / "test.robot").write_text(
            _ISOLATION_PROBE.format(own=own, other=other), encoding="utf-8")

        with patch.object(docker_service, "ROBOT_TESTS_DIR", str(tmp_path)), \
             patch.object(docker_service, "resolve_host_robot_tests_dir",
                          return_value=str(tmp_path)):
            result = docker_service.run_test_in_container(docker_client, own, "test.robot")

        assert result["test_status"] == "passed", result.get("message")
        assert (tmp_path / own / "output.xml").exists()
        assert (tmp_path / own / "written_by_container.txt").exists()
        assert secret.read_text(encoding="utf-8") == "typed password: hunter2"
