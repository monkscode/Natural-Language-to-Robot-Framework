"""Grants and idempotency for the Grafana read-only Postgres role.

Referenced by: nothing — pytest entry point.
Depends on: a live PostgreSQL container named nlrf-postgres. Connection
            details are hardcoded, not DATABASE_URL-driven: _ro_dsn() targets
            127.0.0.1:5432/nlrf directly, and _apply_script() runs the role
            script via `docker exec nlrf-postgres psql`.
"""
import os
import subprocess

import psycopg
import pytest

GRANTED_TABLES = (
    "workflow_metrics",
    "test_runs",
    "llm_traces",
    "execution_records",
    "learning_metrics",
    "nl_feedback_corrections",
    "trigger_events",
)

SCRIPT = "observability/postgres/create_readonly_role.sql"


def _ro_dsn() -> str:
    password = os.environ.get("GRAFANA_DB_PASSWORD", "grafana_ro")
    return f"postgresql://grafana_ro:{password}@127.0.0.1:5432/nlrf"


def _apply_script() -> subprocess.CompletedProcess:
    """Run the role script through the postgres container, as the app user."""
    password = os.environ.get("GRAFANA_DB_PASSWORD", "grafana_ro")
    with open(SCRIPT, encoding="utf-8") as fh:
        sql = fh.read()
    return subprocess.run(
        ["docker", "exec", "-i", "nlrf-postgres", "psql", "-U", "nlrf", "-d", "nlrf",
         "-v", "ON_ERROR_STOP=1", "-v", f"grafana_password={password}"],
        input=sql, text=True, capture_output=True,
    )


@pytest.mark.integration
def test_script_is_idempotent():
    first = _apply_script()
    assert first.returncode == 0, first.stderr
    second = _apply_script()
    assert second.returncode == 0, second.stderr


@pytest.mark.integration
def test_role_can_select_every_granted_table():
    _apply_script()
    with psycopg.connect(_ro_dsn()) as conn, conn.cursor() as cur:
        for table in GRANTED_TABLES:
            cur.execute(f"SELECT 1 FROM {table} LIMIT 1")


@pytest.mark.integration
def test_role_cannot_write():
    _apply_script()
    with psycopg.connect(_ro_dsn()) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("INSERT INTO test_runs (run_id, status) VALUES ('x', 'error')")


@pytest.mark.integration
def test_role_cannot_read_ungranted_tables():
    _apply_script()
    with psycopg.connect(_ro_dsn()) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("SELECT 1 FROM audit_log LIMIT 1")
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("SELECT 1 FROM users LIMIT 1")
