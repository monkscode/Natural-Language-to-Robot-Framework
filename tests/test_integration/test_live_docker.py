"""
Live integration tests for Docker service (Tier 2).

Purpose: Verify Docker daemon connectivity and image management when Docker
         Desktop is running.  These tests do NOT execute Robot Framework tests
         inside containers (that would be a full end-to-end run).

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
