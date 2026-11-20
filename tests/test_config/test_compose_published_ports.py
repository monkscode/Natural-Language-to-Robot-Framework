"""browser-service has no login, so compose must not publish its port.

On Docker Desktop a test container reaches host-loopback ports through
host.docker.internal (measured 2026-10-13 and 2026-11-20), so a published
:4999 let any pasted test list and read other users' discovery tasks.
fastapi reaches the service by name over nlrf-network.
"""

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


def _services() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def test_browser_service_publishes_no_host_port():
    assert "ports" not in _services()["browser-service"]


def test_fastapi_still_publishes_its_port():
    # Control: the reader sees a service that does publish one.
    assert "127.0.0.1:5000:5000" in _services()["fastapi"]["ports"]


def test_fastapi_reaches_browser_service_by_name():
    assert "BROWSER_USE_SERVICE_URL=http://browser-service:4999" in _services()["fastapi"]["environment"]
