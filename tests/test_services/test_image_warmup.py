"""The runner image should download while the user is busy, not while they wait.

Measured cold (2026-08-17): the pull is ~0.5 GB and takes 99s, and it starts the
instant the user clicks Run Test — the worst possible moment. Everything before
that click (browser-service reaching healthy, signup, typing a query, the 23s
generation) is dead time the download could have used instead.

Warm-up is deliberately PULL-ONLY: a local image build is minutes of CPU, and
silently starting one at every container boot is a surprise. The build still
happens on demand, exactly as before.
"""
import threading
from unittest.mock import MagicMock, patch

import docker
import pytest

from src.backend.services import docker_service as ds


def _client_without_image():
    client = MagicMock()
    client.images.get.side_effect = docker.errors.ImageNotFound("absent")
    return client


def _client_with_image():
    client = MagicMock()
    client.images.get.return_value = MagicMock()
    return client


class TestWarmImageCache:
    def test_no_op_when_the_image_is_already_present(self):
        client = _client_with_image()
        assert ds.warm_image_cache(client) is False
        client.api.pull.assert_not_called()

    def test_pulls_and_tags_when_absent(self):
        client = _client_without_image()
        # images.get raises for IMAGE_TAG, then must succeed for REMOTE_IMAGE so
        # the pulled image can be tagged.
        pulled = MagicMock()
        client.images.get.side_effect = [docker.errors.ImageNotFound("absent"), pulled]
        client.api.pull.return_value = [{"status": "Downloading"}, {"status": "Pull complete"}]

        # PREFER_REMOTE_IMAGE defaults to false in code (compose sets it true), so
        # the pulling paths must say so explicitly rather than passing by accident.
        with patch.object(ds, "PREFER_REMOTE_IMAGE", True):
            assert ds.warm_image_cache(client) is True
        client.api.pull.assert_called_once()
        assert client.api.pull.call_args[0][0] == ds.REMOTE_IMAGE
        pulled.tag.assert_called_once_with(ds.IMAGE_TAG)

    def test_never_builds_when_the_pull_fails(self):
        # Warm-up is pull-only: a failed pull must not kick off a local build,
        # which is minutes of CPU nobody asked for at container boot.
        client = _client_without_image()
        client.api.pull.side_effect = docker.errors.APIError("no such tag")
        with patch.object(ds, "PREFER_REMOTE_IMAGE", True):
            assert ds.warm_image_cache(client) is False
        client.build.assert_not_called()
        client.images.build.assert_not_called()

    def test_a_pull_error_event_does_not_raise(self):
        # The daemon reports failures as an 'error' key in the stream, not by
        # raising — a warm-up that propagates it would kill the startup thread.
        client = _client_without_image()
        client.api.pull.return_value = [{"error": "manifest unknown"}]
        with patch.object(ds, "PREFER_REMOTE_IMAGE", True):
            assert ds.warm_image_cache(client) is False

    def test_docker_being_unavailable_is_swallowed(self):
        client = MagicMock()
        client.images.get.side_effect = docker.errors.DockerException("socket gone")
        with patch.object(ds, "PREFER_REMOTE_IMAGE", True):
            assert ds.warm_image_cache(client) is False

    def test_skipped_when_remote_pull_is_disabled(self):
        client = _client_without_image()
        with patch.object(ds, "PREFER_REMOTE_IMAGE", False):
            assert ds.warm_image_cache(client) is False
        client.api.pull.assert_not_called()


class TestSerialisation:
    def test_warmup_and_ensure_image_do_not_pull_concurrently(self):
        """Without a shared lock a warm-up in flight and a Run Test click would
        each start their own pull of the same 0.5 GB image."""
        started = threading.Event()
        release = threading.Event()
        overlapped = []

        def slow_pull(*_a, **_kw):
            started.set()
            release.wait(timeout=5)
            return [{"status": "done"}]

        client = MagicMock()
        client.images.get.side_effect = (
            lambda *_a, **_kw: (_ for _ in ()).throw(docker.errors.ImageNotFound("absent")))
        client.api.pull.side_effect = slow_pull

        def warm():
            with patch.object(ds, "PREFER_REMOTE_IMAGE", True):
                ds.warm_image_cache(client)

        t = threading.Thread(target=warm, daemon=True)
        t.start()
        assert started.wait(timeout=5), "warm-up never began pulling"

        # While the warm-up holds the lock, a second provisioning attempt must
        # block rather than start its own pull.
        acquired = ds.IMAGE_PROVISION_LOCK.acquire(timeout=0.2)
        if acquired:
            ds.IMAGE_PROVISION_LOCK.release()
            overlapped.append(True)
        release.set()
        t.join(timeout=5)

        assert not overlapped, "the provisioning lock was free during an active pull"

    def test_rebuild_waits_for_a_pull_in_flight(self):
        """rebuild_image is the third writer of IMAGE_TAG and must queue behind
        the other two.

        Unguarded it overlaps a warm-up: rebuild removes IMAGE_TAG and builds it
        from source, then the pull that was already running finishes and tags the
        registry image as IMAGE_TAG — silently discarding the admin's rebuild.
        """
        started = threading.Event()
        release = threading.Event()
        rebuild_touched_the_image = threading.Event()

        def slow_pull(*_a, **_kw):
            started.set()
            release.wait(timeout=5)
            return [{"status": "done"}]

        client = MagicMock()
        client.images.get.side_effect = (
            lambda *_a, **_kw: (_ for _ in ()).throw(docker.errors.ImageNotFound("absent")))
        client.api.pull.side_effect = slow_pull
        client.images.remove.side_effect = lambda *_a, **_kw: rebuild_touched_the_image.set()

        def warm():
            with patch.object(ds, "PREFER_REMOTE_IMAGE", True):
                ds.warm_image_cache(client)

        t = threading.Thread(target=warm, daemon=True)
        t.start()
        assert started.wait(timeout=5), "warm-up never began pulling"

        r = threading.Thread(target=lambda: ds.rebuild_image(client), daemon=True)
        r.start()
        assert not rebuild_touched_the_image.wait(timeout=0.3), (
            "rebuild removed the image while a pull was still in flight; the pull "
            "will re-tag over the rebuilt image when it completes")

        release.set()
        t.join(timeout=5)
        r.join(timeout=5)
        assert rebuild_touched_the_image.is_set(), "rebuild never ran after the lock freed"


class TestStartupHook:
    def test_startup_thread_is_a_daemon_and_does_not_block(self):
        """A boot-time network fetch must never hold up the executor coming up."""
        from src.backend.runner_exec import app as runner_app

        spawned = {}

        class FakeThread:
            def __init__(self, target=None, daemon=None, name=None):
                spawned["target"] = target
                spawned["daemon"] = daemon
            def start(self):
                spawned["started"] = True

        with patch.object(runner_app.threading, "Thread", FakeThread):
            runner_app._start_image_warmup()

        assert spawned.get("started") is True
        assert spawned.get("daemon") is True, "warm-up thread must not block shutdown"

    def test_warmup_can_be_disabled(self):
        from src.backend.runner_exec import app as runner_app

        spawned = {}

        class FakeThread:
            def __init__(self, **kw):
                spawned["made"] = True
            def start(self):
                spawned["started"] = True

        with patch.object(runner_app, "WARMUP_ENABLED", False), \
             patch.object(runner_app.threading, "Thread", FakeThread):
            runner_app._start_image_warmup()

        assert not spawned, "RUNNER_IMAGE_WARMUP=false must skip the warm-up entirely"


@pytest.fixture(autouse=True)
def _reset_lock():
    yield
    if ds.IMAGE_PROVISION_LOCK.locked():  # pragma: no cover — safety net
        ds.IMAGE_PROVISION_LOCK.release()
