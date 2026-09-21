"""Re-derive stored failure labels from their own stored error messages (E5b).

Dry run by default: a READ-ONLY transaction that prints what would change and
writes nothing. --apply does everything in ONE transaction: it first copies
every row it is about to change into timestamped snapshot tables, then
relabels, deletes and syncs hints. Any error rolls the whole run back,
snapshot included.

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
- nl_feedback_corrections.original_failure_category — a hint's own copy of its
  source run's label — is synced to that run's label as this apply leaves it
  (a run relabelled in the same apply hands its hints the new label). A run
  with no label never overwrites a hint. Each synced hint gets its own
  hint_audit row, so its visible history says what changed, when and why.

Every label write is counted from the cursor's rowcount, not from the plan: a
relabel or a hint's compare-and-set must touch exactly one row — a hint edited
since it was read touches none — and an anti-pattern delete must touch every
id. Any miss rolls the whole transaction back. A restore checks the same way,
plus its anti-pattern and learning_anchors re-inserts, checked against the
snapshot's own counts. Mirrored execution_embeddings writes are reported, not
required: a row with no mirror is not an error.

To undo an apply, name its stamp — printed on success, and in the snapshot table
names (e5b_backfill_<stamp>_labels):
    python tools/backfill_failure_categories.py --restore <stamp>            # dry run
    python tools/backfill_failure_categories.py --restore <stamp> --apply
The restore runs in one transaction and refuses, writing nothing, when any row the
apply changed has moved since (a label changed, a row vanished, an anti-pattern was
reinforced), when a deleted anti-pattern or its anchor exists again, or when a newer
apply is still in place — restores run newest first. anti_patterns.last_seen is TEXT
in UTC; a value that does not parse as a timestamp refuses rather than guessing.
Do not hand-write restore SQL: bare statements under psql autocommit half-apply, and
nothing in them checks drift. A restored hint also gets its own hint_audit row —
audit rows are append-only, so a restore never deletes the apply's.

The snapshot tables are the only undo record: keep them until the owner accepts the new labels.

Run it after the E5b classifier is deployed, so no process is still writing
old labels while it runs.

A label back-fill is DATA, not schema, so it deliberately does not live in
PG_MIGRATIONS: a failing migration is swallowed at boot and silently disables
the whole learning system.

Referenced by: nothing (operator tool).
Depends on: crew_ai/optimization/failure_analyzer.py, core/config.py, psycopg.
"""
import argparse
import json
import re
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
    """Return the ids of anti-patterns whose own message is a placeholder failure. Pure.

    A composite row is skipped here for the same reason plan_backfill skips it:
    its stored message is a sentence the detector wrote about the query, so no
    message-derived conclusion may act on it.
    """
    classifier = FailureClassifier()
    return [
        row["id"]
        for row in rows
        if row["table"] == "anti_patterns"
        and row.get("error_message")
        and row.get("failure_category") not in COMPOSITE_ONLY_CATEGORIES
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


# nl_feedback_corrections.original_failure_category is a third copy of the run's
# label, taken when the hint is created (nl_feedback_engine.py) and never refreshed.
# Owner decision 2026-09-21: it means "the run's label", so a relabelled run carries
# its hints with it. Each changed hint gets a hint_audit row, as an admin edit would,
# so its visible history says what changed and why.
HINT_ROWS_SELECT = (
    "SELECT c.id, c.original_failure_category, c.source_workflow_id, "
    "e.failure_category AS run_category FROM nl_feedback_corrections c "
    "JOIN execution_records e ON e.workflow_id = c.source_workflow_id "
    "ORDER BY c.id")
# Compare-and-set: a hint edited since it was read touches no row, and the
# rowcount guard then rolls the whole run back.
HINT_SYNC_UPDATE = (
    "UPDATE nl_feedback_corrections SET original_failure_category = %s "
    "WHERE id = %s::bigint AND original_failure_category IS NOT DISTINCT FROM %s")
# 'change_category' is already labelled "changed category" in HintDrawer's
# ACTION_LABELS and no other writer uses it; 'system' matches auto_disable.
HINT_AUDIT_INSERT = (
    "INSERT INTO hint_audit (hint_id, action, actor, reason, before_value, after_value, "
    "created_at) VALUES (%s::integer, 'change_category', 'system', %s, %s, %s, %s)")


def plan_hint_sync(hint_rows: list[dict], record_plan: list[dict]) -> list[dict]:
    """One entry per hint whose copy of its run's label disagrees with the run. Pure.

    The run's label is taken as this apply will leave it: a run relabelled by
    record_plan hands its hints the new label, so a dry run shows what --apply writes.
    A run with no label never overwrites a hint.
    """
    relabelled = {entry["id"]: entry["new"] for entry in record_plan
                  if entry["table"] == "execution_records"}
    plan: list[dict] = []
    for row in hint_rows:
        run_label = relabelled.get(row["source_workflow_id"], row["run_category"])
        if run_label and row["original_failure_category"] != run_label:
            plan.append({"table": "nl_feedback_corrections", "id": str(row["id"]),
                         "old": row["original_failure_category"], "new": run_label,
                         "source_workflow_id": row["source_workflow_id"]})
    return plan


# --- restore -------------------------------------------------------------------

# A stamp is the UTC second an --apply ran, embedded in its snapshot table names.
# It is the only operator input that reaches an SQL identifier, so it is checked
# against this before any SQL is built: 14 digits cannot carry an injection.
STAMP_RE = re.compile(r"[0-9]{14}")

# Where each snapshotted label lives now, keyed as the snapshot's source_table.
# _load_current appends FOR UPDATE when applying, so a concurrent learning write
# cannot land between the drift check and the restore.
CURRENT_SELECTS = {
    "execution_records": (
        "SELECT workflow_id AS row_id, failure_category, NULL::text AS last_seen "
        "FROM execution_records WHERE workflow_id = ANY(%s)"),
    "execution_embeddings": (
        "SELECT workflow_id AS row_id, failure_category, NULL::text AS last_seen "
        "FROM execution_embeddings WHERE workflow_id = ANY(%s)"),
    "anti_patterns": (
        "SELECT id::text AS row_id, failure_category, last_seen "
        "FROM anti_patterns WHERE id::text = ANY(%s)"),
    "nl_feedback_corrections": (
        "SELECT id::text AS row_id, original_failure_category AS failure_category, "
        "NULL::text AS last_seen FROM nl_feedback_corrections WHERE id::text = ANY(%s)"),
}
# The restore writes old_category back through the statements the apply used, so
# the two cannot address different rows.
RESTORE_UPDATES = {**UPDATES, "execution_embeddings": MIRROR_UPDATE}
# Named columns, never SELECT *: a column dropped and re-added moves to the end of
# the table, and a positional copy would then swap values with no error.
ANTI_PATTERN_COLUMNS = (
    "id, failure_category, query_pattern, bad_code_snippet, error_message, "
    "correct_alternative, domain, score, evidence_count, last_seen, org_id")
ANCHOR_COLUMNS = "anchor_key, kind, record_id, anchor_query, embedding, org_id"
# A deleted anti-pattern whose id, exact twin or anchor key is back in the live
# tables would be duplicated by a restore.
COLLISION_CHECKS = (
    ("anti-pattern id {v} is in use again",
     "SELECT a.id::text AS v FROM anti_patterns a JOIN {anti} s ON a.id = s.id"),
    ("anti-pattern {v} is an exact twin of a deleted one",
     "SELECT a.id::text AS v FROM anti_patterns a JOIN {anti} s "
     "ON a.org_id IS NOT DISTINCT FROM s.org_id AND a.failure_category = s.failure_category "
     "AND a.query_pattern IS NOT DISTINCT FROM s.query_pattern AND a.id <> s.id"),
    ("anchor key {v} exists again",
     "SELECT a.anchor_key AS v FROM learning_anchors a JOIN {anchors} s USING (anchor_key)"),
)
SNAPSHOT_LABEL_TABLES = (
    "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
    "AND tablename ~ '^e5b_backfill_[0-9]{14}_labels$' ORDER BY tablename")


def parse_stamp(stamp: str) -> datetime:
    """The UTC instant an --apply ran, from the stamp in its snapshot table names."""
    if not isinstance(stamp, str) or not STAMP_RE.fullmatch(stamp):
        raise ValueError(f"a stamp is 14 digits, YYYYMMDDHHMMSS; got {stamp!r}")
    return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def parse_last_seen(value: str) -> datetime | None:
    """Read anti_patterns.last_seen, which is TEXT, as a UTC instant.

    Two writers exist: the Postgres datetime() shim stamps 'YYYY-MM-DD HH:MM:SS'
    in UTC (pg_schema.py), and a Python writer may stamp an isoformat() string.
    A naive value is UTC. Returns None when the text is not a timestamp at all —
    the caller must then refuse, never guess.
    """
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def plan_restore_refusals(labels: list[dict], current: dict[tuple[str, str], dict],
                          collisions: list[str], applied_at: datetime) -> list[str]:
    """Every reason a restore must not run. Pure — no I/O. An empty list means safe.

    labels:     the snapshot's rows {source_table, row_id, old_category, new_category}
    current:    live rows keyed (source_table, row_id); a missing key is a row that
                no longer exists
    collisions: a deleted anti-pattern's id, exact twin or anchor key that exists again
    applied_at: parse_stamp() of the apply being undone
    """
    refusals: list[str] = []
    for label in labels:
        key = (label["source_table"], label["row_id"])
        row = current.get(key)
        if row is None:
            refusals.append(f"{key[0]} {key[1]}: the row no longer exists")
            continue
        if row["failure_category"] != label["new_category"]:
            refusals.append(f"{key[0]} {key[1]}: now {row['failure_category']!r}, "
                            f"but the apply wrote {label['new_category']!r}")
        # Reinforcing an anti-pattern stamps last_seen and adds evidence under its
        # current label; putting the old label back would misfile that evidence.
        # NULL means it was never reinforced. The same second counts as after: a
        # false refusal costs a look, a false restore writes stale data silently.
        if key[0] == "anti_patterns" and row.get("last_seen") is not None:
            seen = parse_last_seen(row["last_seen"])
            if seen is None:
                refusals.append(f"anti_patterns {key[1]}: last_seen {row['last_seen']!r} "
                                "is not a timestamp, so reinforcement cannot be ruled out")
            elif seen >= applied_at:
                refusals.append(f"anti_patterns {key[1]}: reinforced at "
                                f"{seen.isoformat()}, at or after the apply")
    return refusals + collisions


def _load_current(conn, labels: list[dict], lock: bool) -> dict[tuple[str, str], dict]:
    """The live row behind each snapshot label, keyed (source_table, row_id)."""
    current: dict[tuple[str, str], dict] = {}
    for table, select in CURRENT_SELECTS.items():
        ids = [label["row_id"] for label in labels if label["source_table"] == table]
        if not ids:
            continue
        for row in conn.execute(select + (" FOR UPDATE" if lock else ""), (ids,)).fetchall():
            current[(table, row["row_id"])] = row
    return current


def _collisions(conn, anti: str, anchors: str) -> list[str]:
    found: list[str] = []
    for template, sql in COLLISION_CHECKS:
        for row in conn.execute(sql.format(anti=anti, anchors=anchors)).fetchall():
            found.append(template.format(v=row["v"]))
    return found


def _unrestored_newer_stamps(conn, stamp: str) -> list[str]:
    """Snapshots taken after this one whose own changes are still in place.

    Restores must run newest first: an older restore under a newer apply would put
    back labels the newer apply built on. A newer snapshot counts as restored once
    every row it changed holds its old_category again.
    """
    blocking: list[str] = []
    for row in conn.execute(SNAPSHOT_LABEL_TABLES).fetchall():
        other = row["tablename"][len("e5b_backfill_"):][:14]
        if other <= stamp:
            continue
        labels = conn.execute("SELECT source_table, row_id, old_category, new_category "
                              f"FROM e5b_backfill_{other}_labels").fetchall()
        current = _load_current(conn, labels, lock=False)
        if any(current.get((l["source_table"], l["row_id"]), {}).get("failure_category")
               != l["old_category"] for l in labels):
            blocking.append(f"snapshot {other} is newer and not restored — restore it first")
    return blocking


def _restore(conn, stamp: str, applied_at: datetime, apply: bool) -> None:
    """Undo the --apply that took snapshot `stamp`. A dry run unless apply.

    Refuses, writing nothing, when any row the apply changed has moved since, when a
    deleted anti-pattern or its anchor exists again, or when a newer apply is still
    in place. Every write is counted against the snapshot.
    """
    labels_t, anti_t, anchors_t = (f"e5b_backfill_{stamp}_{part}"
                                   for part in ("labels", "anti_patterns", "anchors"))
    for table in (labels_t, anti_t, anchors_t):
        if conn.execute("SELECT to_regclass(%s) AS t", (table,)).fetchone()["t"] is None:
            raise SystemExit(f"restore refused: snapshot table {table} does not exist")
    labels = conn.execute("SELECT source_table, row_id, old_category, new_category "
                          f"FROM {labels_t}").fetchall()
    n_anti = conn.execute(f"SELECT count(*) AS n FROM {anti_t}").fetchone()["n"]
    n_anchors = conn.execute(f"SELECT count(*) AS n FROM {anchors_t}").fetchone()["n"]

    unknown = sorted({label["source_table"] for label in labels} - set(CURRENT_SELECTS))
    refusals = [f"this tool cannot restore rows of {table}" for table in unknown]
    refusals += _unrestored_newer_stamps(conn, stamp)
    current = _load_current(conn, labels, lock=apply)
    refusals += plan_restore_refusals(labels, current, _collisions(conn, anti_t, anchors_t),
                                      applied_at)

    print(f"snapshot {stamp}: {len(labels)} label(s), {n_anti} anti-pattern(s) and "
          f"{n_anchors} anchor(s) to put back")
    if refusals:
        for reason in refusals:
            print(f"  refused: {reason}")
        raise SystemExit(f"restore refused for {len(refusals)} reason(s) above; "
                         "nothing was written")
    if not apply:
        print("Dry run: nothing was written. Re-run with --apply to restore.")
        return

    print(f"\nrestoring into {conn.info.dbname} on {conn.info.host}")
    now = datetime.now(timezone.utc).isoformat()
    for label in labels:
        is_hint = label["source_table"] == "nl_feedback_corrections"
        if is_hint:
            cursor = conn.execute(HINT_SYNC_UPDATE, (label["old_category"], label["row_id"],
                                                     label["new_category"]))
        else:
            cursor = conn.execute(RESTORE_UPDATES[label["source_table"]],
                                  (label["old_category"], label["row_id"]))
        if cursor.rowcount != 1:
            raise RuntimeError(f"{label['source_table']} {label['row_id']}: expected 1 row "
                               f"restored, got {cursor.rowcount} — rolling back")
        if is_hint:
            conn.execute(HINT_AUDIT_INSERT, (
                label["row_id"], f"E5b back-fill restore (snapshot {stamp})",
                json.dumps({"original_failure_category": label["new_category"]}),
                json.dumps({"original_failure_category": label["old_category"]}), now))
    restored_anti = conn.execute(
        f"INSERT INTO anti_patterns ({ANTI_PATTERN_COLUMNS}) OVERRIDING SYSTEM VALUE "
        f"SELECT {ANTI_PATTERN_COLUMNS} FROM {anti_t}").rowcount
    restored_anchors = conn.execute(
        f"INSERT INTO learning_anchors ({ANCHOR_COLUMNS}) "
        f"SELECT {ANCHOR_COLUMNS} FROM {anchors_t}").rowcount
    if (restored_anti, restored_anchors) != (n_anti, n_anchors):
        raise RuntimeError(f"expected {n_anti} anti-pattern(s) and {n_anchors} anchor(s) "
                           f"restored, got {restored_anti} and {restored_anchors} — rolling back")
    conn.commit()
    print(f"restored: {len(labels)} label(s), {restored_anti} anti-pattern(s) and "
          f"{restored_anchors} anchor(s) from snapshot {stamp}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="snapshot, then write the changes (default: dry run)")
    parser.add_argument("--restore", metavar="STAMP",
                        help="undo the --apply whose snapshot tables carry STAMP "
                             "(a dry run unless --apply is also given)")
    args = parser.parse_args(argv)

    applied_at = None
    if args.restore is not None:
        try:
            applied_at = parse_stamp(args.restore)
        except ValueError as exc:
            parser.error(str(exc))

    import psycopg
    from psycopg.rows import dict_row

    from src.backend.core.config import settings

    with psycopg.connect(settings.DATABASE_URL, row_factory=dict_row) as conn:
        # A dry run cannot write, even by mistake.
        conn.read_only = not args.apply
        if args.restore is not None:
            _restore(conn, args.restore, applied_at, args.apply)
            return
        rows = _load(conn, "execution_records") + _load(conn, "anti_patterns")
        plan = plan_backfill(rows)
        deletions = plan_placeholder_deletions(rows)
        hint_rows = conn.execute(
            HINT_ROWS_SELECT + (" FOR UPDATE OF c" if args.apply else "")).fetchall()
        hint_plan = plan_hint_sync(hint_rows, plan)

        print(f"{len(rows)} rows read: {len(plan)} would be relabelled, "
              f"{len(deletions)} placeholder anti-pattern(s) would be deleted, "
              f"{len(hint_plan)} hint(s) would take their run's label")
        for entry in plan:
            print(f"  relabel {entry['table']:18} {entry['id']:40} "
                  f"{entry['old']} -> {entry['new']} ({entry['specific_type']})")
        for anti_id in deletions:
            print(f"  delete  anti_patterns      {anti_id:40} placeholder failure, with its anchor")
        for entry in hint_plan:
            print(f"  sync    nl_feedback_corrections {entry['id']:24} "
                  f"{entry['old']} -> {entry['new']} (from run {entry['source_workflow_id']})")

        if not args.apply:
            print("\nDry run: nothing was written. Re-run with --apply to snapshot and write. "
                  "--apply also mirrors each execution_records label onto its "
                  "execution_embeddings row.")
            return

        if not plan and not deletions and not hint_plan:
            print("\nNothing to apply: no stored label changes. No snapshot taken.")
            return

        print(f"\napplying to {conn.info.dbname} on {conn.info.host}")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        snapshots = _snapshot(conn, stamp, plan + hint_plan, deletions)

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
        synced = 0
        for entry in hint_plan:
            cursor = conn.execute(HINT_SYNC_UPDATE, (entry["new"], entry["id"], entry["old"]))
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"hint {entry['id']}: expected 1 row synced, got {cursor.rowcount} "
                    "(edited since it was read?) — rolling back")
            conn.execute(HINT_AUDIT_INSERT, (
                entry["id"],
                f"E5b back-fill: synced from source run {entry['source_workflow_id']} "
                f"(snapshot {stamp})",
                json.dumps({"original_failure_category": entry["old"]}),
                json.dumps({"original_failure_category": entry["new"]}),
                datetime.now(timezone.utc).isoformat()))
            synced += 1
        conn.commit()
        print(f"applied: {relabelled} relabelled, {mirrored} mirrored onto "
              f"execution_embeddings, {deleted} anti-pattern(s) deleted with "
              f"{anchors} anchor(s), {synced} hint(s) synced and audited; "
              f"snapshot tables: {', '.join(snapshots)}")


if __name__ == "__main__":
    main()
