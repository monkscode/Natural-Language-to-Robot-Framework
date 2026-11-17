"""Tests for the held-out sweep's query file loader and run scoring.

Referenced by: nothing (pytest collects it)
Depends on: bench/sweep_lib.py
"""

import json

import pytest

from bench.sweep_lib import SweepQuery, failure_tag, load_sweep_queries, score_run

LOCATOR_TIMEOUT = "TimeoutError: locator.click: Timeout 10000ms exceeded.\nwaiting for locator('#x')"
ASSERT_MSG = "'$54.00' does not contain '$74.00'"


def _entry(**over):
    base = {
        "id": "h01",
        "query": "Open the page and check the total",
        "source": "own",
        "half": "dev",
        "expect": "pass",
        "judge": "The total reads $74.00",
    }
    base.update(over)
    return base


def _write(tmp_path, entries):
    p = tmp_path / "q.json"
    p.write_text(json.dumps({"queries": entries}), encoding="utf-8")
    return p


def _q(**over):
    base = dict(id="h01", query="q", source="own", half="dev", expect="pass",
                judge="j", bug_signature=None)
    base.update(over)
    return SweepQuery(**base)


def test_loads_valid_file(tmp_path):
    p = _write(tmp_path, [
        _entry(),
        _entry(id="h02", expect="fail", judge=None, bug_signature="$54.00",
               source="public", half="dev"),
    ])
    qs = load_sweep_queries(p)
    assert [q.id for q in qs] == ["h01", "h02"]
    assert qs[0].bug_signature is None
    assert qs[1].judge is None and qs[1].bug_signature == "$54.00"
    assert qs[1].source == "public"


def test_loads_utf8(tmp_path):
    p = _write(tmp_path, [_entry(judge="Price reads ₹500")])
    assert load_sweep_queries(p)[0].judge == "Price reads ₹500"


@pytest.mark.parametrize("entries,needle", [
    ([_entry(), _entry()], "h01"),
    ([_entry(half="test")], "h01"),
    ([_entry(expect="maybe")], "h01"),
    ([_entry(judge=None)], "h01"),
    ([_entry(expect="fail", judge=None)], "h01"),
    ([_entry(source="public", half="sealed")], "h01"),
    ([_entry(bugsignature="x")], "h01"),
])
def test_refusals_name_the_id(tmp_path, entries, needle):
    with pytest.raises(ValueError, match=needle):
        load_sweep_queries(_write(tmp_path, entries))


def test_refuses_duplicate_id_names_second(tmp_path):
    with pytest.raises(ValueError, match="h07"):
        load_sweep_queries(_write(tmp_path, [_entry(id="h06"), _entry(id="h07"), _entry(id="h07")]))


def test_refuses_sealed_public(tmp_path):
    with pytest.raises(ValueError, match="sealed"):
        load_sweep_queries(_write(tmp_path, [_entry(source="public", half="sealed")]))


def test_refuses_pass_without_judge(tmp_path):
    with pytest.raises(ValueError, match="judge"):
        load_sweep_queries(_write(tmp_path, [_entry(judge=None)]))


def test_refuses_fail_without_signature(tmp_path):
    with pytest.raises(ValueError, match="bug_signature"):
        load_sweep_queries(_write(tmp_path, [_entry(expect="fail", judge=None)]))


def test_sealed_own_is_allowed(tmp_path):
    assert load_sweep_queries(_write(tmp_path, [_entry(half="sealed")]))[0].half == "sealed"


def test_failure_tag_locator_dead():
    assert failure_tag(LOCATOR_TIMEOUT) == "locator_dead"
    assert failure_tag("waiting for locator('#x')") == "locator_dead"
    assert failure_tag("Timeout 5000ms exceeded.") == "locator_dead"


def test_failure_tag_timeout_alone_is_not_locator_dead():
    assert failure_tag("Timeout while connecting") == "other"
    assert failure_tag("exceeded the quota") == "other"


def test_failure_tag_assertion():
    assert failure_tag(ASSERT_MSG) == "assertion"
    for m in ("x should be y", "a != b", "a == b", "page should contain z"):
        assert failure_tag(m) == "assertion", m


def test_failure_tag_other_and_none():
    assert failure_tag("Docker exploded") == "other"
    assert failure_tag("") == "none"
    assert failure_tag(None) == "none"


def test_score_planted_caught():
    q = _q(expect="fail", judge=None, bug_signature="$54.00")
    assert score_run(q, "complete", "failed", None, ASSERT_MSG) == "handled"


def test_score_planted_missed():
    q = _q(expect="fail", judge=None, bug_signature="$54.00")
    assert score_run(q, "complete", "passed", None, None) == "missed_bug"


def test_score_planted_wrong_failure():
    q = _q(expect="fail", judge=None, bug_signature="$54.00")
    assert score_run(q, "complete", "failed", None, LOCATOR_TIMEOUT) == "not_handled"
    assert score_run(q, "complete", "failed", None, None) == "not_handled"
    assert score_run(q, "complete", "error", None, "$54.00") == "not_handled"


def test_score_planted_signature_only_in_resolved_html():
    msg = (
        "TimeoutError: locator.click: Timeout 30000ms exceeded.\n"
        "Call log:\n"
        "  - waiting for locator('#react-select-3-option-1')\n"
        '    - locator resolved to <div id="x" class="select__option '
        'select__option--is-disabled css-1">Blue</div>\n'
    )
    q = _q(expect="fail", judge=None, bug_signature="Blue")
    assert score_run(q, "complete", "failed", None, msg) == "not_handled"


def test_score_planted_timeout_catch_counts():
    msg = (
        "TimeoutError: locator.waitFor: Timeout 10000ms exceeded.\n"
        "Call log:\n"
        "  - waiting for locator('[data-testid=\"orders-list\"]') to be visible"
    )
    q = _q(expect="fail", judge=None, bug_signature="orders-list")
    assert score_run(q, "complete", "failed", None, msg) == "handled"


def test_score_pass_handled():
    assert score_run(_q(), "complete", "passed", "passed", None) == "handled"


def test_score_judge_failed():
    assert score_run(_q(), "complete", "passed", "failed", None) == "judge_failed"


def test_score_judge_error():
    assert score_run(_q(), "complete", "passed", "error", None) == "judge_error"
    assert score_run(_q(), "complete", "passed", None, None) == "judge_error"


def test_score_pass_test_not_passed():
    for t in ("failed", "error", None):
        assert score_run(_q(), "complete", t, None, "boom") == "test_failed"


def test_score_generation_failed():
    for g in ("error", None):
        assert score_run(_q(), g, "passed", "passed", None) == "generation_failed"
        assert score_run(_q(expect="fail", judge=None, bug_signature="x"),
                         g, "failed", None, "x") == "generation_failed"
