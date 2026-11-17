"""Tests for the held-out sweep report (bench/sweep_report.py).

Referenced by: nothing (pytest collects it).
Depends on: bench.sweep_report.
"""

import json

from bench.sweep_report import main, summarize, wilson


def _row(qid, half, repeat, outcome, tag=None, hollow=(), error_message=None):
    return {
        "query_id": qid, "half": half, "source": "s", "expect": "pass",
        "repeat": repeat, "workflow_id": f"wf-{qid}-{repeat}",
        "outcome": outcome, "tag": tag, "hollow": list(hollow),
        "error_message": error_message,
    }


def _write(tmp_path, rows):
    (tmp_path / "results.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def test_wilson_matches_issue():
    lo, hi = wilson(11, 18)
    assert round(lo, 3) == 0.386
    assert round(hi, 3) == 0.797
    assert round(wilson(30, 30)[0], 3) == 0.886
    assert wilson(0, 0) == (0.0, 0.0)


def test_counts_by_half():
    rows = [
        _row("d1", "dev", 1, "handled"),
        _row("d1", "dev", 2, "not_handled", tag="TAG-A", hollow=["READ_NOT_PERFORMED"]),
        _row("s1", "sealed", 1, "handled"),
    ]
    s = summarize(rows)
    assert s["dev"]["runs"] == 2 and s["dev"]["handled"] == 1
    assert s["dev"]["outcomes"] == {"handled": 1, "not_handled": 1}
    assert s["dev"]["tags"] == {"TAG-A": 1}
    assert s["dev"]["hollow"] == {"READ_NOT_PERFORMED": 1}
    assert s["sealed"]["runs"] == 1 and s["sealed"]["handled"] == 1


def test_cold_vs_warm_split():
    rows = [
        _row("d1", "dev", 1, "handled"),
        _row("d1", "dev", 2, "handled"),
        _row("d1", "dev", 3, "not_handled"),
        _row("d2", "dev", 1, "not_handled"),
        _row("d2", "dev", 2, "handled"),
        _row("d2", "dev", 3, "handled"),
    ]
    s = summarize(rows)["dev"]
    assert s["cold"] == (1, 2)
    assert s["warm"] == (3, 4)


def test_query_level_run1(tmp_path, capsys):
    rows = [
        _row("d1", "dev", 1, "handled"),
        _row("d2", "dev", 1, "handled"),
        _row("d3", "dev", 1, "not_handled"),
        _row("d3", "dev", 2, "handled"),
    ]
    assert summarize(rows)["dev"]["run1_queries"] == (2, 3)
    _write(tmp_path, rows)
    main([str(tmp_path)])
    assert "handled on run 1: 2 of 3 queries" in capsys.readouterr().out


def test_sealed_has_no_per_query_lines(tmp_path, capsys):
    rows = [
        _row("dev-one", "dev", 1, "handled"),
        _row("sealed-secret-id", "sealed", 1, "not_handled",
             error_message="sealed query text leaked"),
        _row("sealed-secret-id", "sealed", 2, "handled"),
    ]
    _write(tmp_path, rows)
    main([str(tmp_path)])
    out = capsys.readouterr().out
    assert "dev-one" in out
    assert "sealed-secret-id" not in out
    assert "leaked" not in out
    assert "wf-sealed" not in out
    assert "sealed" in out  # the half's totals are still printed


def test_empty_half_prints_n0(tmp_path, capsys):
    _write(tmp_path, [_row("d1", "dev", 1, "handled")])
    main([str(tmp_path)])
    assert "n=0" in capsys.readouterr().out
