"""bench/gate_inputs.py: refusals, provider-miss classification, re-runs, the pass-rate verdict."""

import csv
import json
import shutil
from pathlib import Path

import litellm
import pytest

from bench.gate_inputs import (
    FAILED,
    PASSED,
    PROVIDER_MISS,
    GateRefused,
    build_slots,
    check_captures,
    check_pins,
    check_same_queries,
    classify,
    is_provider_message,
    load_runs,
    pass_rate_line,
    read_csv,
    write_rerun_files,
)
from src.backend.core.provider_errors import friendly_provider_error, friendly_setup_error
from tests.test_bench.gate_fixtures import (
    PINS,
    PROVIDER_503,
    QUERIES,
    row,
    wf_id,
    write_bench,
    write_capture,
    write_csv,
)

W5_SENTENCES = [
    "The model provider did not answer. vertex_ai/gemini-3.5-flash gave no reply after 3 tries over 7 min 0 s. "
    "The problem is on the provider's side, not in your test description — try again in a few minutes.",
    "Your local model did not answer. ollama/llama3 gave no reply.",
    "The model provider rate-limited this request (quota exceeded). Wait a moment and try again. If it keeps "
    "happening, the free tier is likely too small for back-to-back runs — switch MODEL_PROVIDER to vertex in "
    "src/backend/.env, or request more quota.",
    "The model provider rate-limited this request (quota exceeded). Wait a moment and try again.",
    "Vertex AI is out of capacity for this model right now (HTTP 429). Wait a minute and try again. If it "
    "keeps happening, set VERTEXAI_LOCATION in src/backend/.env to global unless your data must stay in one "
    "region (Google then routes requests to whichever region has capacity).",
    "The model provider's daily quota for this model is used up, so retrying now will not help.",
    PROVIDER_503,
]
# "An error occurred: <str(e)>" for each temporary LiteLLM class (the 503 is PROVIDER_503 above).
TEMPORARY_RAW = [
    'An error occurred: litellm.RateLimitError: VertexAIException - {"error": {"code": 429, '
    '"status": "RESOURCE_EXHAUSTED"}}',
    'An error occurred: litellm.InternalServerError: VertexAIException - {"error": {"code": 500, '
    '"status": "INTERNAL"}}',
    "An error occurred: litellm.Timeout: Connection timed out after 60.0 seconds.",
    "An error occurred: litellm.APIConnectionError: VertexAIException - 504 Deadline Exceeded",
]


def _rate_limit() -> Exception:
    return litellm.RateLimitError(message='VertexAIException - {"error": {"code": 429}}',
                                  llm_provider="vertex_ai", model="gemini-3.5-flash")


def _per_day_quota() -> Exception:
    body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}
    return litellm.RateLimitError(message="VertexAIException - " + json.dumps(body),
                                  llm_provider="gemini", model="gemini-3.5-flash")


# Every sentence src/backend/core/provider_errors.py writes today, built by that module itself, so
# a rewording there fails here instead of silently turning provider misses into real failures
# (the first draft of this plan carried a Vertex prefix that PR #118's final wording no longer matched).
TEMPORARY_SENTENCES = {
    "vertex 429": lambda: friendly_setup_error(_rate_limit(), "vertex"),
    "gemini 429": lambda: friendly_setup_error(_rate_limit(), "gemini"),
    "other 429": lambda: friendly_setup_error(_rate_limit(), "local"),
    "daily quota": lambda: friendly_setup_error(_per_day_quota(), "gemini"),
    "cloud timeout": lambda: friendly_provider_error(litellm.Timeout(
        message="timed out", model="gemini-3.5-flash", llm_provider="vertex_ai")),
    "local timeout": lambda: friendly_provider_error(litellm.Timeout(
        message="timed out", model="llama3", llm_provider="ollama")),
}
SETUP_SENTENCES = {
    "AI Studio key": lambda: friendly_setup_error(Exception("API key not valid. Please pass a valid API key.")),
    "Vertex credentials": lambda: friendly_setup_error(Exception(
        "litellm.APIConnectionError: Unable to load vertex credentials from environment.")),
    "billing": lambda: friendly_setup_error(Exception("This API method requires billing to be enabled.")),
    "API disabled": lambda: friendly_setup_error(Exception('{"details": [{"reason": "SERVICE_DISABLED"}]}')),
    "IAM permission": lambda: friendly_setup_error(Exception("Permission 'aiplatform.endpoints.predict' denied")),
}


def meta_with(**nlrf) -> dict:
    meta = json.loads(json.dumps(PINS))
    meta["nlrf_pins"].update(nlrf)
    return meta


def gen_error(tmp_path: Path, message: str | None, qid: str = "q03", rep: int = 2):
    """A generation error: CSV gen=error, no workflow_metrics row, no test (the 2216fb03 / 9c983e90 shape)."""
    wf = wf_id("err", qid, rep)
    r = row(qid, rep, wf, test_status="", generation_status="error")
    capture = write_capture(tmp_path / "bench" / "runs", wf, code=None, error_message=message,
                            status="error", tokens={}, traces=[])
    return r, capture


class TestPins:
    def test_same_pins_pass(self, tmp_path):
        base, _ = write_bench(tmp_path, "base")
        cand, _ = write_bench(tmp_path, "cand")
        check_pins(base, cand)

    @pytest.mark.parametrize("meta", [
        meta_with(online_model="gemini-2.5-flash"),
        meta_with(model_provider="gemini"),
        {**PINS, "browser_service": {"model_provider": "gemini", "headless": True}},
    ])
    def test_a_model_or_provider_change_needs_a_new_baseline(self, tmp_path, meta):
        base, _ = write_bench(tmp_path, "base")
        cand, _ = write_bench(tmp_path, "cand", meta=meta)
        with pytest.raises(GateRefused, match="a new baseline is needed"):
            check_pins(base, cand)

    def test_an_unpinned_run_is_refused(self, tmp_path):
        base, _ = write_bench(tmp_path, "base")
        cand, _ = write_bench(tmp_path, "cand", meta=meta_with(optimization_enabled=True))
        with pytest.raises(GateRefused, match="not run with the bench pins"):
            check_pins(base, cand)

    def test_a_csv_without_meta_is_refused(self, tmp_path):
        base, _ = write_bench(tmp_path, "base")
        cand, _ = write_bench(tmp_path, "cand", meta=None)
        with pytest.raises(GateRefused, match="has no .meta.json"):
            check_pins(base, cand)


class TestSameQueries:
    def rows(self, drop=None, add=None, reword=None, extra_repeat=None):
        rows = [row(q, r, wf_id("x", q, r)) for q in QUERIES for r in (1, 2, 3)]
        if drop:
            rows = [r for r in rows if r["query_id"] != drop]
        if add:
            rows.append({**rows[0], "query_id": add, "query": "a new query"})
        if reword:
            rows[0] = {**rows[0], "query": rows[0]["query"] + " please"}
        if extra_repeat:
            rows.append({**rows[-1], "repeat": 4})
        return rows

    def test_identical_queries_pass(self):
        check_same_queries(self.rows(), self.rows(), Path("cand.csv"))

    @pytest.mark.parametrize("kwargs, message", [
        ({"drop": "q03"}, "missing queries \\['q03'\\]"),
        ({"add": "q11"}, "extra queries \\['q11'\\]"),
        ({"reword": True}, "wording differs from the baseline for \\['q01'\\]"),
        ({"extra_repeat": True}, "q10 has 4 rows, the baseline 3"),
    ])
    def test_a_different_query_set_is_refused(self, kwargs, message):
        with pytest.raises(GateRefused, match=message):
            check_same_queries(self.rows(), self.rows(**kwargs), Path("cand.csv"))


class TestCaptures:
    def test_complete_captures_pass(self, tmp_path):
        cand, _ = write_bench(tmp_path, "cand")
        check_captures(load_runs(cand, tmp_path / "bench" / "runs"))

    def test_a_missing_capture_folder_is_refused(self, tmp_path):
        cand, rows = write_bench(tmp_path, "cand")
        shutil.rmtree(tmp_path / "bench" / "runs" / rows[4]["workflow_id"])
        with pytest.raises(GateRefused, match=f"no capture folder .*{rows[4]['workflow_id']}"):
            check_captures(load_runs(cand, tmp_path / "bench" / "runs"))

    def test_a_passed_run_without_its_test_is_refused(self, tmp_path):
        cand, rows = write_bench(tmp_path, "cand")
        (tmp_path / "bench" / "runs" / rows[0]["workflow_id"] / "artifacts" / "test.robot").unlink()
        with pytest.raises(GateRefused, match="capture lacks \\['artifacts/test.robot'\\]"):
            check_captures(load_runs(cand, tmp_path / "bench" / "runs"))

    def test_a_missing_traces_file_is_refused(self, tmp_path):
        cand, rows = write_bench(tmp_path, "cand")
        (tmp_path / "bench" / "runs" / rows[0]["workflow_id"] / "llm_traces.json").unlink()
        with pytest.raises(GateRefused, match="capture lacks \\['llm_traces.json'\\]"):
            check_captures(load_runs(cand, tmp_path / "bench" / "runs"))

    def test_duplicate_workflow_ids_are_refused(self, tmp_path):
        _, rows = write_bench(tmp_path, "cand")
        rows[1]["workflow_id"] = rows[0]["workflow_id"]
        cand = write_csv(tmp_path / "dup.csv", rows)
        with pytest.raises(GateRefused, match=f"{rows[0]['workflow_id']} appears twice"):
            check_captures(load_runs(cand, tmp_path / "bench" / "runs"))

    def test_a_row_without_a_workflow_id_is_refused(self, tmp_path):
        _, rows = write_bench(tmp_path, "cand")
        rows[2] = {**rows[2], "workflow_id": "", "generation_status": "error", "test_status": ""}
        cand = write_csv(tmp_path / "noid.csv", rows)
        with pytest.raises(GateRefused, match="the row has no workflow id"):
            check_captures(load_runs(cand, tmp_path / "bench" / "runs"))


class TestReadCsv:
    def test_a_spreadsheet_saved_csv_with_a_bom_reads(self, tmp_path):
        cand, rows = write_bench(tmp_path, "cand")
        text = cand.read_text(encoding="utf-8")
        cand.write_text("\ufeff" + text, encoding="utf-8")
        assert [r["workflow_id"] for r in read_csv(cand)] == [r["workflow_id"] for r in rows]

    def test_a_csv_missing_a_required_column_is_refused(self, tmp_path):
        path = tmp_path / "old.csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows([["query_id", "query", "workflow_id"], ["q01", "x", "y"]])
        with pytest.raises(GateRefused, match="lacks the columns"):
            read_csv(path)


class TestClassify:
    @pytest.mark.parametrize("message", W5_SENTENCES)
    def test_provider_sentences_and_raw_vertex_errors_are_provider_misses(self, tmp_path, message):
        r, capture = gen_error(tmp_path, message)
        run = load_runs(write_csv(tmp_path / "c.csv", [r]), tmp_path / "bench" / "runs")[0]
        assert classify(run)[0] == PROVIDER_MISS

    @pytest.mark.parametrize("message", TEMPORARY_RAW)
    def test_temporary_raw_litellm_errors_are_provider_misses(self, tmp_path, message):
        r, capture = gen_error(tmp_path, message)
        run = load_runs(write_csv(tmp_path / "c.csv", [r]), tmp_path / "bench" / "runs")[0]
        assert classify(run)[0] == PROVIDER_MISS

    @pytest.mark.parametrize("build", list(TEMPORARY_SENTENCES.values()), ids=list(TEMPORARY_SENTENCES))
    def test_every_temporary_sentence_the_backend_writes_is_a_provider_miss(self, build):
        sentence = build()
        assert sentence and is_provider_message(sentence)

    @pytest.mark.parametrize("build", list(SETUP_SENTENCES.values()), ids=list(SETUP_SENTENCES))
    def test_every_setup_sentence_the_backend_writes_is_a_real_failure(self, build):
        sentence = build()
        assert sentence and not is_provider_message(sentence)

    @pytest.mark.parametrize("message", [
        "Failed to generate valid Robot Framework code: Expecting value: line 1 column 1 (char 0)",
        "Google AI Studio rejected the API key. Set a valid GEMINI_API_KEY in src/backend/.env",
        None,
        # the owner's rule: only temporary failures are misses; a 400 is a real failure
        'An error occurred: litellm.BadRequestError: VertexAIException BadRequestError - {"error": '
        '{"code": 400, "message": "Invalid JSON payload received.", "status": "INVALID_ARGUMENT"}}',
        "An error occurred: litellm.ContextWindowExceededError: litellm.BadRequestError: "
        "VertexAIException - 400 Request payload size exceeds the limit",
        'An error occurred: litellm.NotFoundError: VertexAIException - {"error": {"code": 404, '
        '"status": "NOT_FOUND"}}',
        'An error occurred: VertexAIException - {"error": {"code": 400, "status": "INVALID_ARGUMENT"}}',
        # the FIRST class named decides: a 400 whose text mentions an earlier timeout stays a 400
        "An error occurred: litellm.BadRequestError: VertexAIException - bad request (an earlier try "
        "hit litellm.Timeout)",
        # a temporary class named only in the tail is not the error's class (PR #119 review)
        'An error occurred: VertexAIException - {"error": {"code": 400, "status": "INVALID_ARGUMENT"}} '
        '(an earlier try hit litellm.Timeout)',
        "Failed to generate valid Robot Framework code: upstream said litellm.RateLimitError once",
    ])
    def test_other_generation_errors_stay_real_failures(self, tmp_path, message):
        r, capture = gen_error(tmp_path, message)
        run = load_runs(write_csv(tmp_path / "c.csv", [r]), tmp_path / "bench" / "runs")[0]
        outcome, reason = classify(run)
        assert outcome == FAILED and reason.startswith("generation error")

    def test_an_execution_error_without_output_xml_is_a_real_failure(self, tmp_path):
        # the f045735c-ce25-4e71-b666-eb38a0bc354b shape: status=error, test.robot, no output.xml
        wf = wf_id("exec", "q02", 1)
        write_capture(tmp_path / "bench" / "runs", wf, status="error", error_message=None)
        r = row("q02", 1, wf, test_status="")
        run = load_runs(write_csv(tmp_path / "c.csv", [r]), tmp_path / "bench" / "runs")[0]
        assert classify(run) == (FAILED, "test_status=(empty)")

    def test_a_failed_test_quoting_litellm_is_not_a_provider_miss(self, tmp_path):
        wf = wf_id("f", "q02", 1)
        write_capture(tmp_path / "bench" / "runs", wf, status="failed",
                      error_message="Text 'litellm.RateLimitError' should be 'ok'")
        r = row("q02", 1, wf, test_status="failed")
        run = load_runs(write_csv(tmp_path / "c.csv", [r]), tmp_path / "bench" / "runs")[0]
        assert classify(run)[0] == FAILED

    def test_an_empty_test_runs_capture_is_a_real_failure(self, tmp_path):
        r, capture = gen_error(tmp_path, PROVIDER_503)
        (capture / "test_runs.json").write_text("[]", encoding="utf-8")
        run = load_runs(write_csv(tmp_path / "c.csv", [r]), tmp_path / "bench" / "runs")[0]
        assert classify(run) == (FAILED, "generation error: no message recorded")


def bench_with_misses(tmp_path, misses: dict) -> list:
    """The baseline-shaped bench with generation errors at {(qid, rep): message}."""
    over = {k: {"test_status": "", "generation_status": "error"} for k in misses}
    cap = {k: {"code": None, "error_message": m, "status": "error", "tokens": {}, "traces": []}
           for k, m in misses.items()}
    cand, _ = write_bench(tmp_path, "cand", overrides=over, capture_kw=cap)
    return load_runs(cand, tmp_path / "bench" / "runs")


def rerun_csv(tmp_path, name, qid, outcomes: list[str]) -> list:
    """A re-run CSV for one query; outcomes per row: 'passed' | 'provider' | 'failed'."""
    rows = []
    for i, outcome in enumerate(outcomes, start=1):
        wf = wf_id(name, qid, i)
        if outcome == "passed":
            rows.append(row(qid, i, wf))
            write_capture(tmp_path / "bench" / "runs", wf)
        elif outcome == "failed":
            rows.append(row(qid, i, wf, test_status="failed"))
            write_capture(tmp_path / "bench" / "runs", wf, status="failed", error_message="Text mismatch")
        else:
            rows.append(row(qid, i, wf, test_status="", generation_status="error"))
            write_capture(tmp_path / "bench" / "runs", wf, code=None, error_message=PROVIDER_503,
                          status="error", tokens={}, traces=[])
    return load_runs(write_csv(tmp_path / "bench" / "baselines" / f"{name}.csv", rows), tmp_path / "bench" / "runs")


class TestReruns:
    def test_a_green_rerun_replaces_the_miss(self, tmp_path):
        slots = build_slots(bench_with_misses(tmp_path, {("q03", 2): W5_SENTENCES[2]}),
                            rerun_csv(tmp_path, "r1", "q03", ["passed"]))
        assert sum(s.outcome == PASSED for s in slots) == 30
        assert pass_rate_line(slots).status == "PASS"

    def test_a_red_rerun_is_a_real_failure(self, tmp_path):
        slots = build_slots(bench_with_misses(tmp_path, {("q03", 2): W5_SENTENCES[2]}),
                            rerun_csv(tmp_path, "r1", "q03", ["failed"]))
        assert [s.outcome for s in slots if len(s.history) > 1] == [FAILED]
        assert pass_rate_line(slots).text.startswith("29/30 = 96.7%")

    def test_a_rerun_that_misses_again_stays_a_miss_and_may_be_rerun_again(self, tmp_path):
        cand = bench_with_misses(tmp_path, {("q10", 3): PROVIDER_503})
        first = rerun_csv(tmp_path, "r1", "q10", ["provider"])
        slots = build_slots(cand, first)
        assert pass_rate_line(slots).status == "PENDING"
        slots = build_slots(cand, first + rerun_csv(tmp_path, "r2", "q10", ["passed"]))
        target = next(s for s in slots if len(s.history) == 3)
        assert target.outcome == PASSED and pass_rate_line(slots).status == "PASS"

    def test_two_misses_of_one_query_take_rerun_rows_in_candidate_order(self, tmp_path):
        cand = bench_with_misses(tmp_path, {("q07", 1): PROVIDER_503, ("q07", 3): PROVIDER_503})
        slots = build_slots(cand, rerun_csv(tmp_path, "r1", "q07", ["passed", "failed"]))
        q07 = [s for s in slots if s.current.query_id == "q07"]
        assert [s.outcome for s in q07] == [PASSED, PASSED, FAILED]

    def test_a_rerun_of_a_real_failure_is_refused(self, tmp_path):
        cand, _ = write_bench(tmp_path, "cand", overrides={("q04", 3): {"test_status": "failed"}})
        with pytest.raises(GateRefused, match="no provider miss of q04 left to replace"):
            build_slots(load_runs(cand, tmp_path / "bench" / "runs"), rerun_csv(tmp_path, "r1", "q04", ["passed"]))

    def test_a_rerun_with_another_wording_is_refused(self, tmp_path):
        cand = bench_with_misses(tmp_path, {("q03", 2): PROVIDER_503})
        rerun = rerun_csv(tmp_path, "r1", "q03", ["passed"])
        rerun[0].row["query"] = "something else"
        with pytest.raises(GateRefused, match="ran a different wording of q03"):
            build_slots(cand, rerun)


class TestPassRate:
    def test_all_pass(self, tmp_path):
        assert pass_rate_line(build_slots(bench_with_misses(tmp_path, {}), [])).status == "PASS"

    def test_29_of_30_is_96_7_and_passes(self, tmp_path):
        cand, _ = write_bench(tmp_path, "cand", overrides={("q04", 3): {"test_status": "failed"}})
        line = pass_rate_line(build_slots(load_runs(cand, tmp_path / "bench" / "runs"), []))
        assert (line.status, line.text) == ("PASS", "29/30 = 96.7% (gate >= 96.7%)")

    def test_28_of_30_fails(self, tmp_path):
        over = {("q04", 3): {"test_status": "failed"}, ("q05", 1): {"test_status": "failed"}}
        cand, _ = write_bench(tmp_path, "cand", overrides=over)
        assert pass_rate_line(build_slots(load_runs(cand, tmp_path / "bench" / "runs"), [])).status == "FAIL"

    def test_provider_misses_are_pending_until_rerun(self, tmp_path):
        # the 2026-09-24-wrongel-k-d-run1 shape: 28 passes, q03 r2 rate-limited, q10 r3 raw 503
        slots = build_slots(bench_with_misses(tmp_path, {("q03", 2): W5_SENTENCES[2],
                                                         ("q10", 3): PROVIDER_503}), [])
        line = pass_rate_line(slots)
        assert line.status == "PENDING"
        assert "2 provider miss(es) must be re-run first (up to 100.0%)" in line.text

    def test_misses_that_cannot_rescue_the_rate_fail_now(self, tmp_path):
        over = {("q04", 3): {"test_status": "failed"}, ("q05", 1): {"test_status": "failed"},
                ("q06", 1): {"test_status": "failed"}}
        cap = {("q07", 2): {"code": None, "error_message": PROVIDER_503, "status": "error", "tokens": {},
                            "traces": []}}
        over[("q07", 2)] = {"test_status": "", "generation_status": "error"}
        cand, _ = write_bench(tmp_path, "cand", overrides=over, capture_kw=cap)
        line = pass_rate_line(build_slots(load_runs(cand, tmp_path / "bench" / "runs"), []))
        assert line.status == "FAIL" and "it would be 90.0%" in line.text


def test_rerun_files_are_one_query_files_with_the_right_repeat_count(tmp_path):
    cand = bench_with_misses(tmp_path, {("q07", 1): PROVIDER_503, ("q07", 3): PROVIDER_503,
                                        ("q10", 2): W5_SENTENCES[0]})
    commands = write_rerun_files(build_slots(cand, []), tmp_path / "reruns",
                                 Path("bench/baselines/cand.csv"), reruns_given=0)
    assert json.loads((tmp_path / "reruns" / "q07.json").read_text(encoding="utf-8"))["queries"] == [
        {"id": "q07", "query": QUERIES["q07"]}]
    assert [c.split("--repeats ")[1].split(" ")[0] for c in commands] == ["2", "1"]
    assert commands[0].endswith("--out bench/baselines/cand-rerun1-q07.csv")
