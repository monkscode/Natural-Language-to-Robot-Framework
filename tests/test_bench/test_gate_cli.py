"""bench/gate.py end to end: the report, the exit codes, re-run files, the q10 add-on, import hygiene."""

import io
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from bench.gate import exit_code, main, run_gate
from bench.gate_inputs import BASELINE_CSV, GateLine
from tests.test_bench.gate_fixtures import (
    COUNT_ONLY_Q10,
    GOOD_TESTS,
    PINS,
    PROVIDER_503,
    QUERIES,
    SELECT_BLIND_Q08,
    TOKENS,
    row,
    wf_id,
    write_bench,
    write_capture,
    write_csv,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_NAME = Path(BASELINE_CSV).stem


def gate(tmp_path, argv, capsys):
    """main() against a tmp checkout whose baseline is the synthetic one."""
    with patch("bench.gate.REPO_ROOT", tmp_path):
        code = main([str(a) for a in argv] + ["--logs-dir", str(tmp_path / "logs")])
    return code, capsys.readouterr().out


def test_exit_code_order():
    line = lambda status: GateLine("X", status, "")  # noqa: E731
    assert exit_code([line("PASS"), line("INFO")]) == 0
    assert exit_code([line("PASS"), line("FAIL"), line("PENDING")]) == 1
    assert exit_code([line("PASS"), line("PENDING")]) == 2
    assert exit_code([line("CANNOT")]) == 2
    assert exit_code([line("REFUSED")]) == 2


def test_the_baseline_gated_against_itself_passes_every_gate(tmp_path, capsys):
    base, _ = write_bench(tmp_path, BASE_NAME)
    code, out = gate(tmp_path, [base], capsys)
    assert code == 0, out
    for name in ("PINS/INPUTS", "PASS RATE", "LOCATOR", "FLAKE", "SALVAGE", "TOKENS crewai prompt",
                 "TOKENS crewai completion", "TOKENS browser-use prompt", "TOKENS browser-use completion",
                 "HOLLOW", "VERIFIED", "FRAGILE", "NOT CHECKED", "COST", "429 WINDOW"):
        assert sum(line.startswith(name + " ") for line in out.splitlines()) == 1, name
    assert out.splitlines()[-1].startswith("VERDICT") and out.rstrip().endswith("PASS (exit 0)")


def test_an_unregistered_hollow_pass_exits_1(tmp_path, capsys):
    write_bench(tmp_path, BASE_NAME)
    cand, _ = write_bench(tmp_path, "cand", codes={("q08", 2): SELECT_BLIND_Q08})
    code, out = gate(tmp_path, [cand], capsys)
    assert code == 1
    assert [line.split()[1] for line in out.splitlines() if line.startswith("HOLLOW ")] == ["FAIL"]


def test_a_model_change_is_refused_with_exit_2(tmp_path, capsys):
    write_bench(tmp_path, BASE_NAME)
    meta = json.loads(json.dumps(PINS))
    meta["nlrf_pins"]["online_model"] = "gemini-4-flash"
    cand, _ = write_bench(tmp_path, "cand", meta=meta)
    code, out = gate(tmp_path, [cand], capsys)
    assert code == 2 and "a new baseline is needed" in out and "REFUSED" in out


def test_a_missing_capture_folder_is_refused_not_passed(tmp_path, capsys):
    write_bench(tmp_path, BASE_NAME)
    _, rows = write_bench(tmp_path, "cand")
    rows[5]["workflow_id"] = "11111111-2222-4333-8444-555555555555"   # no bench/runs folder
    cand = write_csv(tmp_path / "bench" / "baselines" / "cand2.csv", rows)
    code, out = gate(tmp_path, [cand], capsys)
    assert code == 2 and "no capture folder" in out


def test_provider_misses_write_rerun_files_then_a_rerun_passes(tmp_path, capsys):
    write_bench(tmp_path, BASE_NAME)
    cand, _ = write_bench(
        tmp_path, "cand", overrides={("q10", 3): {"test_status": "", "generation_status": "error"}},
        capture_kw={("q10", 3): {"code": None, "error_message": PROVIDER_503, "status": "error",
                                 "tokens": {}, "traces": []}})
    code, out = gate(tmp_path, [cand, "--rerun-dir", tmp_path / "reruns"], capsys)
    assert code == 2 and "PENDING" in out
    assert json.loads((tmp_path / "reruns" / "q10.json").read_text(encoding="utf-8"))["queries"][0]["id"] == "q10"
    assert "--repeats 1 --out" in out and "cand-rerun1-q10.csv" in out
    wf = wf_id("rerun", "q10", 1)
    write_capture(tmp_path / "bench" / "runs", wf, code=GOOD_TESTS["q10"])
    rerun = write_csv(tmp_path / "bench" / "baselines" / "cand-rerun1-q10.csv", [row("q10", 1, wf)])
    code, out = gate(tmp_path, [cand, "--rerun", rerun], capsys)
    assert code == 0, out
    assert f"re-run: {wf_id('cand', 'q10', 3)} -> {wf} (passed)" in out


def pending_bench(tmp_path):
    write_bench(tmp_path, BASE_NAME)
    cand, _ = write_bench(
        tmp_path, "cand", overrides={("q10", 3): {"test_status": "", "generation_status": "error"}},
        capture_kw={("q10", 3): {"code": None, "error_message": PROVIDER_503, "status": "error",
                                 "tokens": {}, "traces": []}})
    wf = wf_id("rerun", "q10", 1)
    write_capture(tmp_path / "bench" / "runs", wf, code=GOOD_TESTS["q10"])
    return cand, wf


def test_a_rerun_recorded_under_another_model_is_refused(tmp_path, capsys):
    cand, wf = pending_bench(tmp_path)
    meta = json.loads(json.dumps(PINS))
    meta["nlrf_pins"]["model_provider"] = "gemini"
    rerun = write_csv(tmp_path / "bench" / "baselines" / "r.csv", [row("q10", 1, wf)], meta)
    code, out = gate(tmp_path, [cand, "--rerun", rerun], capsys)
    assert code == 2 and "a new baseline is needed" in out


def test_a_rerun_recorded_on_other_code_is_refused(tmp_path, capsys):
    cand, wf = pending_bench(tmp_path)
    other = "f" * 40
    rerun = write_csv(tmp_path / "bench" / "baselines" / "r.csv", [row("q10", 1, wf)], {**PINS, "git_sha": other})
    code, out = gate(tmp_path, [cand, "--rerun", rerun], capsys)
    assert code == 2 and "REFUSED" in out
    assert (f"{rerun}: re-run recorded at {other}, candidate at {PINS['git_sha']} — re-run on the "
            f"candidate's code") in out


def test_a_rerun_with_no_git_sha_is_refused(tmp_path, capsys):
    cand, wf = pending_bench(tmp_path)
    meta = {k: v for k, v in PINS.items() if k != "git_sha"}
    rerun = write_csv(tmp_path / "bench" / "baselines" / "r.csv", [row("q10", 1, wf)], meta)
    code, out = gate(tmp_path, [cand, "--rerun", rerun], capsys)
    assert code == 2 and "REFUSED" in out
    assert f"{rerun}: re-run recorded at no git_sha, candidate at {PINS['git_sha']}" in out


def tokens_line(out: str, family: str) -> list[str]:
    return next(line for line in out.splitlines() if line.startswith(f"TOKENS {family} ")).split()


def test_a_token_fail_waits_as_pending_while_a_provider_miss_awaits_its_rerun(tmp_path, capsys):
    """Tokens are the one gate a re-run can flip from FAIL to PASS, so a token FAIL waits with the pass rate."""
    write_bench(tmp_path, BASE_NAME)
    cap = {(q, r): {"tokens": {**TOKENS, "crewai_prompt_tokens": 8760}}   # +9.5% on nine queries
           for q in QUERIES if q != "q03" for r in (1, 2, 3)}
    cap[("q03", 2)] = {"tokens": {**TOKENS, "crewai_prompt_tokens": 12000}}
    cap[("q03", 3)] = {"code": None, "error_message": PROVIDER_503, "status": "error", "tokens": {}, "traces": []}
    cand, _ = write_bench(tmp_path, "cand", capture_kw=cap,
                          overrides={("q03", 3): {"test_status": "", "generation_status": "error"}})
    code, out = gate(tmp_path, [cand, "--rerun-dir", tmp_path / "reruns"], capsys)
    # q03's median is 10,000 while r3 is missing: 80,000 -> 88,840 = +11.1%, over the +10% limit
    assert code == 2, out
    assert tokens_line(out, "crewai prompt")[3:5] == ["PENDING", "+11.1%"]
    assert "- provisional: 1 provider miss(es) await a re-run" in out
    wf = wf_id("rerun", "q03", 1)
    write_capture(tmp_path / "bench" / "runs", wf)   # 8,000, like q03 r1
    rerun = write_csv(tmp_path / "bench" / "baselines" / "cand-rerun1-q03.csv", [row("q03", 1, wf)])
    code, out = gate(tmp_path, [cand, "--rerun", rerun], capsys)
    # q03's median is 8,000 again: 80,000 -> 86,840 = +8.6%
    assert code == 0, out
    assert tokens_line(out, "crewai prompt")[3:5] == ["PASS", "+8.6%"]
    assert "provisional" not in out


def test_the_same_rerun_file_given_twice_is_refused(tmp_path, capsys):
    cand, wf = pending_bench(tmp_path)
    rerun = write_csv(tmp_path / "bench" / "baselines" / "r.csv", [row("q10", 1, wf)])
    code, out = gate(tmp_path, [cand, "--rerun", rerun, "--rerun", rerun], capsys)
    assert code == 2 and f"{wf} appears twice" in out


def test_run_gate_takes_explicit_paths(tmp_path):
    base, _ = write_bench(tmp_path, "base")
    lines = run_gate(base, baseline_csv=base, runs_dir=tmp_path / "bench" / "runs", logs_dir=tmp_path)
    assert exit_code(lines) == 0


def q10_csv(tmp_path, reads: int, n: int = 10) -> Path:
    rows = []
    for i in range(1, n + 1):
        wf = wf_id("addon", "q10", i)
        write_capture(tmp_path / "bench" / "runs", wf, code=GOOD_TESTS["q10"] if i <= reads else COUNT_ONLY_Q10)
        rows.append(row("q10", i, wf))
    return write_csv(tmp_path / "bench" / "baselines" / "q10x10.csv", rows)


def test_q10_addon_exit_codes(tmp_path, capsys):
    write_bench(tmp_path, BASE_NAME)
    assert gate(tmp_path, ["--q10-addon", q10_csv(tmp_path, 6)], capsys)[0] == 0
    assert gate(tmp_path, ["--q10-addon", q10_csv(tmp_path, 5)], capsys)[0] == 1
    code, out = gate(tmp_path, ["--q10-addon", q10_csv(tmp_path, 6, n=9)], capsys)
    assert code == 2 and "exactly 10 runs" in out


def test_write_q10_queries_writes_the_baseline_wording(tmp_path, capsys):
    write_bench(tmp_path, BASE_NAME)
    target = tmp_path / "q10.json"
    code, _ = gate(tmp_path, ["--write-q10-queries", target], capsys)
    assert code == 0
    assert json.loads(target.read_text(encoding="utf-8"))["queries"] == [{"id": "q10", "query": QUERIES["q10"]}]


def test_a_cp1252_stdout_gets_utf8_and_the_same_exit_code(tmp_path, capsys):
    """A piped run on Windows writes cp1252; a failed run's page text must neither crash nor garble."""
    write_bench(tmp_path, BASE_NAME)
    message = "Text '日本語' not found — the page shows another language"
    cand, _ = write_bench(
        tmp_path, "cand", overrides={("q03", 2): {"test_status": "failed"}},
        capture_kw={("q03", 2): {"error_message": message, "status": "failed"}})
    expected, out = gate(tmp_path, [cand], capsys)
    assert message in out   # the message reaches the report (PASS RATE details)
    raw = io.BytesIO()
    cp1252 = io.TextIOWrapper(raw, encoding="cp1252")
    with patch("sys.stdout", cp1252), patch("bench.gate.REPO_ROOT", tmp_path):
        code = main([str(cand), "--logs-dir", str(tmp_path / "logs")])
    cp1252.flush()
    text = raw.getvalue().decode("utf-8")
    assert "—" in text and "日本語" in text and message in text
    assert code == expected == 0   # 29/30 = 96.7% still passes the gate


def test_importing_the_gate_loads_no_config_database_network_or_runner():
    probe = ("import sys; import bench.gate; "
             "bad = [m for m in ('src.backend.core.config', 'psycopg', 'requests', 'litellm', 'crewai', "
             "'bench.run_bench', 'httpx') if m in sys.modules]; print(bad)")
    out = subprocess.run([sys.executable, "-c", probe], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"
