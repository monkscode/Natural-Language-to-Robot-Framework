import pytest
from unittest.mock import MagicMock, patch
from src.backend.services.docker_service import get_docker_client, build_image, run_test_in_container, rebuild_image, get_docker_status
import docker

def test_get_docker_client_success():
    with patch('docker.from_env') as mock_from_env:
        mock_client = MagicMock()
        mock_from_env.return_value = mock_client
        client = get_docker_client()
        assert client == mock_client
        mock_client.ping.assert_called_once()

def test_get_docker_client_failure():
    with patch('docker.from_env') as mock_from_env:
        mock_from_env.side_effect = docker.errors.DockerException("Test error")
        with pytest.raises(ConnectionError):
            get_docker_client()

@patch('src.backend.services.docker_service.get_docker_client')
def test_rebuild_image(mock_get_docker_client):
    mock_client = MagicMock()
    mock_get_docker_client.return_value = mock_client

    result = rebuild_image(mock_client)

    mock_client.images.remove.assert_called_once()
    mock_client.images.build.assert_called_once()
    assert result['status'] == 'success'

@patch('src.backend.services.docker_service.get_docker_client')
def test_get_docker_status(mock_get_docker_client):
    mock_client = MagicMock()
    mock_get_docker_client.return_value = mock_client

    mock_image = MagicMock()
    mock_image.id = "123"
    mock_image.attrs = {'Created': '2023-01-01', 'Size': 123456}
    mock_client.images.get.return_value = mock_image

    status = get_docker_status(mock_client)

    assert status['docker_available'] is True
    assert status['image']['exists'] is True

def test_build_image_exists():
    mock_client = MagicMock()
    mock_client.images.get.return_value = True

    events = list(build_image(mock_client))

    assert len(events) == 1
    assert events[0]['message'] == 'Using existing container image for test execution...'

def test_run_test_in_container_success():
    mock_client = MagicMock()
    mock_client.containers.run.return_value = b'Success logs'

    result = run_test_in_container(mock_client, 'run123', 'test.robot')

    assert result['status'] == 'complete'
    assert 'All tests passed' in result['message']
    assert result['result']['logs'] == 'Success logs'
