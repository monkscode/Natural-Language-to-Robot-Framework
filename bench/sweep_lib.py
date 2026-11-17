"""Pure logic for the held-out sweep (NLRF #179) - query file and run scoring.

Covers:
- the held-out query file: JSON `{"queries": [...]}`, checked on load
- a coarse failure tag for a run's error message
- one verdict per run (`score_run`) from the stream's own statuses

Status values are the stream's own (docker_service.py): generation_status is
"complete" / "error" / None; test and judge statuses are "passed" / "failed" /
"error" / None.

Referenced by: bench sweep runner (later task), tests/test_bench/test_sweep_lib.py
Depends on: nothing (standard library only)
"""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SweepQuery:
    id: str
    query: str
    source: str
    half: str
    expect: str
    judge: str | None
    bug_signature: str | None


_REQUIRED = ("id", "query", "source", "half", "expect")
_OPTIONAL = ("judge", "bug_signature")


def _check(entry: dict, seen: set[str]) -> SweepQuery:
    qid = entry.get("id")
    extra = set(entry) - set(_REQUIRED) - set(_OPTIONAL)
    if extra:
        raise ValueError(f"query {qid!r}: unknown keys {sorted(extra)}")
    missing = [k for k in _REQUIRED if k not in entry]
    if missing:
        raise ValueError(f"query {qid!r}: missing keys {missing}")
    if qid in seen:
        raise ValueError(f"query {qid!r}: duplicate id")
    q = SweepQuery(**{k: entry.get(k) for k in _REQUIRED + _OPTIONAL})
    if q.half not in ("dev", "sealed"):
        raise ValueError(f"query {qid!r}: half must be dev or sealed, got {q.half!r}")
    if q.expect not in ("pass", "fail"):
        raise ValueError(f"query {qid!r}: expect must be pass or fail, got {q.expect!r}")
    if q.expect == "pass" and not q.judge:
        raise ValueError(f"query {qid!r}: expect pass needs a judge")
    if q.expect == "fail" and not q.bug_signature:
        raise ValueError(f"query {qid!r}: expect fail needs a bug_signature")
    if q.source == "public" and q.half == "sealed":
        raise ValueError(f"query {qid!r}: a public query cannot be in the sealed half")
    return q


def load_sweep_queries(path: Path) -> list[SweepQuery]:
    """Read and check the query file; raises ValueError naming the bad query id."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: list[SweepQuery] = []
    seen: set[str] = set()
    for entry in data["queries"]:
        q = _check(entry, seen)
        seen.add(q.id)
        out.append(q)
    return out


def failure_tag(error_message: str | None) -> str:
    """locator_dead / assertion / other / none, from a run's error message."""
    if not error_message:
        return "none"
    if "waiting for locator" in error_message or (
        "Timeout" in error_message and "exceeded" in error_message
    ):
        return "locator_dead"
    if any(s in error_message for s in ("should be", "does not contain", "!=", "==", "should contain")):
        return "assertion"
    return "other"


def score_run(
    q: SweepQuery,
    generation_status: str | None,
    test_status: str | None,
    judge_status: str | None,
    error_message: str | None,
) -> str:
    """One verdict: generation_failed, handled, judge_failed, test_failed,
    judge_error, missed_bug or not_handled."""
    if generation_status != "complete":
        return "generation_failed"
    if q.expect == "pass":
        if test_status != "passed":
            return "test_failed"
        if judge_status == "passed":
            return "handled"
        return "judge_failed" if judge_status == "failed" else "judge_error"
    if test_status == "passed":
        return "missed_bug"
    if test_status == "failed" and q.bug_signature and q.bug_signature in (error_message or ""):
        return "handled"
    return "not_handled"
