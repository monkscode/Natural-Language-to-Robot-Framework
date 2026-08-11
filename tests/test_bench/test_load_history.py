"""Loader tests against a disposable schema in the live Postgres.

These never touch schema `public` and never touch the real bench schema: each
test creates `bench_test_<uuid>`, points the loader at it, and drops it.

Referenced by: nothing — pytest entry point.
Depends on: bench/load_history.py, a reachable DATABASE_URL
"""
import csv
import json
import os
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from bench import load_history

pytestmark = pytest.mark.integration

HEADER = ["query_id", "query", "repeat", "workflow_id", "started_at",
          "generation_status", "test_status", "dryrun_status", "total_s",
          "llm_cost_usd", "failed_elements"]


def _row(query_id, repeat, wid, gen="complete", test="passed", dry="", cost="0.05"):
    return {"query_id": query_id, "query": "open a page", "repeat": str(repeat),
            "workflow_id": wid, "started_at": "2026-08-01T10:00:00",
            "generation_status": gen, "test_status": test, "dryrun_status": dry,
            "total_s": "40.5", "llm_cost_usd": cost, "failed_elements": "0"}


def _write_sweep(directory: Path, name: str, rows, header=HEADER, meta=None):
    path = directory / name
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    if meta is not None:
        (directory / f"{name}.meta.json").write_text(
            json.dumps(meta), encoding="utf-8")
    return path


@pytest.fixture
def conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    with psycopg.connect(url) as connection:
        schema = f"bench_test_{uuid.uuid4().hex[:12]}"
        connection.execute(f"CREATE SCHEMA {schema}")
        connection.commit()
        try:
            yield connection, schema
        finally:
            connection.rollback()
            connection.execute(f"DROP SCHEMA {schema} CASCADE")
            connection.commit()


def test_loading_twice_produces_identical_counts(conn, tmp_path):
    """Idempotence is the whole contract: run_bench.py calls this at the end
    of every sweep, and an operator will re-run it by hand."""
    connection, schema = conn
    baselines = tmp_path / "baselines"; baselines.mkdir()
    runs = tmp_path / "runs"; runs.mkdir()
    _write_sweep(baselines, "2026-08-01-example.csv",
                 [_row("q01", i, f"wid-{i}") for i in range(3)])

    first = load_history.load_corpus(connection, baselines, runs, schema=schema)
    second = load_history.load_corpus(connection, baselines, runs, schema=schema)
    assert first["runs"] == second["runs"] == 3
    assert first["sweeps"] == second["sweeps"] == 1

    count = connection.execute(f"SELECT count(*) FROM {schema}.runs").fetchone()[0]
    assert count == 3


def test_an_unknown_column_lands_in_extra_and_is_logged(conn, tmp_path, caplog):
    connection, schema = conn
    baselines = tmp_path / "baselines"; baselines.mkdir()
    runs = tmp_path / "runs"; runs.mkdir()
    header = HEADER + ["brand_new_column"]
    row = _row("q01", 0, "wid-0"); row["brand_new_column"] = "surprise"
    _write_sweep(baselines, "2026-08-01-drift.csv", [row], header=header)

    with caplog.at_level("WARNING"):
        result = load_history.load_corpus(connection, baselines, runs, schema=schema)

    assert result["unknown_columns"] == 1
    assert "brand_new_column" in caplog.text
    stored = connection.execute(
        f"SELECT extra FROM {schema}.runs").fetchone()[0]
    assert stored == {"brand_new_column": "surprise"}


def test_generation_error_rows_are_not_stamped_passed(conn, tmp_path):
    connection, schema = conn
    baselines = tmp_path / "baselines"; baselines.mkdir()
    runs = tmp_path / "runs"; runs.mkdir()
    _write_sweep(baselines, "2026-08-01-errors.csv", [
        _row("q01", 0, "wid-0"),
        _row("q02", 0, "", gen="error", test=""),
    ])
    load_history.load_corpus(connection, baselines, runs, schema=schema)
    rows = dict(connection.execute(
        f"SELECT query_id, dryrun_status_normalised FROM {schema}.runs").fetchall())
    assert rows["q01"] == "passed"
    assert rows["q02"] == "not_reached"


def test_a_blank_workflow_id_does_not_collide(conn, tmp_path):
    """Two generation errors in one sweep would break a
    (sweep_name, workflow_id) key. The real key is query_id + repeat."""
    connection, schema = conn
    baselines = tmp_path / "baselines"; baselines.mkdir()
    runs = tmp_path / "runs"; runs.mkdir()
    _write_sweep(baselines, "2026-08-01-two-errors.csv", [
        _row("q01", 0, "", gen="error", test=""),
        _row("q02", 0, "", gen="error", test=""),
    ])
    result = load_history.load_corpus(connection, baselines, runs, schema=schema)
    assert result["runs"] == 2
    stored = connection.execute(
        f"SELECT count(*) FROM {schema}.runs").fetchone()[0]
    assert stored == 2


def test_a_re_costed_sweep_is_marked_derived(conn, tmp_path):
    """The three -ADJ files share every workflow_id with their parent and
    differ only in cost columns. Offering both to the comparison dashboard
    would report a cost delta on a pass delta of zero."""
    connection, schema = conn
    baselines = tmp_path / "baselines"; baselines.mkdir()
    runs = tmp_path / "runs"; runs.mkdir()
    rows = [_row("q01", i, f"wid-{i}") for i in range(3)]
    adjusted = [dict(r, llm_cost_usd="0.09") for r in rows]
    _write_sweep(baselines, "2026-08-01-thing.csv", rows,
                 meta={"captured_at": "2026-08-01T10:00:00"})
    _write_sweep(baselines, "2026-08-01-thing-ADJ.csv", adjusted,
                 meta={"captured_at": "2026-08-01T10:00:00"})

    result = load_history.load_corpus(connection, baselines, runs, schema=schema)
    assert result["derived"] == 1
    derived = dict(connection.execute(
        f"SELECT sweep_name, derived_from FROM {schema}.sweeps").fetchall())
    assert derived["2026-08-01-thing.csv"] is None
    assert derived["2026-08-01-thing-ADJ.csv"] == "2026-08-01-thing.csv"


def test_two_re_costings_of_the_same_parent_both_point_at_the_root(conn, tmp_path):
    """One sweep in the real corpus already has a re-costing; a second
    re-costing of that same parent would create a three-way overlap — a
    parent and TWO derived sweeps that all share the same workflow ids.
    Stopping at the first candidate above DERIVED_THRESHOLD (the old
    behaviour) could settle on one re-costing pointing at the OTHER
    re-costing instead of the root
    parent, because _existing_sweep_ids groups by an unordered GROUP BY, so
    candidate order is not stable across loads. Both re-costings must
    resolve to the same root parent, and the root itself must never be
    marked derived."""
    connection, schema = conn
    baselines = tmp_path / "baselines"; baselines.mkdir()
    runs = tmp_path / "runs"; runs.mkdir()
    rows = [_row("q01", i, f"wid-{i}") for i in range(3)]
    recost_a = [dict(r, llm_cost_usd="0.09") for r in rows]
    recost_b = [dict(r, llm_cost_usd="0.11") for r in rows]
    _write_sweep(baselines, "2026-08-01-thing.csv", rows,
                 meta={"captured_at": "2026-08-01T10:00:00"})
    _write_sweep(baselines, "2026-08-01-thing-ADJ-a.csv", recost_a,
                 meta={"captured_at": "2026-08-01T11:00:00"})
    _write_sweep(baselines, "2026-08-01-thing-ADJ-b.csv", recost_b,
                 meta={"captured_at": "2026-08-01T12:00:00"})

    load_history.load_corpus(connection, baselines, runs, schema=schema)
    derived = dict(connection.execute(
        f"SELECT sweep_name, derived_from FROM {schema}.sweeps").fetchall())
    assert derived["2026-08-01-thing.csv"] is None
    assert derived["2026-08-01-thing-ADJ-a.csv"] == "2026-08-01-thing.csv"
    assert derived["2026-08-01-thing-ADJ-b.csv"] == "2026-08-01-thing.csv"


def test_a_captured_payload_is_attached_and_a_missing_one_is_null(conn, tmp_path):
    connection, schema = conn
    baselines = tmp_path / "baselines"; baselines.mkdir()
    runs = tmp_path / "runs"; runs.mkdir()
    (runs / "wid-0").mkdir()
    (runs / "wid-0" / "workflow_metrics.json").write_text(
        json.dumps([{"workflow_id": "wid-0", "data": {"total_cost": 0.05}}]),
        encoding="utf-8")
    _write_sweep(baselines, "2026-08-01-payloads.csv",
                 [_row("q01", 0, "wid-0"), _row("q02", 0, "wid-missing")])

    load_history.load_corpus(connection, baselines, runs, schema=schema)
    rows = dict(connection.execute(
        f"SELECT query_id, metrics FROM {schema}.runs").fetchall())
    assert rows["q01"] == {"total_cost": 0.05}
    assert rows["q02"] is None


def test_a_short_header_omits_typed_columns_without_breaking_the_insert(conn, tmp_path):
    """A previous review worried that history_lib.split_row omits dict keys
    entirely for columns absent from a short CSV header (it does — see
    CANONICAL_COLUMNS, 52 entries, against HEADER above, 11), and that
    _upsert_run's dynamic column list (`columns = sorted(typed)`) might then
    break, or silently misalign values, on a sweep whose header doesn't carry
    every typed column. It cannot: the INSERT's column list and its VALUES
    tuple are both built from the same `typed` dict via the same `columns`
    ordering, so a column with no key just never appears in that row's
    INSERT, and Postgres fills the gap with its column default — NULL, since
    none of the typed columns other than dryrun_status_normalised are
    NOT NULL. This test proves it rather than asserting it: it loads a sweep
    whose header omits several typed columns entirely (not blank cells —
    absent keys, exactly like every test above already does with HEADER) and
    checks that those columns land as SQL NULL, not a missing-row error."""
    connection, schema = conn
    baselines = tmp_path / "baselines"; baselines.mkdir()
    runs = tmp_path / "runs"; runs.mkdir()
    _write_sweep(baselines, "2026-08-01-short-header.csv",
                 [_row("q01", 0, "wid-0")])

    load_history.load_corpus(connection, baselines, runs, schema=schema)
    row = connection.execute(
        f"SELECT plan_s, llm_calls, locator_success_rate FROM {schema}.runs "
        f"WHERE query_id = 'q01'").fetchone()
    assert row == (None, None, None)
