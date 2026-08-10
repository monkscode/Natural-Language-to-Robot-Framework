"""Grants and idempotency for the Grafana read-only Postgres role.

Referenced by: nothing — pytest entry point.
Depends on: a live PostgreSQL container named nlrf-postgres, holding the app
            schema. Connection details are hardcoded, not DATABASE_URL-driven:
            _ro_dsn() targets 127.0.0.1:5432/nlrf directly, and _apply_script()
            runs the role script via `docker exec nlrf-postgres psql`.

The whole module skips when that container is absent, which is the case in CI.
Two things make the dependency unavoidable rather than lazy. The script uses
`\\gexec`, a psql META-command that no driver can execute, so it has to run
through a psql binary or not be under test at all. And it grants on seven app
tables, so it errors out on a database that does not already have them — a
fresh Postgres has zero tables in `public` (the auth and learning suites build
theirs in ISOLATED schemas), and the script fails there at
`relation "workflow_metrics" does not exist`, verified 2026-08-10.

What CI does cover is test_dashboards.py::test_readonly_role_grants_match_the
_dashboards, which needs no database and catches the drift that actually
happens: a panel querying a table nobody granted.
"""
import os
import subprocess

import psycopg
import pytest


def _stack_postgres_is_running() -> bool:
    """True when a container named exactly nlrf-postgres is up.

    Anchored filter: `name=nlrf-postgres` is a SUBSTRING match in Docker, so an
    unrelated nlrf-postgres-probe would satisfy it. Any failure to ask — Docker
    absent, daemon down, CLI hung — reads as "not available" and skips, which is
    the safe direction for a guard whose only job is deciding whether the real
    dependency is there.
    """
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=^nlrf-postgres$", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "nlrf-postgres" in result.stdout.split()


pytestmark = pytest.mark.skipif(
    not _stack_postgres_is_running(),
    reason="needs the local stack: a running nlrf-postgres container holding the app schema",
)

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
