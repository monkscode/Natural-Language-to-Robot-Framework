"""Bench gate, part 1: what is compared — inputs, refusals, provider misses, re-runs, pass rate.

The gate compares ONE candidate bench CSV against ONE baseline (BASELINE_CSV; the
owner's rule, 2026-09-25). Before any number is computed it refuses (GateRefused,
exit 2) when the comparison would be meaningless:
- a pin differs from the baseline's `.meta.json` (a model or provider change
  means a new baseline is needed; an optimization/dryrun change means the run was
  not a bench run), or the candidate has no `.meta.json`;
- the candidate's queries differ from the baseline's (a query missing, an extra
  one, a different repeat count, or a changed wording);
- a row has no workflow id, two rows share one, or a row's `bench/runs/<id>/`
  capture is missing (or a passed run has no `artifacts/test.robot`) — the gate
  never passes what it could not read.

Provider misses. A run that ended because the model provider did not answer is
not a framework failure. It is classified ONLY from the generation error the run
recorded in `bench/runs/<id>/test_runs.json` (`error_message`) — never from W5's
counters, because a failed generation writes NO workflow_metrics row
(9c983e90-4b50-48a0-b255-fefdde5f584f has an empty list). Only a TEMPORARY
provider failure is a miss (owner, 2026-09-28): 429, 5xx / ServiceUnavailable, a
timeout or a connection error. A miss is a row whose generation_status is "error"
and whose message starts with one of PROVIDER_MISS_PREFIXES (the W5 / W5.1
sentences, each a temporary cause) or whose FIRST named LiteLLM class is in
TEMPORARY_LITELLM_ERRORS. Anything else — a 400, any other status, a setup
error — stays a real failure.

Re-runs. run_bench.py has no subset flag, so for every query with a pending miss
the gate WRITES a one-query `--queries` file and prints the command. Re-run CSVs
are applied in the order given: each re-run row replaces the FIRST slot (in
candidate order) of the same query whose current row is a provider miss. A re-run
that misses on the provider again leaves the slot a provider miss (pending — it
may be re-run again); one that fails for any other reason makes it a real
failure; a green one makes it a pass. A re-run row with no provider-miss slot to
replace is refused: real failures are never re-rolled. So is a re-run CSV recorded
on other code (check_same_code: its `.meta.json` git_sha must equal the
candidate's).

Pass rate: passes / slots, rounded to one decimal as bench/report.py does (29/30
reads 96.7% and passes the 96.7% gate). FAIL when even every pending miss passing
could not reach the gate; PENDING while provider misses await a re-run; else
PASS or FAIL.

Referenced by: bench/gate.py, bench/gate_checks.py, bench/gate_hollow.py.
Depends on: bench/bench_lib.py (compare_pins, load_meta), csv, json, re, pathlib.
(Its tests also import src/backend/core/provider_errors.py to build the real
sentences; this module does not — it must not import litellm.)
"""

import csv
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from bench.bench_lib import compare_pins, load_meta

BASELINE_CSV = "bench/baselines/2026-09-24-develop-b57b683-bs-1.0.39.csv"
TIMING_REFERENCE_CSV = "bench/baselines/2026-08-02-rule6-url-backstop.csv"
PASS_RATE_GATE = 96.7

REQUIRED_COLUMNS = ("query_id", "query", "repeat", "workflow_id", "started_at",
                    "generation_status", "test_status", "total_s",
                    "locator_success_rate", "flake_retries", "dryrun_repairs")
CAPTURE_FILES = ("test_runs.json", "llm_traces.json", "workflow_metrics.json")

# The sentences src/backend/core/provider_errors.py records when the model provider
# gave up on a run for a temporary reason. Matched by prefix, so a sentence's
# per-provider tail cannot break the match; tests/test_bench/test_gate_inputs.py
# builds each one with provider_errors itself, so a rewording fails a test.
PROVIDER_MISS_PREFIXES = (
    "The model provider did not answer.",                 # W5: timeout, 503 or 500 (cloud)
    "Your local model did not answer.",                   # W5: timeout, 503 or 500 (Ollama)
    "The model provider rate-limited this request",       # 429: gemini and any other provider
    "Vertex AI is out of capacity",                       # 429: vertex (W5.1 Task 2b, ba1242d)
    "The model provider's daily quota for this model is used up",  # per-day 429
)
# A raw message counts only when it STARTS "An error occurred: litellm.<Class>: ..." (or
# "litellm.<Class>: ...") and that first class is temporary; a class named later in the
# text does not count: "litellm.ContextWindowExceededError: litellm.BadRequestError: ..."
# is a 400. LiteLLM 1.75.3 maps a Vertex/Gemini 429 to RateLimitError, 503 to
# ServiceUnavailableError, 500 to InternalServerError, 408 to Timeout and every
# status it cannot map (502, 504, ...) to APIConnectionError; 400/403 become
# BadRequestError, 401 AuthenticationError, 404 NotFoundError.
TEMPORARY_LITELLM_ERRORS = frozenset({"RateLimitError", "ServiceUnavailableError",
                                      "InternalServerError", "Timeout", "APIConnectionError"})
_LITELLM_CLASS = re.compile(r"(?:An error occurred: )?litellm\.(?:exceptions\.)?(Timeout|[A-Z][A-Za-z]*Error)\b")
_MODEL_PIN_KEYS = ("model_provider", "online_model", "browser_service.model_provider")

PASSED = "passed"
PROVIDER_MISS = "provider_miss"
FAILED = "failed"


class GateRefused(Exception):
    """The candidate cannot be compared with the baseline; the message says why."""


@dataclass
class GateLine:
    """One line of the gate report. status: PASS FAIL PENDING CANNOT INFO."""

    name: str
    status: str
    text: str
    details: list[str] = field(default_factory=list)


@dataclass
class Run:
    """One CSV row and where its capture lives."""

    row: dict
    source: str
    capture: Path

    @property
    def workflow_id(self) -> str:
        return (self.row.get("workflow_id") or "").strip()

    @property
    def query_id(self) -> str:
        return (self.row.get("query_id") or "?").strip()

    @property
    def label(self) -> str:
        return f"{self.query_id} r{self.row.get('repeat') or '?'} {self.workflow_id or '(no workflow id)'}"


@dataclass
class Slot:
    """One candidate row, plus every re-run applied to it, newest last."""

    history: list[Run]
    outcome: str = ""
    reason: str = ""

    @property
    def current(self) -> Run:
        return self.history[-1]


def read_csv(path: Path) -> list[dict]:
    """Rows of a bench CSV; refuses a file missing any REQUIRED_COLUMNS.

    utf-8-sig: a CSV re-saved by a spreadsheet starts with a BOM, which would
    otherwise glue itself to the first column name and hide `query_id`.
    """
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
            header = reader.fieldnames or []
    except OSError as exc:
        raise GateRefused(f"cannot read {path}: {exc}") from exc
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise GateRefused(f"{path} lacks the columns {missing}")
    if not rows:
        raise GateRefused(f"{path} has no data rows")
    return rows


def check_pins(baseline_csv: Path, other_csv: Path) -> None:
    """Refuse unless `other_csv` was recorded under the baseline's pins."""
    base_meta, other_meta = load_meta(baseline_csv), load_meta(other_csv)
    if base_meta is None:
        raise GateRefused(f"the baseline {baseline_csv} has no .meta.json")
    if other_meta is None:
        raise GateRefused(f"{other_csv} has no .meta.json, so its pins cannot be checked "
                          f"against the baseline")
    mismatches = compare_pins(base_meta, other_meta)
    if not mismatches:
        return
    if any(m.startswith(_MODEL_PIN_KEYS) for m in mismatches):
        raise GateRefused(f"{other_csv}: the model or provider differs from the baseline — "
                          f"a new baseline is needed, never a comparison: {'; '.join(mismatches)}")
    raise GateRefused(f"{other_csv} was not run with the bench pins: {'; '.join(mismatches)}")


def check_same_code(candidate_csv: Path, rerun_csv: Path) -> None:
    """Refuse a re-run unless it was recorded on the candidate's code.

    Both `.meta.json` files must carry the same non-empty `git_sha` (bench_lib.build_meta
    records it, or None when git cannot answer): a re-run on other code would replace a
    candidate's provider miss with another commit's result.
    """
    cand_sha = (load_meta(candidate_csv) or {}).get("git_sha") or ""
    rerun_sha = (load_meta(rerun_csv) or {}).get("git_sha") or ""
    if not cand_sha or cand_sha != rerun_sha:
        raise GateRefused(f"{rerun_csv}: re-run recorded at {rerun_sha or 'no git_sha'}, candidate at "
                          f"{cand_sha or 'no git_sha'} — re-run on the candidate's code")


def check_same_queries(base_rows: list[dict], cand_rows: list[dict], cand_path: Path) -> None:
    """Refuse unless the candidate ran the baseline's queries, wording and repeat count."""
    base_n = Counter(r["query_id"] for r in base_rows)
    cand_n = Counter(r["query_id"] for r in cand_rows)
    base_text = {r["query_id"]: r["query"] for r in base_rows}
    problems = []
    missing = sorted(set(base_n) - set(cand_n))
    extra = sorted(set(cand_n) - set(base_n))
    if missing:
        problems.append(f"missing queries {missing}")
    if extra:
        problems.append(f"extra queries {extra}")
    for qid in sorted(set(base_n) & set(cand_n)):
        if base_n[qid] != cand_n[qid]:
            problems.append(f"{qid} has {cand_n[qid]} rows, the baseline {base_n[qid]}")
    changed = sorted({r["query_id"] for r in cand_rows
                      if r["query_id"] in base_text and r["query"] != base_text[r["query_id"]]})
    if changed:
        problems.append(f"query wording differs from the baseline for {changed}")
    if problems:
        raise GateRefused(f"{cand_path} did not run the baseline's queries: {'; '.join(problems)}")


def check_captures(runs: list[Run]) -> None:
    """Refuse on a missing/duplicate workflow id or an incomplete bench/runs capture."""
    problems = []
    seen: dict[str, str] = {}
    for run in runs:
        wf = run.workflow_id
        if not wf:
            problems.append(f"{run.source}: {run.label} — the row has no workflow id")
            continue
        if wf in seen:
            problems.append(f"{wf} appears twice ({seen[wf]} and {run.source})")
            continue
        seen[wf] = run.source
        if not run.capture.is_dir():
            problems.append(f"{run.label}: no capture folder {run.capture}")
            continue
        absent = [f for f in CAPTURE_FILES if not (run.capture / f).exists()]
        if run.row.get("test_status") == PASSED and not (run.capture / "artifacts" / "test.robot").exists():
            absent.append("artifacts/test.robot")
        if absent:
            problems.append(f"{run.label}: capture lacks {absent}")
    if problems:
        raise GateRefused("the gate cannot read every run, so it will not pass any:\n    "
                          + "\n    ".join(problems))


def load_runs(csv_path: Path, runs_dir: Path) -> list[Run]:
    return [Run(row, str(csv_path), runs_dir / (row.get("workflow_id") or "").strip())
            for row in read_csv(csv_path)]


def error_message(run: Run) -> str:
    """The run's recorded test_runs.error_message ('' when none)."""
    try:
        rows = json.loads((run.capture / "test_runs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(rows, list) or not rows:
        return ""
    row = next((r for r in rows if r.get("run_id") == run.workflow_id), rows[0])
    return (row.get("error_message") or "").strip()


def is_provider_message(message: str) -> bool:
    """True for a temporary provider failure (see PROVIDER_MISS_PREFIXES, TEMPORARY_LITELLM_ERRORS)."""
    if message.startswith(PROVIDER_MISS_PREFIXES):
        return True
    raw = _LITELLM_CLASS.match(message)
    return raw is not None and raw.group(1) in TEMPORARY_LITELLM_ERRORS


def classify(run: Run) -> tuple[str, str]:
    """(PASSED | PROVIDER_MISS | FAILED, reason) for one run."""
    if run.row.get("test_status") == PASSED:
        return PASSED, ""
    message = error_message(run)
    head = " ".join(message.split())[:140]
    if run.row.get("generation_status") == "error":
        if is_provider_message(message):
            return PROVIDER_MISS, head
        return FAILED, f"generation error: {head or 'no message recorded'}"
    return FAILED, (f"test_status={run.row.get('test_status') or '(empty)'}"
                    + (f": {head}" if head else ""))


def build_slots(cand_runs: list[Run], rerun_runs: list[Run]) -> list[Slot]:
    """Classify every candidate row, then apply the re-runs in order (see module doc)."""
    slots = [Slot([run]) for run in cand_runs]
    for slot in slots:
        slot.outcome, slot.reason = classify(slot.current)
    for rerun in rerun_runs:
        target = next((s for s in slots
                       if s.current.query_id == rerun.query_id and s.outcome == PROVIDER_MISS), None)
        if target is None:
            raise GateRefused(f"{rerun.source}: re-run {rerun.label} has no provider miss of "
                              f"{rerun.query_id} left to replace (a real failure is never re-run)")
        if rerun.row.get("query") != target.current.row.get("query"):
            raise GateRefused(f"{rerun.source}: re-run {rerun.label} ran a different wording of "
                              f"{rerun.query_id}")
        target.history.append(rerun)
        target.outcome, target.reason = classify(rerun)
    return slots


def pass_rate_line(slots: list[Slot]) -> GateLine:
    n = len(slots)
    passes = sum(s.outcome == PASSED for s in slots)
    pending = [s for s in slots if s.outcome == PROVIDER_MISS]
    failed = [s for s in slots if s.outcome == FAILED]
    rate = round(passes / n * 100, 1)
    best = round((passes + len(pending)) / n * 100, 1)
    details = [f"failed: {s.current.label} — {s.reason}" for s in failed]
    details += [f"provider miss (awaits a re-run): {s.current.label} — {s.reason}" for s in pending]
    details += [f"re-run: {' -> '.join(r.workflow_id for r in s.history)} ({s.outcome})"
                for s in slots if len(s.history) > 1]
    text = f"{passes}/{n} = {rate}% (gate >= {PASS_RATE_GATE}%)"
    if best < PASS_RATE_GATE:
        return GateLine("PASS RATE", "FAIL", text + (f"; even if the {len(pending)} provider "
                        f"miss(es) passed on a re-run it would be {best}%" if pending else ""), details)
    if pending:
        return GateLine("PASS RATE", "PENDING", text + f"; {len(pending)} provider miss(es) must be "
                        f"re-run first (up to {best}%)", details)
    return GateLine("PASS RATE", "PASS" if rate >= PASS_RATE_GATE else "FAIL", text, details)


def write_query_file(path: Path, query_id: str, query: str) -> Path:
    """A one-query --queries file for bench/run_bench.py."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"comment": f"One-query file written by bench/gate.py for {query_id}.",
               "queries": [{"id": query_id, "query": query}]}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def write_rerun_files(slots: list[Slot], rerun_dir: Path, candidate_csv: Path,
                      reruns_given: int) -> list[str]:
    """One one-query file per query with pending provider misses; returns the commands."""
    pending = Counter(s.current.query_id for s in slots if s.outcome == PROVIDER_MISS)
    commands = []
    for qid, count in sorted(pending.items()):
        text = next(s.current.row["query"] for s in slots
                    if s.current.query_id == qid and s.outcome == PROVIDER_MISS)
        path = write_query_file(rerun_dir / f"{qid}.json", qid, text)
        out = candidate_csv.with_name(f"{candidate_csv.stem}-rerun{reruns_given + 1}-{qid}.csv")
        commands.append(f"PYTHONPATH=. venv/Scripts/python.exe bench/run_bench.py "
                        f"--queries {path.as_posix()} --repeats {count} --out {out.as_posix()}")
    return commands
