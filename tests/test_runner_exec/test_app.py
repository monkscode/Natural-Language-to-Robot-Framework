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
