"""Bench gate, part 3: hollow passes — the (query, shape) registry, the verified rate, the q10 add-on.

A hollow pass is a passed run whose test src/backend/core/pass_quality.py flags
with a hollow shape. The gate FAILS on any hollow (query, shape) pair NOT in
REGISTERED_HOLLOW. Registered today (owner, 2026-09-25): (q10, READ_NOT_PERFORMED)
— the planner drops the read; a planner rule is approved — and (q05,
READ_LOCATOR_IS_THE_ANSWER) — bs builds the locator from the observed text.
(q08, SELECT_CHECK_CANNOT_FAIL) is deliberately NOT registered: this gate lands
BEFORE the q08 normalizer fix, so every pre-fix bench that holds one fails here —
the intended signal. After the fix, its return is a live regression.

REPORTED, never gated: the verified pass rate (passes minus hollow passes, over
all slots — 4 of 4 recent unchanged benches would fail a 96.7% verified gate:
93.3 / 86.7 / 83.3 / 70.0%); FRAGILE_NUMERIC_ID passes (right today, pinned to
one repository id); and a standing line that a read of the wrong cell (the q05
wrong column, 3555f609-1258-48df-8188-ecf1a07fa12a) is not statically detectable.

The q10 add-on (a later bench runs q10 ten times after the planner read rule):
PASS when at least Q10_ADDON_MIN_READS of exactly Q10_ADDON_RUNS runs perform a
read — pass_quality's READ_NOT_PERFORMED does not fire on the run's test. A run
with no test (a generation error) does not read.

Referenced by: bench/gate.py.
Depends on: src/backend/core/pass_quality.py, bench/gate_inputs.py (GateLine, Run,
Slot, PASSED, classify).
"""

from collections import Counter

from bench.gate_inputs import PASSED, GateLine, Run, Slot, classify
from src.backend.core.pass_quality import (
    FRAGILE_NUMERIC_ID,
    READ_LOCATOR_IS_THE_ANSWER,
    READ_NOT_PERFORMED,
    check_pass_quality,
)

REGISTERED_HOLLOW = frozenset({("q10", READ_NOT_PERFORMED), ("q05", READ_LOCATOR_IS_THE_ANSWER)})
WRONG_CELL_EXAMPLE = "3555f609-1258-48df-8188-ecf1a07fa12a"
Q10_ADDON_RUNS = 10
Q10_ADDON_MIN_READS = 6


def _code(run: Run) -> str | None:
    path = run.capture / "artifacts" / "test.robot"
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def pass_findings(slots: list[Slot]) -> list[tuple[Run, list]]:
    """(run, findings) for every passed current run."""
    out = []
    for slot in slots:
        if slot.outcome != PASSED:
            continue
        run = slot.current
        out.append((run, check_pass_quality(_code(run) or "", run.row.get("query") or "")))
    return out


def hollow_lines(slots: list[Slot]) -> list[GateLine]:
    found = pass_findings(slots)
    unregistered, registered = [], Counter()
    hollow_runs, fragile = 0, []
    for run, findings in found:
        hollow = [f for f in findings if f.hollow]
        hollow_runs += bool(hollow)
        for f in hollow:
            if (run.query_id, f.shape) in REGISTERED_HOLLOW:
                registered[(run.query_id, f.shape)] += 1
            else:
                unregistered.append(f"({run.query_id}, {f.shape}) {run.label}"
                                    + (f" [{f.detail}]" if f.detail else ""))
        if any(f.shape == FRAGILE_NUMERIC_ID for f in findings):
            fragile.append(run)
    reg_text = ", ".join(f"({q}, {s}) x{n}" for (q, s), n in sorted(registered.items())) or "none"
    if unregistered:
        hollow = GateLine("HOLLOW", "FAIL", f"{len(unregistered)} hollow pass finding(s) NOT in the "
                          f"registry; registered ones seen: {reg_text}", unregistered)
    else:
        hollow = GateLine("HOLLOW", "PASS", f"no unregistered hollow pair; registered ones seen: {reg_text}")
    n, passes = len(slots), len(found)
    verified = passes - hollow_runs
    return [
        hollow,
        GateLine("VERIFIED", "INFO", f"verified pass rate {verified}/{n} = {verified / n * 100:.1f}% "
                 f"({passes} passes minus {hollow_runs} hollow) — reported, not gated"),
        GateLine("FRAGILE", "INFO", f"{len(fragile)} passes read by a numeric id (FRAGILE_NUMERIC_ID) — "
                 f"reported, not hollow", [r.label for r in fragile]),
        GateLine("NOT CHECKED", "INFO", "a read of the wrong cell is not statically detectable "
                 f"(e.g. the q05 wrong column, {WRONG_CELL_EXAMPLE})"),
    ]


def q10_addon_line(runs: list[Run]) -> GateLine:
    reads, details = 0, []
    for run in runs:
        code = _code(run)
        if code is None:
            outcome, reason = classify(run)
            details.append(f"no test: {run.label} ({outcome}{': ' + reason if reason else ''})")
            continue
        shapes = {f.shape for f in check_pass_quality(code, run.row.get("query") or "")}
        if READ_NOT_PERFORMED in shapes:
            details.append(f"no read: {run.label}")
        else:
            reads += 1
            details.append(f"reads: {run.label}")
    status = "PASS" if reads >= Q10_ADDON_MIN_READS else "FAIL"
    return GateLine("Q10 READS", status, f"{reads}/{len(runs)} runs read the titles "
                    f"(acceptance >= {Q10_ADDON_MIN_READS}/{Q10_ADDON_RUNS})", details)
