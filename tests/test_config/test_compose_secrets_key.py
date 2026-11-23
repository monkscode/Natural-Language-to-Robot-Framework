"""#131: only fastapi may hold SECRETS_ENCRYPTION_KEY; the two services that share its env_file blank it."""

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


def _env_list(service: str) -> list[str]:
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    return services[service]["environment"]


def test_runner_exec_and_browser_service_blank_the_encryption_key():
    for service in ("runner-exec", "browser-service"):
        assert "SECRETS_ENCRYPTION_KEY=" in _env_list(service), service


def test_fastapi_does_not_blank_the_encryption_key():
    assert not any(str(e).startswith("SECRETS_ENCRYPTION_KEY") for e in _env_list("fastapi"))
