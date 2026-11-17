"""Held-out sweep report: totals per half, per-query lines for the dev half only.

Usage (from the repo root):

    python -m bench.sweep_report <out_dir>

Reads <out_dir>/results.jsonl. The sealed half is shown as counts only: no query
id, no query text, no error message, no workflow id.

Referenced by: run by hand after a sweep (`python -m bench.sweep_report <out_dir>`).
Depends on: the results.jsonl rows written by bench/sweep.py.
"""

import json
import math
import sys
from collections import Counter
from pathlib import Path

Z = 1.959964  # 95%
HALVES = ("dev", "sealed")


def wilson(k: int, n: int) -> tuple[float, float]:
    """Wilson 95% interval for k successes in n trials; (0.0, 0.0) when n == 0."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + Z * Z / n
    centre = (p + Z * Z / (2 * n)) / d
    half = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def _count(rows: list[dict]) -> tuple[int, int]:
    return (sum(r["outcome"] == "handled" for r in rows), len(rows))


def summarize(rows: list[dict]) -> dict:
    """Per half: counts, cold/warm/run-1 splits, outcome/tag/hollow tallies.

    "max_repeat" is the highest repeat in the rows (1 when there are none). Only the dev half
    carries "per_query" (outcomes of each repeat by query id).
    """
    out: dict = {"max_repeat": max((r["repeat"] for r in rows), default=1)}
    for half in HALVES:
        hr = [r for r in rows if r["half"] == half]
        run1 = [r for r in hr if r["repeat"] == 1]
        out[half] = {
            "runs": len(hr),
            "handled": _count(hr)[0],
            "cold": _count(run1),
            "warm": _count([r for r in hr if r["repeat"] != 1]),
            # Runs 2-3 depend on run 1 under learning, so run 1 is the one-per-query sample.
            "run1_queries": _count(run1),
            "outcomes": dict(Counter(r["outcome"] for r in hr)),
            "tags": dict(Counter(r["tag"] for r in hr if r["tag"])),
            "hollow": dict(Counter(h for r in hr for h in r["hollow"])),
        }
    per_query: dict[str, dict[int, str]] = {}
    for r in rows:
        if r["half"] == "dev":
            per_query.setdefault(r["query_id"], {})[r["repeat"]] = r["outcome"]
    out["dev"]["per_query"] = per_query
    return out


def _line(label: str, k: int, n: int, unit: str = "") -> str:
    if n == 0:
        return f"  {label}: 0 of 0{unit}, n=0"
    lo, hi = wilson(k, n)
    return f"  {label}: {k} of {n}{unit} ({k / n:.1%}), Wilson 95% {lo:.3f}-{hi:.3f}"


def render(summary: dict) -> str:
    lines: list[str] = []
    top = summary["max_repeat"]
    for half in HALVES:
        s = summary[half]
        lines.append(f"== {half} half ==")
        lines.append(_line("handled / runs", s["handled"], s["runs"]))
        lines.append(_line("run 1 (cold) runs", *s["cold"]))
        if top >= 2:  # one repeat has no warm runs; "2-1" would be an inverted range
            lines.append(_line(f"runs 2-{top} (after learning)", *s["warm"]))
        lines.append(_line("handled on run 1", *s["run1_queries"], unit=" queries"))
        lines.append("  outcomes: " + (", ".join(f"{o}={c}" for o, c in sorted(s["outcomes"].items())) or "none"))
        lines.append("  tags: " + (", ".join(f"{t}={c}" for t, c in sorted(s["tags"].items())) or "none"))
        lines.append("  hollow: " + (", ".join(f"{h}={c}" for h, c in sorted(s["hollow"].items())) or "none"))
        lines.append("")
    lines.append("== dev half, per query ==")
    for qid, by_repeat in sorted(summary["dev"]["per_query"].items()):
        lines.append(f"  {qid}: " + " ".join(by_repeat.get(i, "-") for i in range(1, top + 1)))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m bench.sweep_report <out_dir>", file=sys.stderr)
        return 2
    path = Path(args[0]) / "results.jsonl"
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    print(render(summarize(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
