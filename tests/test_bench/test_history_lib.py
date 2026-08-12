"""Unit tests for bench/history_lib.py and the bench schema DDL.

Referenced by: nothing — pytest entry point.
Depends on: bench/history_lib.py, observability/postgres/create_bench_schema.sql
"""
import ast
import os
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bench import bench_lib, history_lib

DDL = Path("observability/postgres/create_bench_schema.sql")


def _ddl_columns(table: str) -> set[str]:
    """Column names declared inside one CREATE TABLE block of the DDL."""
    sql = DDL.read_text(encoding="utf-8")
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\((.*?)\n\);",
        sql, re.IGNORECASE | re.DOTALL,
    )
    assert match, f"{DDL.name} has no CREATE TABLE for {table}"
    cols = set()
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        if re.match(r"^(PRIMARY|FOREIGN|UNIQUE|CHECK|CONSTRAINT)\b", line, re.IGNORECASE):
            continue
        ident = re.match(r"^([a-z_][a-z0-9_]*)\b", line)
        if ident:
            cols.add(ident.group(1))
    return cols


def test_ddl_is_idempotent_by_construction():
    """Every object the DDL creates must be guarded, because grafana-db-init
    runs this file on every `docker compose --profile observability up` under
    ON_ERROR_STOP=1. One unguarded CREATE aborts the init container, and
    Grafana then never starts."""
    sql = DDL.read_text(encoding="utf-8")
    creates = re.findall(r"CREATE\s+(SCHEMA|TABLE|INDEX)\s+(IF NOT EXISTS)?", sql,
                         re.IGNORECASE)
    assert creates, "DDL creates nothing"
    for kind, guard in creates:
        assert guard, f"CREATE {kind} without IF NOT EXISTS is not idempotent"


def test_sweeps_table_has_the_provenance_columns():
    cols = _ddl_columns("bench.sweeps")
    assert {"sweep_name", "captured_at", "captured_at_source", "family",
            "run_count", "expected_count", "is_complete", "is_flagged_invalid",
            "derived_from", "pins", "git_sha", "git_branch",
            "header_shape"} <= cols


def test_runs_primary_key_is_query_and_repeat_not_workflow_id():
    """3 corpus rows carry a blank workflow_id (all generation errors), so
    (sweep_name, workflow_id) is unique only by accident of which sweeps they
    landed in. (sweep_name, query_id, repeat) was verified unique across all
    78 files and 1,984 rows."""
    sql = DDL.read_text(encoding="utf-8")
    match = re.search(r"PRIMARY KEY\s*\(([^)]*)\)", sql, re.IGNORECASE)
    assert match, "bench.runs declares no PRIMARY KEY"
    key = [c.strip() for c in match.group(1).split(",")]
    assert key == ["sweep_name", "query_id", "repeat_index"], key


def test_runs_table_carries_the_normalised_dryrun_column():
    assert "dryrun_status_normalised" in _ddl_columns("bench.runs")


def test_canonical_columns_cover_the_union_of_all_four_header_shapes():
    """52 columns across shapes 33/45/50/51. `agent_steps` is in exactly one
    sweep — added at shape 50, dropped at shape 51 — and must still be typed,
    because a dropped-then-renamed column is the drift this guards against."""
    assert len(history_lib.CANONICAL_COLUMNS) == 52
    assert history_lib.CANONICAL_COLUMNS["agent_steps"] == "int"
    assert history_lib.CANONICAL_COLUMNS["query_id"] == "text"
    assert history_lib.CANONICAL_COLUMNS["step_budget_exhausted"] == "bool"


def test_repeat_is_renamed_because_it_is_a_postgres_function():
    assert history_lib.CSV_TO_SQL["repeat"] == "repeat_index"
    assert history_lib.CSV_TO_SQL["query_id"] == "query_id"


def test_every_canonical_column_has_a_sql_name():
    assert set(history_lib.CSV_TO_SQL) == set(history_lib.CANONICAL_COLUMNS)


def test_blank_cells_become_null_not_zero():
    """1,982 of 1,984 rows have a blank dryrun_status and 33 of 1,984 have
    blank timings. A blank coerced to 0 would read as a real measurement."""
    assert history_lib.coerce("", "float") is None
    assert history_lib.coerce("   ", "int") is None
    assert history_lib.coerce(None, "text") is None


def test_numeric_coercion():
    assert history_lib.coerce("12.5", "float") == 12.5
    # Counts are written by some sweeps as floats; int("3.0") raises.
    assert history_lib.coerce("3.0", "int") == 3
    assert history_lib.coerce("3", "int") == 3


def test_boolean_coercion_accepts_the_spellings_the_corpus_uses():
    assert history_lib.coerce("True", "bool") is True
    assert history_lib.coerce("true", "bool") is True
    assert history_lib.coerce("False", "bool") is False
    assert history_lib.coerce("", "bool") is None


def test_unknown_columns_are_routed_to_extra_not_dropped():
    """Schema drift is not monotone — shape45 is NOT a subset of shape50 — so
    a future rename must land somewhere visible rather than vanish."""
    row = {"query_id": "q01", "repeat": "1", "total_s": "40.5",
           "brand_new_column": "surprise"}
    typed, extra = history_lib.split_row(row)
    assert typed["query_id"] == "q01"
    assert typed["repeat_index"] == 1
    assert typed["total_s"] == 40.5
    assert extra == {"brand_new_column": "surprise"}


def test_known_columns_never_leak_into_extra():
    row = {c: "" for c in history_lib.CANONICAL_COLUMNS}
    _, extra = history_lib.split_row(row)
    assert extra == {}


def test_every_canonical_column_exists_in_the_ddl():
    """A column typed in Python but missing from the DDL fails at INSERT time
    against a real database, which no unit test would otherwise reach."""
    ddl_cols = _ddl_columns("bench.runs")
    missing = set(history_lib.CSV_TO_SQL.values()) - ddl_cols
    assert not missing, f"DDL is missing columns: {sorted(missing)}"


class TestNormaliseDryrun:
    """Branch order matters and was verified against the corpus: the 34
    generation-error rows and the 60 rows carrying a recorded value are
    disjoint sets, so no row can take two branches."""

    def test_recorded_value_wins(self):
        assert history_lib.normalise_dryrun("complete", "", "passed") == "passed"
        assert history_lib.normalise_dryrun("complete", "", "failed") == "failed"

    def test_non_blank_csv_cell_is_used_when_nothing_was_recorded(self):
        assert history_lib.normalise_dryrun("complete", "failed", None) == "failed"

    def test_generation_error_never_reached_the_gate(self):
        """All 34 generation-error rows have a blank cell. Stamping them
        'passed' would invent a gate result on a run that never ran one."""
        assert history_lib.normalise_dryrun("error", "", None) == "not_reached"

    def test_blank_on_a_completed_run_means_passed(self):
        """1,982 of 1,984 cells are blank. Reading blank as 'unknown' would
        invert the pipeline's main quality gate on almost every row."""
        assert history_lib.normalise_dryrun("complete", "", None) == "passed"

    def test_the_result_is_never_null_or_unknown(self):
        for gen in ("complete", "error", "", None):
            out = history_lib.normalise_dryrun(gen, "", None)
            assert out and out != "unknown"


class TestSweepMetadata:
    def test_family_is_astpp_when_any_query_id_is(self):
        assert history_lib.sweep_family(["q01", "q02"]) == "public"
        assert history_lib.sweep_family(["astpp_q01"]) == "astpp"

    def test_expected_counts(self):
        assert history_lib.expected_count("public") == 30
        assert history_lib.expected_count("astpp") == 18

    def test_invalid_flag_comes_from_the_filename(self):
        assert history_lib.is_flagged_invalid(
            "INVALID-429-2026-01-01-example.csv")
        assert not history_lib.is_flagged_invalid("2026-01-01-example.csv")


class TestDerivedSweepDetection:
    def test_full_overlap_is_derived(self):
        ids = {f"id{i}" for i in range(30)}
        assert history_lib.overlap_ratio(ids, ids) == 1.0
        assert history_lib.overlap_ratio(ids, ids) >= history_lib.DERIVED_THRESHOLD

    def test_disjoint_sweeps_are_independent(self):
        a = {f"a{i}" for i in range(30)}
        b = {f"b{i}" for i in range(30)}
        assert history_lib.overlap_ratio(a, b) == 0.0

    def test_a_sweep_that_shares_a_couple_of_reruns_is_not_derived(self):
        a = {f"id{i}" for i in range(30)}
        b = {f"id{i}" for i in range(2)} | {f"new{i}" for i in range(28)}
        assert history_lib.overlap_ratio(a, b) < history_lib.DERIVED_THRESHOLD

    def test_a_resumed_subset_sweep_is_still_derived(self):
        """overlap_ratio divides by the SMALLER set on purpose, not by
        Jaccard. A resumed sweep is a strict subset of its parent, and
        dividing by the smaller set is exactly what makes that subset score
        1.0 instead of some partial number that could fall under the
        threshold. Measured across the whole corpus: of all 3,003 sweep
        pairs, the only non-zero overlap ratio that occurs is 1.0, exactly 3
        times, all equal-sized (the three known -ADJ pairs) — there is no
        partial-overlap case in the data today, so this test pins intent,
        not a case that has happened yet."""
        parent = {f"id{i}" for i in range(30)}
        resumed_subset = {f"id{i}" for i in range(15)}
        assert history_lib.overlap_ratio(parent, resumed_subset) == 1.0
        assert history_lib.overlap_ratio(parent, resumed_subset) >= history_lib.DERIVED_THRESHOLD

    def test_empty_sets_never_report_overlap(self):
        assert history_lib.overlap_ratio(set(), {"a"}) == 0.0
        assert history_lib.overlap_ratio(set(), set()) == 0.0

    def test_parent_is_the_earlier_sweep(self):
        assert history_lib.pick_parent("later.csv", "earlier.csv",
                                       "2026-08-02", "2026-08-01") == "earlier.csv"

    def test_ties_break_to_the_shorter_name(self):
        """The three real derived pairs have no .meta.json and are dated by
        file mtime, which can tie or invert. In all three the derived file is
        the longer name — it appends a suffix to its parent's."""
        assert history_lib.pick_parent(
            "2026-01-01-example-ADJ.csv",
            "2026-01-01-example.csv",
            "2026-01-01", "2026-01-01") == "2026-01-01-example.csv"


class TestBuildMetaRecordsRevision:
    """The 78 historical sweeps have only their filename as provenance, and
    that cannot be recovered honestly. Every future sweep describes itself."""

    def test_meta_carries_git_sha_and_branch(self):
        """`"git_sha" in meta` alone stays green even if `_git` degrades to
        None on every call — a fired timeout, a wrong cwd, git missing from
        an image, or a future edit that just returns None; every future
        sidecar would silently lose provenance and nothing would catch it.
        This checkout is a real git repo, so assert the actual shape for
        git_sha: a 40-character hex string. git_branch cannot be asserted
        "not None" the same way — CI checks out a detached merge ref (no
        symbolic ref points at HEAD there), so `git_branch` is honestly None
        on every PR run, and asserting non-None would turn a green CI branch
        red the moment this test is pushed. Instead, compare against ground
        truth measured the same way `_git` measures it, in this same
        process and cwd, so the assertion is correct in both environments:
        the real branch name here, None on CI. `!= "HEAD"` stays as a direct
        regression guard for the detached-HEAD bug a companion fix corrected
        — `rev-parse --abbrev-ref HEAD` prints the literal string "HEAD" and
        exits 0 on a detached checkout, so a regression back to it would
        pass an "is not None" check while reporting a fake branch."""
        if shutil.which("git") is None:
            pytest.skip("git binary not on PATH in this environment")
        meta = bench_lib.build_meta({}, {}, "http://localhost:5000",
                                    "http://localhost:4999")
        assert meta["git_sha"] is not None
        assert re.fullmatch(r"[0-9a-f]{40}", meta["git_sha"]), meta["git_sha"]
        expected = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True, text=True,
            cwd=Path(bench_lib.__file__).resolve().parents[1],
        ).stdout.strip() or None
        assert meta["git_branch"] == expected
        assert meta["git_branch"] != "HEAD"

    def test_detached_head_reports_no_branch_but_a_real_sha(self):
        """CI checks out a detached merge ref (`actions/checkout@v4` with no
        `ref:` — verified against a real sonarqube.yml run on a PR: `git
        checkout --force refs/remotes/pull/<n>/merge` leaves the repo in
        'detached HEAD' state), so the live test above cannot assert a
        branch exists there — on CI, None is the honest, correct value.
        This test pins the detached-HEAD CONTRACT deterministically instead,
        independent of how any given checkout happens to be, by emulating
        what git prints in that state: `symbolic-ref` fails (no branch
        points at a detached HEAD), and `rev-parse HEAD` still returns a
        real sha. A regression back to reading the branch via
        `rev-parse --abbrev-ref HEAD` — which prints the literal string
        "HEAD" and exits 0 even when detached — is also emulated and must
        turn this test red, since that fake value is exactly what the
        deleted `is not None` assertion used to let through silently."""
        zero_sha = "0" * 40

        def fake_git(argv, **kwargs):
            if "symbolic-ref" in argv:
                raise subprocess.CalledProcessError(1, argv)
            if "--abbrev-ref" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="HEAD\n")
            return subprocess.CompletedProcess(argv, 0, stdout=f"{zero_sha}\n")

        with patch("bench.bench_lib.subprocess.run", side_effect=fake_git):
            meta = bench_lib.build_meta({}, {}, "http://localhost:5000",
                                        "http://localhost:4999")
        assert meta["git_branch"] is None
        assert meta["git_sha"] == zero_sha

    def test_a_git_failure_does_not_break_the_sweep(self):
        """A detached HEAD, a missing git binary, or a tarball checkout must
        cost provenance, never the run."""
        with patch("bench.bench_lib.subprocess.run",
                   side_effect=subprocess.CalledProcessError(128, "git")):
            meta = bench_lib.build_meta({}, {}, "http://localhost:5000",
                                        "http://localhost:4999")
        assert meta["git_sha"] is None
        assert meta["git_branch"] is None


def test_a_dead_database_does_not_break_a_finished_sweep(capsys):
    """The CSV is already on disk when this runs. Losing the dashboard
    refresh is acceptable; losing a 40-minute paid sweep is not."""
    from bench import run_bench
    with patch.dict(os.environ, {"DATABASE_URL": "postgresql://nope:1/none"}):
        run_bench._load_history_best_effort("bench/baselines/whatever.csv")
    assert "NOT loaded" in capsys.readouterr().out


def test_a_finished_sweep_is_loaded_by_name(capsys):
    """The sweep just written is loaded by filename, not the whole corpus."""
    from bench import run_bench
    with patch.dict(os.environ, {"DATABASE_URL": "postgresql://nope:1/none"}):
        with patch("bench.run_bench.psycopg.connect") as mock_connect:
            with patch("bench.load_history.load_corpus") as mock_load_corpus:
                mock_load_corpus.return_value = {
                    "sweeps": 1, "runs": 30, "derived": 0, "unknown_columns": [],
                }
                run_bench._load_history_best_effort(
                    "bench/baselines/2026-08-11-x.csv")
    mock_connect.assert_called_once()
    mock_load_corpus.assert_called_once()
    _, kwargs = mock_load_corpus.call_args
    assert kwargs.get("only") == "2026-08-11-x.csv"
    assert "loaded" in capsys.readouterr().out


def test_main_calls_load_history_after_the_done_log():
    """No behavioural test can catch the call site vanishing: deleting the
    `_load_history_best_effort(out_path)` line from main() leaves every other
    test in this suite green, because nothing else calls main() end-to-end
    and _load_history_best_effort's own tests invoke it directly, off the
    module, without going through main() at all. So this is a static check —
    it parses bench/run_bench.py's source with ast and inspects main()'s
    TOP-LEVEL statement list (its body), comparing statement POSITION rather
    than line number or ast.walk order. Line numbers are not enough: the
    sweep loop logs its own progress with `_log(f"({done}/{total}) ...")`,
    and that counter variable is named `done`, so a substring match for
    "done" also matches those in-loop logs — and ast.walk visits nodes
    breadth-first, so a naive walk can anchor on one of those instead of the
    real done-log. Anchoring on top-level statement position sidesteps this
    entirely: the only thing asserted is that _load_history_best_effort is a
    top-level statement of main() that comes after the top-level log
    mentioning "done" in main()'s own statement list. A call nested inside
    `finally:` (which also runs on Ctrl-C or an unexpected raise, before a
    single row may exist) or inside the per-run sweep loop (which would
    reload the corpus once per run) is not a top-level statement of main() at
    all, so it fails this check structurally rather than by line-number
    coincidence — which is exactly what the owner's ruling to place the call
    at the end of main(), never inside the preflight helper or the loop,
    requires."""
    source = Path("bench/run_bench.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    main_func = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main")

    def is_named_call_stmt(stmt, name):
        """True if `stmt` is a bare top-level `name(...)` expression statement."""
        if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
            return False
        call = stmt.value
        return isinstance(call.func, ast.Name) and call.func.id == name

    done_log_indices = [
        i for i, stmt in enumerate(main_func.body)
        if is_named_call_stmt(stmt, "_log") and "done" in ast.unparse(stmt.value)
    ]
    assert len(done_log_indices) == 1, (
        "expected exactly one top-level _log(...) statement in main() whose "
        "source mentions 'done' to anchor this test on; found a different "
        "count, so a human must re-anchor this test deliberately")
    done_log_index = done_log_indices[0]

    loader_indices = [
        i for i, stmt in enumerate(main_func.body)
        if is_named_call_stmt(stmt, "_load_history_best_effort")
    ]
    assert loader_indices, (
        "main() no longer calls _load_history_best_effort as a top-level "
        "statement — a finished sweep would never refresh the bench "
        "dashboards again")
    assert loader_indices[0] > done_log_index, (
        "_load_history_best_effort is not a top-level statement of main() "
        "positioned after the top-level 'done' log — it must run once, "
        "after the sweep loop has finished and the last CSV row is on "
        "disk, not from inside a nested block such as finally: or the "
        "per-run sweep loop")

    all_loader_calls = [
        node for node in ast.walk(main_func)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_load_history_best_effort"
    ]
    assert len(all_loader_calls) == 1, (
        "main() calls _load_history_best_effort more than once — a correct "
        "top-level call plus a stray duplicate elsewhere (e.g. left inside "
        "the sweep loop) would reload the corpus once per run instead of "
        "once at the end, and the top-level-position checks above cannot "
        "see a second, nested call")
