"""Tests for the runner-exec executor app: request validation (path-traversal
rejection) and that each handler proxies to the docker_service/dryrun_service
primitives with validated args.

Referenced by: (pytest).
Depends on: src/backend/runner_exec/app.py.
"""
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from src.backend.runner_exec.app import app

client = TestClient(app, raise_server_exceptions=False)


def test_execute_rejects_path_traversal():
    r = client.post("/execute", json={"run_id": "../etc", "test_filename": "test.robot"})
    assert r.status_code == 400


def test_execute_calls_run_test_in_container_with_validated_args():
    fake_client = MagicMock()
    with patch("src.backend.runner_exec.app.get_docker_client", return_value=fake_client), \
         patch("src.backend.runner_exec.app.run_test_in_container",
               return_value={"status": "complete", "test_status": "passed"}) as run_mock:
        r = client.post("/execute", json={"run_id": "abc123", "test_filename": "test.robot"})
    assert r.status_code == 200
    assert r.json()["test_status"] == "passed"
    run_mock.assert_called_once_with(fake_client, "abc123", "test.robot")


def test_dryrun_rejects_bad_run_id():
    r = client.post("/dryrun", json={"run_id": "a/b", "code": "x"})
    assert r.status_code == 400


def test_docker_status_proxies():
    with patch("src.backend.runner_exec.app.get_docker_client", return_value=MagicMock()), \
         patch("src.backend.runner_exec.app.get_docker_status",
               return_value={"status": "success", "docker_available": True}):
        r = client.get("/docker-status")
    assert r.status_code == 200 and r.json()["docker_available"] is True


def test_health_reports_socket_reachable():
    fake_client = MagicMock()
    with patch("src.backend.runner_exec.app.get_docker_client", return_value=fake_client):
        r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


_MARK = "pw_marker_131"


def _post_execute(env):
    return client.post("/execute", json={"run_id": "abc123", "test_filename": "test.robot", "env": env})


def test_execute_passes_a_valid_secret_env_through():
    with patch("src.backend.runner_exec.app.get_docker_client", return_value=MagicMock()), \
         patch("src.backend.runner_exec.app.run_test_in_container",
               return_value={"status": "complete"}) as run_mock:
        r = _post_execute({"NLRF_SECRET_PW": _MARK})
    assert r.status_code == 200
    assert run_mock.call_args.kwargs["env"] == {"NLRF_SECRET_PW": _MARK}


def test_execute_refuses_a_key_outside_the_secret_prefix_and_never_echoes_a_value():
    with patch("src.backend.runner_exec.app.run_test_in_container") as run_mock:
        r = _post_execute({"LD_PRELOAD": _MARK})
    assert r.status_code == 400
    assert "LD_PRELOAD" in r.text and _MARK not in r.text
    run_mock.assert_not_called()


def test_execute_refuses_a_non_string_value_and_never_echoes_it():
    with patch("src.backend.runner_exec.app.run_test_in_container") as run_mock:
        r = _post_execute({"NLRF_SECRET_PW": {"x": _MARK}})
    assert r.status_code == 400
    assert "NLRF_SECRET_PW" in r.text and _MARK not in r.text
    run_mock.assert_not_called()


def test_execute_refuses_a_non_object_env_without_echoing_it():
    r = _post_execute(_MARK)
    assert r.status_code == 400 and _MARK not in r.text


def test_execute_masks_a_value_in_the_500_detail():
    with patch("src.backend.runner_exec.app.get_docker_client", return_value=MagicMock()), \
         patch("src.backend.runner_exec.app.run_test_in_container",
               side_effect=RuntimeError(f"boom {_MARK}")):
        r = _post_execute({"NLRF_SECRET_PW": _MARK})
    assert r.status_code == 500
    assert _MARK not in r.text and "boom ***" in r.text
