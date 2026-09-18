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
- execution_embeddings carries a second copy of the label for the same
  workflow_id, written from the same value. Each relabelled execution_record
  is mirrored there, so the two copies cannot disagree. A row with no mirror
  (no user_query at store time) is simply skipped.
- anti_patterns whose own message is a placeholder failure are DELETED, with
  their learning_anchors row. E5b stops storing placeholder failures as
  anti-patterns; a stored one would keep being injected into prompts.

Every write is counted from the cursor's rowcount, not from the plan. A
relabel that does not touch exactly one row, or an anti-pattern delete that
does not touch every id, raises — which rolls the whole transaction back,
snapshot included.

To undo an apply, using the snapshot tables printed on success:
    UPDATE execution_records e SET failure_category = s.old_category
      FROM e5b_backfill_<stamp>_labels s
     WHERE s.source_table = 'execution_records' AND e.workflow_id = s.row_id;
    UPDATE execution_embeddings x SET failure_category = s.old_category
      FROM e5b_backfill_<stamp>_labels s
     WHERE s.source_table = 'execution_embeddings' AND x.workflow_id = s.row_id;
    UPDATE anti_patterns a SET failure_category = s.old_category
      FROM e5b_backfill_<stamp>_labels s
     WHERE s.source_table = 'anti_patterns' AND a.id = s.row_id::bigint;
    INSERT INTO anti_patterns SELECT * FROM e5b_backfill_<stamp>_anti_patterns;
    INSERT INTO learning_anchors SELECT * FROM e5b_backfill_<stamp>_anchors;

Run it after the E5b classifier is deployed, so no process is still writing
old labels while it runs.

A label back-fill is DATA, not schema, so it deliberately does not live in
PG_MIGRATIONS: a failing migration is swallowed at boot and silently disables
the whole learning system.

Referenced by: nothing (operator tool).
Depends on: crew_ai/optimization/failure_analyzer.py, core/config.py, psycopg.
"""
import argparse
from datetime import datetime, timezone

from src.backend.crew_ai.optimization.failure_analyzer import FailureClassifier

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
# execution_embeddings keeps a denormalised copy of the label under the same
# workflow_id; a relabel that skipped it would leave the two disagreeing.
MIRROR_UPDATE = (
    "UPDATE execution_embeddings SET failure_category = %s WHERE workflow_id = %s"
)


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
    for entry in plan:
        if entry["table"] == "execution_records":
            conn.execute(
                f"INSERT INTO {labels} (source_table, row_id, old_category, new_category) "
                "SELECT 'execution_embeddings', workflow_id, failure_category, %s "
                "FROM execution_embeddings WHERE workflow_id = %s",
                (entry["new"], entry["id"]))
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
            print("\nDry run: nothing was written. Re-run with --apply to snapshot and write. "
                  "--apply also mirrors each execution_records label onto its "
                  "execution_embeddings row.")
            return

        if not plan and not deletions:
            print("\nNothing to apply: no stored label changes. No snapshot taken.")
            return

        print(f"\napplying to {conn.info.dbname} on {conn.info.host}")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        snapshots = _snapshot(conn, stamp, plan, deletions)

        relabelled = mirrored = deleted = anchors = 0
        for entry in plan:
            cursor = conn.execute(UPDATES[entry["table"]], (entry["new"], entry["id"]))
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"{entry['table']} {entry['id']}: expected 1 row relabelled, "
                    f"got {cursor.rowcount} — rolling back")
            relabelled += 1
            if entry["table"] == "execution_records":
                # A workflow with no stored embedding row has nothing to mirror.
                mirrored += conn.execute(
                    MIRROR_UPDATE, (entry["new"], entry["id"])).rowcount
        if deletions:
            ids = [int(anti_id) for anti_id in deletions]
            # An anti-pattern only has an anchor if it was created after anchoring
            # existed, so this count is reported, not required.
            anchors = conn.execute("DELETE FROM learning_anchors "
                                   "WHERE kind = 'anti' AND record_id = ANY(%s)",
                                   (ids,)).rowcount
            removed = conn.execute("DELETE FROM anti_patterns WHERE id = ANY(%s)", (ids,))
            if removed.rowcount != len(ids):
                raise RuntimeError(
                    f"expected {len(ids)} anti-pattern(s) deleted, got "
                    f"{removed.rowcount} — rolling back")
            deleted = removed.rowcount
        conn.commit()
        print(f"applied: {relabelled} relabelled, {mirrored} mirrored onto "
              f"execution_embeddings, {deleted} anti-pattern(s) deleted with "
              f"{anchors} anchor(s); snapshot tables: {', '.join(snapshots)}")


if __name__ == "__main__":
    main()
