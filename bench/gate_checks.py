"""Bench gate, part 2: locator success, flake, salvage, tokens, cost/cache, the 429 window.

Gates (each a GateLine; PASS / FAIL / CANNOT):
- LOCATOR: every generated run (generation_status == complete) has
  locator_success_rate == 1.0. A generated run with no value cannot be checked.
- FLAKE: flake_retries - dryrun_repairs == 0 on every generated run (per run, so a
  negative cannot cancel a positive).
- SALVAGE: 0 salvage rows, counted by PROMPT SHAPE in every run's llm_traces.json
  (replaced provider misses included): the first message is crewai's Converter
  instruction, "Ensure your final answer ..." (crewai 1.8.1) or "Format your final
  answer ..." (crewai 1.15), sent either as instructor's single user message
  "SYSTEM: <instruction>\n\nUSER: <text>" or as the plain / post-W5.1 routed pair
  [system: <instruction>, user: <text>]. Also 0 unparsed planner answers: the
  planner row is chosen BY PROMPT (first message = the Test Automation Planner
  system prompt), rows ordered by start_time_ns (the capture has no ORDER BY), the
  last planner row with an answer is the one crewai converted, and it is unparsed
  when neither it nor its greedy {...} span is a JSON object with a "steps" list
  (crewai 1.8.1's own extraction — exactly what reaches the Converter: over all
  2,105 captures that leaves the 7 known 2026-08-01 salvage runs). Until W5.1
  ships, a 1.8.1 salvage row carries no workflow id, so the bench never captures
  it; an unparsed planner answer is then the only trace of it. A GENERATED run
  with no planner row or answer makes the line CANNOT, naming the run (never
  passed blind; owner, 2026-09-29); a generation error without one is only counted.
- TOKENS, four families gated SEPARATELY from each run's workflow_metrics.json
  (the CSV carries only crewai + browser-use sums): the SUM of per-query medians,
  candidate vs baseline, must not grow more than crewai prompt +10%, crewai
  completion +15%, browser-use prompt +5%, browser-use completion +15% (~3x the
  same-code noise 3.2 / 4.7 / 0.5 / 4.3% measured on 3 same-code pairs). A query
  whose baseline median is 0, or that has no value on one side, is listed and left
  out of the sum. So is a CANDIDATE median of 0 against a non-zero baseline: a
  zeroed token accumulator, never a saving (owner, 2026-09-29; the false pass
  bench/run_bench.py:warn_if_zero_crewai_tokens warns of).
  So is a query whose OWN baseline runs spread more than 25%
  (max > 1.25 x min; owner, 2026-09-28): it cannot anchor a +5% sum (q01's
  browser-use prompt swings 22,848 -> 35,712 between baseline runs). Any single
  query moving more than 25% either way is LISTED, never
  gated (q10 moved +52.7% on the same code).
REPORTED only (INFO): dollars (llm_cost_usd), the browser-use cache share, the
median paired_pct of llm_tokens (bench_lib.compare_by_query), and the 429 window:
429 / 503 signature lines in logs/application.log.1 + application.log (the log
rotates mid-bench) inside the bench's UTC window. Never a bare "429": timestamps
and token counts contain it. The window comes from the CSV's local started_at plus
total_s. The 429 count says whether TIMING can be trusted; timing is not gated.

Referenced by: bench/gate.py.
Depends on: bench/gate_inputs.py (GateLine, Run, Slot), bench/bench_lib.py (coerce,
compare_by_query), json, re, statistics, datetime.
"""

import json
import re
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

from bench.bench_lib import coerce, compare_by_query
from bench.gate_inputs import GateLine, Run, Slot

TOKEN_FAMILIES = (  # (label, workflow_metrics data key, limit %)
    ("crewai prompt", "crewai_prompt_tokens", 10.0),
    ("crewai completion", "crewai_completion_tokens", 15.0),
    ("browser-use prompt", "browser_use_prompt_tokens", 5.0),
    ("browser-use completion", "browser_use_completion_tokens", 15.0),
)
LISTED_MOVE_PCT = 25.0
JUMPY_SPREAD_PCT = 25.0   # a query's own baseline runs spread more than this -> listed, not summed
PLANNER_PROMPT = "You are Test Automation Planner"
_CONVERTER_INSTRUCTIONS = {"Ensure your final answer": "1.8.1 wording",
                           "Format your final answer": "1.15 wording"}
_JSON_SPAN = re.compile(r"({.*})", re.DOTALL)   # crewai 1.8.1 converter._JSON_PATTERN
_TIMESTAMP = re.compile(r'"timestamp": "(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})')
SIGNATURE_429 = re.compile(r'429 Too Many|code\\?": 429|RateLimitError|RESOURCE_EXHAUSTED')
SIGNATURE_503 = re.compile(r'503 Service|code\\?": 503|ServiceUnavailable')
LOG_FILES = ("application.log.1", "application.log")


def _generated(slot: Slot) -> bool:
    return slot.current.row.get("generation_status") == "complete"


def _generated_run(run: Run) -> bool:
    return (run.row.get("generation_status") or "").strip() == "complete"


def locator_line(slots: list[Slot]) -> GateLine:
    gen = [s for s in slots if _generated(s)]
    below = [s for s in gen if coerce(s.current.row.get("locator_success_rate")) not in (None, 1.0)]
    blank = [s for s in gen if coerce(s.current.row.get("locator_success_rate")) is None]
    details = [f"below 1.0: {s.current.label} = {s.current.row.get('locator_success_rate')}" for s in below]
    details += [f"not measured: {s.current.label}" for s in blank]
    text = f"{len(gen) - len(below) - len(blank)}/{len(gen)} generated runs at locator_success_rate 1.0"
    status = "FAIL" if below else ("CANNOT" if blank else "PASS")
    return GateLine("LOCATOR", status, text, details)


def flake_line(slots: list[Slot]) -> GateLine:
    gen = [s for s in slots if _generated(s)]
    bad, blank = [], []
    for s in gen:
        flake = coerce(s.current.row.get("flake_retries"))
        repairs = coerce(s.current.row.get("dryrun_repairs")) or 0
        if flake is None:
            blank.append(s)
        elif flake - repairs != 0:
            bad.append((s, flake - repairs))
    details = [f"{s.current.label}: flake_retries - dryrun_repairs = {d:g}" for s, d in bad]
    details += [f"not measured: {s.current.label}" for s in blank]
    text = f"flake_retries - dryrun_repairs == 0 on {len(gen) - len(bad) - len(blank)}/{len(gen)} generated runs"
    return GateLine("FLAKE", "FAIL" if bad else ("CANNOT" if blank else "PASS"), text, details)


def _load_list(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _first_message(prompt_text: str | None) -> tuple[str, str] | None:
    try:
        messages = json.loads(prompt_text or "")
    except ValueError:
        return None
    if not isinstance(messages, list) or not messages or not isinstance(messages[0], dict):
        return None
    content = messages[0].get("content")
    return (str(messages[0].get("role") or ""), content) if isinstance(content, str) else None


def salvage_shape(prompt_text: str | None) -> str | None:
    """The salvage shape of one llm_traces row's prompt, or None for any other call."""
    first = _first_message(prompt_text)
    if first is None:
        return None
    role, content = first
    if role == "user" and content.startswith("SYSTEM: "):
        body, form = content[len("SYSTEM: "):], "instructor"
    elif role == "system":
        body, form = content, "plain Converter / post-W5.1 routed"
    else:
        return None
    for head, wording in _CONVERTER_INSTRUCTIONS.items():
        if body.startswith(head):
            return f"{form} ({wording})"
    return None


def _ordered(traces: list) -> list[dict]:
    rows = [t for t in traces if isinstance(t, dict)]
    return sorted(rows, key=lambda t: (coerce(t.get("start_time_ns")) is None,
                                       coerce(t.get("start_time_ns")) or 0))


def _is_planner(trace: dict) -> bool:
    first = _first_message(trace.get("prompt_text"))
    return first is not None and first[0] == "system" and first[1].startswith(PLANNER_PROMPT)


def _steps_object(text: str, *, strict: bool = True) -> bool:
    try:
        answer = json.loads(text, strict=strict)
    except ValueError:
        return False
    return isinstance(answer, dict) and isinstance(answer.get("steps"), list)


def planner_state(traces: list) -> str:
    """'parsed' | 'UNPARSED' | 'no planner answer' | 'no planner row' (see module doc).

    'parsed' mirrors crewai 1.8.1's own parse (converter.convert_to_model, then
    handle_partial_json): the whole answer is JSON under json.loads(strict=False),
    which takes a raw control character inside a string, or its greedy {...} span
    is strict JSON — crewai validates the span with pydantic model_validate_json,
    which rejects one (a pre-schema "Final Answer: {...}" answer never reached the
    Converter). UNPARSED is exactly what reaches it.
    """
    planner = [t for t in _ordered(traces) if _is_planner(t)]
    if not planner:
        return "no planner row"
    answered = [t for t in planner if (t.get("response_text") or "").strip()]
    if not answered:
        return "no planner answer"
    text = answered[-1]["response_text"]
    span = _JSON_SPAN.search(text)
    parsed = _steps_object(text, strict=False) or (span and _steps_object(span.group(1)))
    return "parsed" if parsed else "UNPARSED"


def salvage_line(slots: list[Slot]) -> GateLine:
    runs = [run for s in slots for run in s.history]
    shapes: Counter = Counter()
    details, unparsed, no_answer = [], [], []
    for run in runs:
        traces = _load_list(run.capture / "llm_traces.json")
        for t in _ordered(traces):
            shape = salvage_shape(t.get("prompt_text"))
            if shape:
                shapes[shape] += 1
                details.append(f"salvage row {t.get('id')} [{shape}] in {run.label}")
        state = planner_state(traces)
        if state == "UNPARSED":
            unparsed.append(run)
            details.append(f"unparsed planner answer in {run.label}")
        elif state != "parsed":
            no_answer.append((run, state))
    # a generated run whose planner answer the capture lost is never passed blind; a generation
    # error (e.g. a provider miss, whose traces are often []) without one is informational
    blind = [(run, state) for run, state in no_answer if _generated_run(run)]
    details += [f"{state} in {run.label}" for run, state in blind]
    n_rows = sum(shapes.values())
    text = (f"{n_rows} salvage-shaped rows, {len(unparsed)} unparsed planner answers over {len(runs)} runs "
            f"({len(blind)} generated runs with no planner answer, {len(no_answer) - len(blind)} other runs "
            f"without one)")
    if shapes:
        text += " — " + ", ".join(f"{k}: {v}" for k, v in sorted(shapes.items()))
    if n_rows or unparsed:
        status = "FAIL"
    else:
        status = "CANNOT" if blind else "PASS"
    return GateLine("SALVAGE", status, text, details)


def _metrics_data(run: Run) -> dict:
    rows = _load_list(run.capture / "workflow_metrics.json")
    if not rows or not isinstance(rows[0], dict):
        return {}
    data = rows[0].get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return {}
    return data if isinstance(data, dict) else {}


def _values(runs: list[Run], key: str) -> dict[str, list[float]]:
    groups: dict[str, list[float]] = {}
    for run in runs:
        value = coerce(_metrics_data(run).get(key))
        if value is not None:
            groups.setdefault(run.query_id, []).append(value)
    return groups


def _jumpy(values: list[float]) -> bool:
    """The runs spread more than JUMPY_SPREAD_PCT: max > 1.25 x min (multiplied, so exactly 25% is not)."""
    return len(values) > 1 and max(values) * 100 > min(values) * (100 + JUMPY_SPREAD_PCT)


def token_lines(base_runs: list[Run], cand_slots: list[Slot]) -> list[GateLine]:
    cand_runs = [s.current for s in cand_slots]
    lines = []
    for label, key, limit in TOKEN_FAMILIES:
        base_values = _values(base_runs, key)
        base = {q: statistics.median(v) for q, v in base_values.items()}
        cand = {q: statistics.median(v) for q, v in _values(cand_runs, key).items()}
        every = {r.query_id for r in base_runs} | {r.query_id for r in cand_runs}
        zero = sorted(q for q in base if base[q] == 0)
        jumpy = sorted(q for q in base if base[q] != 0 and _jumpy(base_values[q]))
        comparable = [q for q in base if q in cand and base[q] != 0 and q not in jumpy]
        cand_zero = sorted(q for q in comparable if cand[q] == 0)   # a zeroed accumulator is no saving
        paired = sorted(q for q in comparable if cand[q] != 0)
        unpaired = sorted(every - set(paired) - set(zero) - set(jumpy) - set(cand_zero))
        details = [f"baseline median 0, not in the sum: {q}" for q in zero]
        for q in jumpy:
            lo, hi = min(base_values[q]), max(base_values[q])
            spread = f"{(hi - lo) * 100 / lo:.1f}%" if lo else "from 0"   # one zeroed baseline run
            after = f"{cand[q]:,.0f}" if q in cand else "no value"
            details.append(f"baseline runs spread {spread} (more than {JUMPY_SPREAD_PCT:g}%), "
                           f"not in the sum: {q} {lo:,.0f}..{hi:,.0f} -> {after}")
        details += [f"candidate median 0, not in the sum: {q} {base[q]:,.0f} -> 0" for q in cand_zero]
        details += [f"no value on one side, not in the sum: {q}" for q in unpaired]
        if not paired:
            lines.append(GateLine(f"TOKENS {label}", "CANNOT", "no query has a value on both sides", details))
            continue
        s_base, s_cand = sum(base[q] for q in paired), sum(cand[q] for q in paired)
        pct = (s_cand - s_base) * 100 / s_base   # multiply first: +5% of an integer sum is exactly 5.0
        for q in paired:
            move = (cand[q] - base[q]) * 100 / base[q]
            if abs(move) > LISTED_MOVE_PCT:
                details.append(f"listed, not gated: {q} {base[q]:,.0f} -> {cand[q]:,.0f} ({move:+.1f}%)")
        text = (f"{pct:+.1f}% (limit +{limit:g}%): sum of per-query medians {s_base:,.0f} -> "
                f"{s_cand:,.0f} over {len(paired)} queries")
        lines.append(GateLine(f"TOKENS {label}", "PASS" if pct <= limit else "FAIL", text, details))
    return lines


def _sum_share(runs: list[Run]) -> float | None:
    cached = prompt = 0.0
    for run in runs:
        data = _metrics_data(run)
        c, p = coerce(data.get("browser_use_cached_tokens")), coerce(data.get("browser_use_prompt_tokens"))
        if c is not None and p:
            cached, prompt = cached + c, prompt + p
    return cached / prompt * 100 if prompt else None


def reported_cost_lines(base_runs: list[Run], cand_slots: list[Slot]) -> list[GateLine]:
    cand_runs = [s.current for s in cand_slots]
    base_rows, cand_rows = [r.row for r in base_runs], [r.row for r in cand_runs]

    def csv_medians(rows):
        groups: dict[str, list[float]] = {}
        for row in rows:
            v = coerce(row.get("llm_cost_usd"))
            if v is not None:
                groups.setdefault(row.get("query_id") or "?", []).append(v)
        return {q: statistics.median(v) for q, v in groups.items()}

    b, c = csv_medians(base_rows), csv_medians(cand_rows)
    both = sorted(set(b) & set(c))
    if both and sum(b[q] for q in both):
        sb, sc = sum(b[q] for q in both), sum(c[q] for q in both)
        cost = f"llm_cost_usd sum of per-query medians ${sb:.4f} -> ${sc:.4f} ({(sc - sb) / sb * 100:+.1f}%)"
    else:
        cost = "llm_cost_usd: no comparable values"
    share_b, share_c = _sum_share(base_runs), _sum_share(cand_runs)
    share = ("browser-use cache share "
             + (f"{share_b:.1f}%" if share_b is not None else "n/a") + " -> "
             + (f"{share_c:.1f}%" if share_c is not None else "n/a"))
    paired = compare_by_query(base_rows, cand_rows, ["llm_tokens"])["llm_tokens"]["paired_pct"]
    paired_text = f"llm_tokens median paired change {paired:+.1f}%" if paired is not None else \
        "llm_tokens median paired change n/a (no numeric llm_tokens pairs)"
    return [GateLine("COST", "INFO", f"{cost}; {share}; {paired_text} — reported, not gated")]


def _local_to_utc(text: str, local_tz: tzinfo | None) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=local_tz) if local_tz is not None else stamp.astimezone()
    return stamp.astimezone(timezone.utc)


def bench_window(runs: list[Run], local_tz: tzinfo | None = None) -> tuple[datetime, datetime] | None:
    """(start, end) in UTC: first started_at .. max(started_at + total_s)."""
    spans = []
    for run in runs:
        start = _local_to_utc(run.row.get("started_at") or "", local_tz)
        if start is not None:
            spans.append((start, start + timedelta(seconds=coerce(run.row.get("total_s")) or 0)))
    if not spans:
        return None
    return min(s for s, _ in spans), max(e for _, e in spans)


def window_line(runs: list[Run], logs_dir: Path, local_tz: tzinfo | None = None) -> GateLine:
    window = bench_window(runs, local_tz)
    if window is None:
        return GateLine("429 WINDOW", "INFO", "no started_at to build a window from — not checked")
    start, end = (w.strftime("%Y-%m-%dT%H:%M:%S") for w in window)
    files = [logs_dir / name for name in LOG_FILES if (logs_dir / name).exists()]
    if not files:
        return GateLine("429 WINDOW", "INFO", f"{start}Z..{end}Z: no application.log in {logs_dir} — not checked")
    earliest, in_window, n429, n503 = None, 0, 0, 0
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _TIMESTAMP.search(line)
                if not m:
                    continue
                stamp = m.group(1)
                earliest = stamp if earliest is None or stamp < earliest else earliest
                if start <= stamp <= end:
                    in_window += 1
                    n429 += bool(SIGNATURE_429.search(line))
                    n503 += bool(SIGNATURE_503.search(line))
    text = (f"{start}Z..{end}Z: {n429} lines match the 429 signature, {n503} the 503 signature, "
            f"{in_window} log lines in the window ({', '.join(f.name for f in files)})")
    if in_window == 0:
        text += " — EMPTY window: the count says nothing (wrong window or rotated away)"
    elif earliest is not None and earliest > start:
        text += f" — the logs start at {earliest}Z, after the window start: partly rotated away"
    return GateLine("429 WINDOW", "INFO", text + "; timing trust only, not gated")
