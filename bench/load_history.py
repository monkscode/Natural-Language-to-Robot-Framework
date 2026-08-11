"""Load the bench sweep corpus into the `bench` schema for Grafana.

Reads bench/baselines/*.csv, their .meta.json sidecars, and the captured
workflow_metrics/test_runs payloads under bench/runs/<workflow_id>/. Writes two
tables. Idempotent — safe to run repeatedly, and run_bench.py calls it at the
end of every sweep.

llm_traces (94.7 MiB) and artifacts (1,895.2 MiB / 19,128 files) are NOT loaded:
the dashboard says which run, the filesystem holds the bulk.

Detachment: writes only to schema `bench`, fully qualified. The application
never references it, so bench data cannot reach History or the production
metrics dashboards.

Referenced by: bench/run_bench.py, tests/test_bench/test_load_history.py
Depends on: bench/history_lib.py, observability/postgres/create_bench_schema.sql
"""
import argparse
import csv
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from bench import history_lib

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DDL_PATH = REPO_ROOT / "observability" / "postgres" / "create_bench_schema.sql"
BASELINES = REPO_ROOT / "bench" / "baselines"
RUNS = REPO_ROOT / "bench" / "runs"


def ensure_schema(conn, schema: str = "bench") -> None:
    """Create the schema and tables if absent. Same DDL grafana-db-init runs."""
    sql = DDL_PATH.read_text(encoding="utf-8")
    if schema != "bench":
        sql = sql.replace("bench.", f"{schema}.").replace(
            "CREATE SCHEMA IF NOT EXISTS bench", f"CREATE SCHEMA IF NOT EXISTS {schema}")
    conn.execute(sql)
    conn.commit()


def _read_meta(csv_path: Path) -> tuple[dict, datetime, str]:
    """Sidecar contents, capture time, and which source the time came from."""
    sidecar = Path(f"{csv_path}.meta.json")
    if sidecar.exists():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("unparseable sidecar, falling back to mtime: %s", sidecar)
            meta = {}
        captured = meta.get("captured_at")
        if captured:
            return meta, datetime.fromisoformat(captured), "meta"
    else:
        meta = {}
    # 3 of 78 sweeps have no sidecar. mtime is weaker provenance — a copy
    # resets it — and the dashboards say so.
    return meta, datetime.fromtimestamp(csv_path.stat().st_mtime), "mtime"


def _payload(runs_dir: Path, workflow_id: str, filename: str) -> dict | None:
    """First captured row's payload, or None when absent/empty/unusable."""
    if not workflow_id:
        return None
    path = runs_dir / workflow_id / filename
    if not path.exists():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("unreadable payload: %s", path)
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    return parsed[0]


def load_corpus(conn, baselines_dir: Path = BASELINES, runs_dir: Path = RUNS,
                only: str | None = None, schema: str = "bench") -> dict[str, int]:
    """Load every sweep CSV (or just `only`) into the bench schema."""
    ensure_schema(conn, schema)

    paths = sorted(baselines_dir.glob("*.csv"))
    if only:
        paths = [p for p in paths if p.name == only]
        if not paths:
            raise FileNotFoundError(f"no sweep named {only} in {baselines_dir}")

    parsed_sweeps = []
    for path in paths:
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
            header = reader.fieldnames or []
        meta, captured, source = _read_meta(path)
        ids = {(r.get("workflow_id") or "").strip() for r in rows}
        parsed_sweeps.append({
            "name": path.name, "rows": rows, "header": header, "meta": meta,
            "captured": captured, "source": source,
            "ids": {i for i in ids if i},
        })

    # Derived-sweep detection runs across the whole batch, so a re-costed file
    # is caught whether or not its parent was loaded on an earlier run.
    known = _existing_sweep_ids(conn, schema)
    for sweep in parsed_sweeps:
        known.setdefault(sweep["name"], sweep["ids"])
    derived_count = 0
    for sweep in parsed_sweeps:
        parent = None
        for other_name, other_ids in known.items():
            if other_name == sweep["name"]:
                continue
            ratio = history_lib.overlap_ratio(sweep["ids"], other_ids)
            if ratio >= history_lib.DERIVED_THRESHOLD:
                other_captured = _captured_of(parsed_sweeps, other_name, conn, schema)
                winner = history_lib.pick_parent(
                    sweep["name"], other_name, sweep["captured"], other_captured)
                if winner != sweep["name"]:
                    parent = other_name
                break
        sweep["derived_from"] = parent
        if parent:
            derived_count += 1
            logger.warning(
                "%s shares its run ids with %s — marking it derived; it is a "
                "re-rendering, not a separate experiment",
                sweep["name"], parent)

    unknown_total = 0
    run_total = 0
    for sweep in parsed_sweeps:
        query_ids = [r.get("query_id") or "" for r in sweep["rows"]]
        family = history_lib.sweep_family(query_ids)
        expected = history_lib.expected_count(family)
        conn.execute(
            f"""INSERT INTO {schema}.sweeps (
                    sweep_name, captured_at, captured_at_source, family,
                    run_count, expected_count, is_complete, is_flagged_invalid,
                    derived_from, pins, git_sha, git_branch, header_shape)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (sweep_name) DO UPDATE SET
                    captured_at = EXCLUDED.captured_at,
                    captured_at_source = EXCLUDED.captured_at_source,
                    family = EXCLUDED.family,
                    run_count = EXCLUDED.run_count,
                    expected_count = EXCLUDED.expected_count,
                    is_complete = EXCLUDED.is_complete,
                    is_flagged_invalid = EXCLUDED.is_flagged_invalid,
                    derived_from = EXCLUDED.derived_from,
                    pins = EXCLUDED.pins,
                    git_sha = EXCLUDED.git_sha,
                    git_branch = EXCLUDED.git_branch,
                    header_shape = EXCLUDED.header_shape""",
            (sweep["name"], sweep["captured"], sweep["source"], family,
             len(sweep["rows"]), expected, len(sweep["rows"]) == expected,
             history_lib.is_flagged_invalid(sweep["name"]), sweep["derived_from"],
             Jsonb(sweep["meta"].get("nlrf_pins") or {}),
             sweep["meta"].get("git_sha"), sweep["meta"].get("git_branch"),
             len(sweep["header"])))

        for row in sweep["rows"]:
            typed, extra = history_lib.split_row(row)
            if extra:
                unknown_total += len(extra)
                logger.warning("%s: unrecognised columns routed to extra: %s",
                               sweep["name"], sorted(extra))
            workflow_id = (row.get("workflow_id") or "").strip()
            metrics_row = _payload(runs_dir, workflow_id, "workflow_metrics.json")
            data = metrics_row.get("data") if isinstance(metrics_row, dict) else None
            if not isinstance(data, dict) or not data:
                data = None
            test_row = _payload(runs_dir, workflow_id, "test_runs.json")
            typed["dryrun_status_normalised"] = history_lib.normalise_dryrun(
                row.get("generation_status"), row.get("dryrun_status"),
                (data or {}).get("dryrun_status"))
            typed["sweep_name"] = sweep["name"]
            typed["metrics"] = Jsonb(data) if data is not None else None
            typed["test_run"] = Jsonb(test_row) if test_row is not None else None
            typed["extra"] = Jsonb(extra)
            typed["artifact_dir"] = (
                str(runs_dir / workflow_id / "artifacts") if workflow_id else None)
            _upsert_run(conn, typed, schema)
            run_total += 1

    conn.commit()
    logger.info("loaded %d sweeps / %d runs (%d derived, %d unknown columns)",
                len(parsed_sweeps), run_total, derived_count, unknown_total)
    return {"sweeps": len(parsed_sweeps), "runs": run_total,
            "derived": derived_count, "unknown_columns": unknown_total}


def _existing_sweep_ids(conn, schema: str) -> dict[str, set[str]]:
    rows = conn.execute(
        f"SELECT sweep_name, array_agg(workflow_id) FROM {schema}.runs "
        f"WHERE workflow_id IS NOT NULL AND workflow_id <> '' "
        f"GROUP BY sweep_name").fetchall()
    return {name: set(ids) for name, ids in rows}


def _captured_of(parsed_sweeps, name: str, conn, schema: str):
    for sweep in parsed_sweeps:
        if sweep["name"] == name:
            return sweep["captured"]
    row = conn.execute(
        f"SELECT captured_at FROM {schema}.sweeps WHERE sweep_name = %s",
        (name,)).fetchone()
    return row[0] if row else datetime.min


def _upsert_run(conn, typed: dict, schema: str) -> None:
    columns = sorted(typed)
    placeholders = ",".join(["%s"] * len(columns))
    assignments = ",".join(
        f"{c} = EXCLUDED.{c}" for c in columns
        if c not in ("sweep_name", "query_id", "repeat_index"))
    conn.execute(
        f"""INSERT INTO {schema}.runs ({",".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT (sweep_name, query_id, repeat_index)
            DO UPDATE SET {assignments}""",
        [typed[c] for c in columns])


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Load bench sweep CSVs into the Postgres `bench` schema.")
    parser.add_argument("--only", default=None,
                        help="load a single sweep by CSV filename")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url:
        logger.error("no DATABASE_URL set and --database-url not given")
        return 2
    with psycopg.connect(args.database_url) as conn:
        result = load_corpus(conn, only=args.only)
    logger.info("done: %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
