"""Re-derive stored failure labels from their own stored error messages (E5b).

Dry run by default: a READ-ONLY transaction that prints what would change and
writes nothing. --apply does everything in ONE transaction: it first copies
every row it is about to change into timestamped snapshot tables, then
relabels and deletes. Any error rolls the whole run back, snapshot included.

What --apply changes:
- execution_records (failed runs) and anti_patterns: failure_category is
  re-derived with the current FailureClassifier from the row's own
  error_message — exactly how the label was first made. A row whose message
  classifies as unknown keeps its label. A1/A7 rows keep theirs: those come
  from the composite detector, which reads the query and the code, not the
  message.
- anti_patterns whose own message is a placeholder failure are DELETED, with
  their learning_anchors row. E5b stops storing placeholder failures as
  anti-patterns; a stored one would keep being injected into prompts.

Run it after the E5b classifier is deployed, so no process is still writing
old labels while it runs.

A label back-fill is DATA, not schema, so it deliberately does not live in
PG_MIGRATIONS: a failing migration is swallowed at boot and silently disables
the whole learning system.

Referenced by: nothing (operator tool).
Depends on: crew_ai/optimization/failure_analyzer.py, core/config.py, psycopg.
"""
import argparse
import logging
from datetime import datetime, timezone

from src.backend.crew_ai.optimization.failure_analyzer import FailureClassifier

logger = logging.getLogger(__name__)

# Composite-detector labels: derived from the query and the code structure,
# not from the message, so the message cannot re-derive them.
COMPOSITE_ONLY_CATEGORIES = frozenset({"A1", "A7"})
PLACEHOLDER_TYPE = "placeholder_never_resolved"

SELECTS = {
    "execution_records": (
        "SELECT workflow_id AS id, failure_category, error_message "
        "FROM execution_records WHERE test_status = 'failed'"
    ),
    "anti_patterns": (
        "SELECT id::text AS id, failure_category, error_message FROM anti_patterns"
    ),
}
UPDATES = {
    "execution_records": "UPDATE execution_records SET failure_category = %s WHERE workflow_id = %s",
    "anti_patterns": "UPDATE anti_patterns SET failure_category = %s WHERE id = %s::bigint",
}


def plan_backfill(rows: list[dict]) -> list[dict]:
    """Return one relabel entry per row whose category changes. Pure — no I/O.

    Placeholder anti-patterns are not relabelled: plan_placeholder_deletions()
    removes them instead.
    """
    classifier = FailureClassifier()
    plan: list[dict] = []
    for row in rows:
        message = row.get("error_message")
        if not message or row.get("failure_category") in COMPOSITE_ONLY_CATEGORIES:
            continue
        analysis = classifier.classify(message)
        if analysis.category == "unknown":
            continue
        if row["table"] == "anti_patterns" and analysis.specific_type == PLACEHOLDER_TYPE:
            continue
        if analysis.category != row.get("failure_category"):
            plan.append({
                "table": row["table"],
                "id": row["id"],
                "old": row.get("failure_category"),
                "new": analysis.category,
                "specific_type": analysis.specific_type,
            })
    return plan


def plan_placeholder_deletions(rows: list[dict]) -> list[str]:
    """Return the ids of anti-patterns whose own message is a placeholder failure. Pure."""
    classifier = FailureClassifier()
    return [
        row["id"]
        for row in rows
        if row["table"] == "anti_patterns"
        and row.get("error_message")
        and classifier.classify(row["error_message"]).specific_type == PLACEHOLDER_TYPE
    ]


def _load(conn, table: str) -> list[dict]:
    rows = conn.execute(SELECTS[table]).fetchall()
    return [{"table": table, **dict(r)} for r in rows]


def _snapshot(conn, stamp: str, plan: list[dict], deletions: list[str]) -> list[str]:
    """Copy everything --apply is about to change, in the same transaction as the writes."""
    labels = f"e5b_backfill_{stamp}_labels"
    anti = f"e5b_backfill_{stamp}_anti_patterns"
    anchors = f"e5b_backfill_{stamp}_anchors"
    ids = [int(anti_id) for anti_id in deletions]
    conn.execute(f"CREATE TABLE {labels} (source_table TEXT, row_id TEXT, "
                 "old_category TEXT, new_category TEXT)")
    for entry in plan:
        conn.execute(f"INSERT INTO {labels} VALUES (%s, %s, %s, %s)",
                     (entry["table"], entry["id"], entry["old"], entry["new"]))
    # LIKE + INSERT rather than CREATE TABLE AS: a utility statement cannot
    # take bound parameters.
    conn.execute(f"CREATE TABLE {anti} (LIKE anti_patterns)")
    conn.execute(f"INSERT INTO {anti} SELECT * FROM anti_patterns WHERE id = ANY(%s)", (ids,))
    conn.execute(f"CREATE TABLE {anchors} (LIKE learning_anchors)")
    conn.execute(f"INSERT INTO {anchors} SELECT * FROM learning_anchors "
                 "WHERE kind = 'anti' AND record_id = ANY(%s)", (ids,))
    return [labels, anti, anchors]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="snapshot, then write the changes (default: dry run)")
    args = parser.parse_args(argv)

    import psycopg
    from psycopg.rows import dict_row

    from src.backend.core.config import settings

    with psycopg.connect(settings.DATABASE_URL, row_factory=dict_row) as conn:
        # A dry run cannot write, even by mistake.
        conn.read_only = not args.apply
        rows = _load(conn, "execution_records") + _load(conn, "anti_patterns")
        plan = plan_backfill(rows)
        deletions = plan_placeholder_deletions(rows)

        print(f"{len(rows)} rows read: {len(plan)} would be relabelled, "
              f"{len(deletions)} placeholder anti-pattern(s) would be deleted")
        for entry in plan:
            print(f"  relabel {entry['table']:18} {entry['id']:40} "
                  f"{entry['old']} -> {entry['new']} ({entry['specific_type']})")
        for anti_id in deletions:
            print(f"  delete  anti_patterns      {anti_id:40} placeholder failure, with its anchor")

        if not args.apply:
            print("\nDry run: nothing was written. Re-run with --apply to snapshot and write.")
            return

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        snapshots = _snapshot(conn, stamp, plan, deletions)
        for entry in plan:
            conn.execute(UPDATES[entry["table"]], (entry["new"], entry["id"]))
        if deletions:
            ids = [int(anti_id) for anti_id in deletions]
            conn.execute("DELETE FROM learning_anchors "
                         "WHERE kind = 'anti' AND record_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM anti_patterns WHERE id = ANY(%s)", (ids,))
        conn.commit()
        print(f"applied: {len(plan)} relabelled, {len(deletions)} deleted; "
              f"snapshot tables: {', '.join(snapshots)}")


if __name__ == "__main__":
    main()
