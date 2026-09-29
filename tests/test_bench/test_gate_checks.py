"""bench/gate_checks.py: locator, flake, salvage (by prompt shape), token families, cost, the 429 window."""

import json
from datetime import timedelta, timezone

import pytest

from bench.gate_checks import (
    bench_window,
    flake_line,
    locator_line,
    planner_state,
    reported_cost_lines,
    salvage_line,
    salvage_shape,
    token_lines,
    window_line,
)
from bench.gate_inputs import PASSED, build_slots, load_runs
from tests.test_bench.gate_fixtures import (
    ASSEMBLER_ROW,
    PLANNER_ROW,
    PROVIDER_503,
    ROOT_ROW,
    TOKENS,
    row,
    wf_id,
    write_bench,
    write_capture,
    write_csv,
)

IST = timezone(timedelta(hours=5, minutes=30))


def slots_of(tmp_path, name="cand", **kw):
    cand, _ = write_bench(tmp_path, name, **kw)
    return build_slots(load_runs(cand, tmp_path / "bench" / "runs"), [])


def runs_of(tmp_path, name, **kw):
    path, _ = write_bench(tmp_path, name, **kw)
    return load_runs(path, tmp_path / "bench" / "runs")


def prompt(role: str, content: str) -> str:
    return json.dumps([{"role": role, "content": content}, {"role": "user", "content": "x"}])


INSTRUCTOR_1_8_1 = json.dumps([{"role": "user", "content":
                                "SYSTEM: Ensure your final answer strictly adheres to the following OpenAPI "
                                "schema: {...}\n\nUSER: Thought: I now can give a great answer"}])
INSTRUCTOR_1_15 = json.dumps([{"role": "user", "content":
                               "SYSTEM: Format your final answer according to the following OpenAPI schema: "
                               "{...}\n\nUSER: {\"steps\": ["}])
ROUTED = prompt("system", "Ensure your final answer strictly adheres to the following OpenAPI schema: {...}")
PLAIN_1_15 = prompt("system", "Format your final answer according to the following OpenAPI schema: {...}")


class TestLocatorAndFlake:
    def test_clean_bench_passes_both(self, tmp_path):
        slots = slots_of(tmp_path)
        assert locator_line(slots).status == "PASS" and flake_line(slots).status == "PASS"

    def test_a_run_below_1_fails_locator(self, tmp_path):
        slots = slots_of(tmp_path, overrides={("q02", 1): {"locator_success_rate": "0.5"}})
        assert locator_line(slots).status == "FAIL"

    def test_a_generated_run_without_a_value_cannot_be_checked(self, tmp_path):
        slots = slots_of(tmp_path, overrides={("q02", 1): {"locator_success_rate": ""}})
        assert locator_line(slots).status == "CANNOT"

    def test_generation_errors_are_not_locator_runs(self, tmp_path):
        slots = slots_of(tmp_path, overrides={("q02", 1): {"generation_status": "error", "test_status": "",
                                                           "locator_success_rate": "", "flake_retries": "",
                                                           "dryrun_repairs": ""}},
                         capture_kw={("q02", 1): {"code": None, "error_message": PROVIDER_503,
                                                  "status": "error", "tokens": {}, "traces": []}})
        assert locator_line(slots).text.startswith("29/29")
        assert flake_line(slots).status == "PASS"

    def test_repairs_are_subtracted_per_run(self, tmp_path):
        slots = slots_of(tmp_path, overrides={("q03", 1): {"flake_retries": "2", "dryrun_repairs": "2"}})
        assert flake_line(slots).status == "PASS"

    def test_a_flake_fails_even_when_another_run_is_negative(self, tmp_path):
        slots = slots_of(tmp_path, overrides={("q03", 1): {"flake_retries": "1", "dryrun_repairs": "0"},
                                              ("q03", 2): {"flake_retries": "0", "dryrun_repairs": "1"}})
        assert flake_line(slots).status == "FAIL"


class TestSalvageShape:
    @pytest.mark.parametrize("text, expected", [
        (INSTRUCTOR_1_8_1, "instructor (1.8.1 wording)"),
        (INSTRUCTOR_1_15, "instructor (1.15 wording)"),
        (ROUTED, "plain Converter / post-W5.1 routed (1.8.1 wording)"),
        (PLAIN_1_15, "plain Converter / post-W5.1 routed (1.15 wording)"),
    ])
    def test_every_converter_shape_is_a_salvage(self, text, expected):
        assert salvage_shape(text) == expected

    @pytest.mark.parametrize("text", [
        PLANNER_ROW["prompt_text"], ASSEMBLER_ROW["prompt_text"], None, "", "not json",
        json.dumps([{"role": "user", "content": "SYSTEM: You are a helpful assistant"}]),
        json.dumps([{"role": "user", "content": "Ensure your final answer is short"}]),
    ])
    def test_ordinary_calls_are_not_salvage(self, text):
        assert salvage_shape(text) is None


class TestPlannerState:
    def test_the_planner_row_is_chosen_by_prompt_not_file_order(self):
        salvage_first = {"id": "s", "start_time_ns": 250, "prompt_text": ROUTED,
                         "response_text": json.dumps({"steps": []})}
        rows = [salvage_first, ASSEMBLER_ROW, ROOT_ROW, PLANNER_ROW]  # capture has no ORDER BY
        assert planner_state(rows) == "parsed"

    def test_the_latest_planner_answer_is_the_one_converted(self):
        early = {**PLANNER_ROW, "id": "p0", "start_time_ns": 100, "response_text": "Thought: no JSON"}
        middle = {**PLANNER_ROW, "id": "p1", "start_time_ns": 200, "response_text": "Thought: still none"}
        late = {**PLANNER_ROW, "id": "p2", "start_time_ns": 300}
        # file order is neither time order nor its reverse: only start_time_ns picks `late`
        assert planner_state([middle, late, early]) == "parsed"
        assert planner_state([{**late, "start_time_ns": 50}, middle, early]) == "UNPARSED"

    @pytest.mark.parametrize("answer", ["Thought: I now can give a great answer",
                                        '{"steps": [{"step_description": "Open the browser", "keyword": "New'])
    def test_an_unparsed_planner_answer(self, answer):
        assert planner_state([{**PLANNER_ROW, "response_text": answer}]) == "UNPARSED"

    def test_a_pre_schema_final_answer_wrapper_is_parsed_like_crewai_does(self):
        # 251 captures of 2026-07-03..07-10 answered 'Final Answer: {...}'; crewai's {.*} span took them
        text = 'Final Answer: {"steps": [{"step_description": "Open", "keyword": "New Page"}]}'
        assert planner_state([{**PLANNER_ROW, "response_text": text}]) == "parsed"

    def test_a_raw_newline_inside_a_string_parses_like_crewai_does(self):
        # crewai 1.8.1 parses the whole answer with json.loads(strict=False), which takes a raw control character
        text = '{"steps": ["line one\nline two"]}'   # a real newline inside the string, not the \n escape
        assert planner_state([{**PLANNER_ROW, "response_text": text}]) == "parsed"

    def test_a_raw_newline_that_only_the_span_can_reach_is_unparsed_like_crewai(self):
        # crewai validates the {...} span with pydantic model_validate_json, which rejects a raw control character
        text = 'Final Answer: {"steps": ["line one\nline two"]}'
        assert planner_state([{**PLANNER_ROW, "response_text": text}]) == "UNPARSED"

    def test_no_planner_row_and_no_answer(self):
        assert planner_state([ROOT_ROW]) == "no planner row"
        assert planner_state([{**PLANNER_ROW, "response_text": None}]) == "no planner answer"


class TestSalvageLine:
    def test_clean_runs_pass(self, tmp_path):
        assert salvage_line(slots_of(tmp_path)).status == "PASS"

    def test_a_salvage_row_fails_the_gate(self, tmp_path):
        rows = [ROOT_ROW, PLANNER_ROW, {"id": "s1", "start_time_ns": 250, "prompt_text": INSTRUCTOR_1_15,
                                        "response_text": None}, ASSEMBLER_ROW]
        line = salvage_line(slots_of(tmp_path, capture_kw={("q06", 2): {"traces": rows}}))
        assert line.status == "FAIL" and "instructor (1.15 wording): 1" in line.text

    def test_an_unparsed_planner_answer_fails_the_gate(self, tmp_path):
        rows = [ROOT_ROW, {**PLANNER_ROW, "response_text": "Thought: I now can give a great answer"}]
        line = salvage_line(slots_of(tmp_path, capture_kw={("q06", 2): {"traces": rows}}))
        assert line.status == "FAIL" and "1 unparsed planner answers" in line.text

    def test_generated_runs_with_no_traces_cannot_be_checked(self, tmp_path):
        cap = {(q, r): {"traces": []} for q in [f"q{i:02d}" for i in range(1, 11)] for r in (1, 2, 3)}
        slots = slots_of(tmp_path, capture_kw=cap)
        line = salvage_line(slots)
        assert line.status == "CANNOT"
        assert line.text == ("0 salvage-shaped rows, 0 unparsed planner answers over 30 runs "
                             "(30 generated runs with no planner answer, 0 other runs without one)")
        assert len(line.details) == 30
        for s, detail in zip(slots, line.details):
            assert detail.startswith("no planner row in ") and s.current.workflow_id in detail
            assert len(s.current.workflow_id) == 36   # the full id, never a prefix

    def test_a_generated_run_with_no_planner_answer_cannot_be_checked(self, tmp_path):
        rows = [ROOT_ROW, {**PLANNER_ROW, "response_text": None}, ASSEMBLER_ROW]
        slots = slots_of(tmp_path, capture_kw={("q06", 2): {"traces": rows}})
        line = salvage_line(slots)
        assert line.status == "CANNOT"
        assert line.details == [f"no planner answer in q06 r2 {wf_id('cand', 'q06', 2)}"]
        assert "(1 generated runs with no planner answer, 0 other runs without one)" in line.text

    def test_a_salvage_row_fails_even_when_another_run_is_blind(self, tmp_path):
        salvage = [ROOT_ROW, PLANNER_ROW, {"id": "s1", "start_time_ns": 250, "prompt_text": INSTRUCTOR_1_15,
                                           "response_text": None}, ASSEMBLER_ROW]
        slots = slots_of(tmp_path, capture_kw={("q01", 1): {"traces": []}, ("q06", 2): {"traces": salvage}})
        line = salvage_line(slots)
        assert line.status == "FAIL"
        assert f"no planner row in q01 r1 {wf_id('cand', 'q01', 1)}" in line.details

    def test_a_generation_error_without_a_planner_answer_is_informational(self, tmp_path):
        slots = slots_of(tmp_path, overrides={("q02", 1): {"generation_status": "error", "test_status": ""}},
                         capture_kw={("q02", 1): {"code": None, "error_message": PROVIDER_503,
                                                  "status": "error", "tokens": {}, "traces": []}})
        line = salvage_line(slots)
        assert (line.status, line.details) == ("PASS", [])
        assert line.text.endswith("(0 generated runs with no planner answer, 1 other runs without one)")

    def test_a_salvage_row_in_a_replaced_provider_miss_still_fails(self, tmp_path):
        miss = {"code": None, "error_message": PROVIDER_503, "status": "error", "tokens": {},
                "traces": [{"id": "s1", "start_time_ns": 250, "prompt_text": INSTRUCTOR_1_15,
                            "response_text": None}]}
        cand, _ = write_bench(tmp_path, "cand", overrides={("q03", 2): {"test_status": "",
                                                                         "generation_status": "error"}},
                              capture_kw={("q03", 2): miss})
        wf = wf_id("r1", "q03", 1)
        write_capture(tmp_path / "bench" / "runs", wf)
        rerun = load_runs(write_csv(tmp_path / "bench" / "baselines" / "r1.csv", [row("q03", 1, wf)]),
                          tmp_path / "bench" / "runs")
        slots = build_slots(load_runs(cand, tmp_path / "bench" / "runs"), rerun)
        replaced = next(s for s in slots if len(s.history) == 2)
        assert replaced.outcome == PASSED and replaced.current.workflow_id == wf
        line = salvage_line(slots)   # counted over every run in the slot's history, not only the current one
        assert line.status == "FAIL"
        assert line.details == [f"salvage row s1 [instructor (1.15 wording)] in {replaced.history[0].label}"]
        assert line.text.startswith("1 salvage-shaped rows, 0 unparsed planner answers over 31 runs "
                                    "(0 generated runs with no planner answer, 1 other runs without one)")


class TestTokens:
    def test_the_same_tokens_pass_every_family(self, tmp_path):
        lines = token_lines(runs_of(tmp_path, "base"), slots_of(tmp_path))
        assert [(line.name, line.status) for line in lines] == [
            ("TOKENS crewai prompt", "PASS"), ("TOKENS crewai completion", "PASS"),
            ("TOKENS browser-use prompt", "PASS"), ("TOKENS browser-use completion", "PASS")]
        assert lines[0].text.startswith("+0.0% (limit +10%)")

    def test_families_are_gated_separately(self, tmp_path):
        more = {**TOKENS, "crewai_prompt_tokens": 8900}  # +11.25% on every query
        cap = {(q, r): {"tokens": more} for q in [f"q{i:02d}" for i in range(1, 11)] for r in (1, 2, 3)}
        lines = token_lines(runs_of(tmp_path, "base"), slots_of(tmp_path, capture_kw=cap))
        assert [line.status for line in lines] == ["FAIL", "PASS", "PASS", "PASS"]

    def test_the_browser_use_prompt_limit_is_5_percent(self, tmp_path):
        every = [(f"q{i:02d}", r) for i in range(1, 11) for r in (1, 2, 3)]
        at_limit = {k: {"tokens": {**TOKENS, "browser_use_prompt_tokens": 31500}} for k in every}   # +5.0%
        over = {k: {"tokens": {**TOKENS, "browser_use_prompt_tokens": 31800}} for k in every}       # +6.0%
        base = runs_of(tmp_path, "base")
        assert token_lines(base, slots_of(tmp_path, "at", capture_kw=at_limit))[2].status == "PASS"
        assert token_lines(base, slots_of(tmp_path, "over", capture_kw=over))[2].status == "FAIL"

    def test_a_family_whose_summed_medians_fall_more_than_25_percent_cannot_be_compared(self, tmp_path):
        # the shape of a broken shared _token_usage accumulator: one agent uncounted, every query 8,000 -> 4,800
        cap = {(q, r): {"tokens": {**TOKENS, "crewai_prompt_tokens": 4800}}
               for q in [f"q{i:02d}" for i in range(1, 11)] for r in (1, 2, 3)}
        lines = token_lines(runs_of(tmp_path, "base"), slots_of(tmp_path, capture_kw=cap))
        assert [line.status for line in lines] == ["CANNOT", "PASS", "PASS", "PASS"]
        assert lines[0].text == ("-40.0%: summed medians fell more than 25% below the baseline — a lost measurement "
                                 "until shown otherwise (a genuine saving needs a new baseline): sum of per-query "
                                 "medians 80,000 -> 48,000 over 10 queries")
        assert lines[0].details == [f"listed, not gated: q{i:02d} 8,000 -> 4,800 (-40.0%)" for i in range(1, 11)]

    @pytest.mark.parametrize("value, pct", [(6000, "-25.0%"), (6080, "-24.0%")])
    def test_a_drop_of_25_percent_or_less_still_passes(self, tmp_path, value, pct):
        cap = {(q, r): {"tokens": {**TOKENS, "crewai_prompt_tokens": value}}
               for q in [f"q{i:02d}" for i in range(1, 11)] for r in (1, 2, 3)}
        line = token_lines(runs_of(tmp_path, "base"), slots_of(tmp_path, capture_kw=cap))[0]
        assert line.status == "PASS" and line.text.startswith(f"{pct} (limit +10%)")

    def test_one_query_over_25_percent_is_listed_not_gated(self, tmp_path):
        more = {**TOKENS, "browser_use_prompt_tokens": 42000}  # q10 +40%; the sum moves +4%
        cap = {("q10", r): {"tokens": more} for r in (1, 2, 3)}
        line = token_lines(runs_of(tmp_path, "base"), slots_of(tmp_path, capture_kw=cap))[2]
        assert line.status == "PASS" and line.text.startswith("+4.0% (limit +5%)")
        assert line.details == ["listed, not gated: q10 30,000 -> 42,000 (+40.0%)"]

    def test_a_zeroed_crewai_accumulator_is_listed(self, tmp_path):
        zero = {**TOKENS, "crewai_prompt_tokens": 0}
        cap = {("q04", r): {"tokens": zero} for r in (1, 2, 3)}
        line = token_lines(runs_of(tmp_path, "base"), slots_of(tmp_path, capture_kw=cap))[0]
        assert line.details == ["candidate median 0, not in the sum: q04 8,000 -> 0"]
        assert line.text.startswith("+0.0% (limit +10%)") and line.text.endswith("over 9 queries")

    def test_a_zeroed_candidate_median_is_not_summed_as_a_saving(self, tmp_path):
        cap = {(q, r): {"tokens": {**TOKENS, "crewai_prompt_tokens": 0 if q == "q04" else 8880}}
               for q in [f"q{i:02d}" for i in range(1, 11)] for r in (1, 2, 3)}
        line = token_lines(runs_of(tmp_path, "base"), slots_of(tmp_path, capture_kw=cap))[0]
        # summed, q04's zero would offset the nine +11% queries: 80,000 -> 79,920 = -0.1%, a PASS
        assert line.status == "FAIL"
        assert line.text.startswith("+11.0% (limit +10%)") and line.text.endswith("over 9 queries")
        assert line.details == ["candidate median 0, not in the sum: q04 8,000 -> 0"]
        assert not any(d.startswith("listed, not gated: q04") for d in line.details)

    def test_a_family_every_candidate_zeroed_cannot_be_compared(self, tmp_path):
        zero = {**TOKENS, "crewai_prompt_tokens": 0, "crewai_completion_tokens": 0}
        cap = {(q, r): {"tokens": zero} for q in [f"q{i:02d}" for i in range(1, 11)] for r in (1, 2, 3)}
        lines = token_lines(runs_of(tmp_path, "base"), slots_of(tmp_path, capture_kw=cap))
        assert [line.status for line in lines] == ["CANNOT", "CANNOT", "PASS", "PASS"]
        for line, before in zip(lines[:2], ("8,000", "500")):
            assert line.text == "no query has a value on both sides"
            assert line.details == [f"candidate median 0, not in the sum: q{i:02d} {before} -> 0"
                                    for i in range(1, 11)]

    def test_a_jumpy_query_with_a_zeroed_candidate_stays_a_jumpy_line_only(self, tmp_path):
        jumpy = {("q01", r): {"tokens": {**TOKENS, "browser_use_prompt_tokens": v}}
                 for r, v in ((1, 22900), (2, 35600), (3, 22900))}
        zero = {("q01", r): {"tokens": {**TOKENS, "browser_use_prompt_tokens": 0}} for r in (1, 2, 3)}
        line = token_lines(runs_of(tmp_path, "base", capture_kw=jumpy), slots_of(tmp_path, capture_kw=zero))[2]
        assert line.details == [
            "baseline runs spread 55.5% (more than 25%), not in the sum: q01 22,900..35,600 -> 0"]

    def test_a_baseline_median_of_zero_is_listed_not_summed(self, tmp_path):
        zero = {**TOKENS, "browser_use_completion_tokens": 0}
        base = runs_of(tmp_path, "base", capture_kw={("q02", r): {"tokens": zero} for r in (1, 2, 3)})
        line = token_lines(base, slots_of(tmp_path))[3]
        assert "baseline median 0, not in the sum: q02" in line.details
        assert line.text.endswith("over 9 queries")

    def test_a_jumpy_baseline_query_is_listed_not_summed(self, tmp_path):
        # q01's browser-use prompt is bimodal in every bench (22,848 / 35,712 on the real baseline).
        jumpy = {("q01", r): {"tokens": {**TOKENS, "browser_use_prompt_tokens": v}}
                 for r, v in ((1, 22900), (2, 35600), (3, 22900))}
        big = {("q01", r): {"tokens": {**TOKENS, "browser_use_prompt_tokens": 40000}} for r in (1, 2, 3)}
        lines = token_lines(runs_of(tmp_path, "base", capture_kw=jumpy), slots_of(tmp_path, capture_kw=big))
        # summed, q01 alone would move the family +5.8% (292,900 -> 310,000) and FAIL the +5% limit
        assert lines[2].status == "PASS" and lines[2].text.startswith("+0.0% (limit +5%)")
        assert lines[2].text.endswith("over 9 queries")
        assert lines[2].details == [
            "baseline runs spread 55.5% (more than 25%), not in the sum: q01 22,900..35,600 -> 40,000"]
        assert lines[0].text.endswith("over 10 queries")   # the rule is per family

    def test_a_spread_of_exactly_25_percent_is_summed(self, tmp_path):
        edge = {("q02", r): {"tokens": {**TOKENS, "crewai_completion_tokens": v}}
                for r, v in ((1, 400), (2, 500), (3, 400))}
        line = token_lines(runs_of(tmp_path, "base", capture_kw=edge), slots_of(tmp_path))[1]
        assert line.text.endswith("over 10 queries") and not line.details

    def test_one_zeroed_baseline_run_makes_the_query_jumpy_without_crashing(self, tmp_path):
        zeroed = {("q03", 1): {"tokens": {**TOKENS, "crewai_prompt_tokens": 0}}}   # median stays 8,000
        line = token_lines(runs_of(tmp_path, "base", capture_kw=zeroed), slots_of(tmp_path))[0]
        assert "baseline runs spread from 0 (more than 25%), not in the sum: q03 0..8,000 -> 8,000" in line.details

    def test_missing_or_non_numeric_values_leave_the_query_out(self, tmp_path):
        old = {k: v for k, v in TOKENS.items() if k != "crewai_completion_tokens"}
        cap = {("q05", r): {"tokens": {**old, "crewai_prompt_tokens": "n/a"}} for r in (1, 2, 3)}
        lines = token_lines(runs_of(tmp_path, "base"), slots_of(tmp_path, capture_kw=cap))
        assert "no value on one side, not in the sum: q05" in lines[0].details
        assert "no value on one side, not in the sum: q05" in lines[1].details

    def test_a_family_nobody_measured_cannot_be_compared(self, tmp_path):
        old = {k: v for k, v in TOKENS.items() if not k.startswith("crewai")}
        cap = {(q, r): {"tokens": old} for q in [f"q{i:02d}" for i in range(1, 11)] for r in (1, 2, 3)}
        lines = token_lines(runs_of(tmp_path, "base"), slots_of(tmp_path, capture_kw=cap))
        assert [line.status for line in lines] == ["CANNOT", "CANNOT", "PASS", "PASS"]


def test_cost_cache_and_paired_change_are_reported_only(tmp_path):
    line = reported_cost_lines(runs_of(tmp_path, "base"), slots_of(tmp_path))[0]
    assert line.status == "INFO"
    assert "(+0.0%)" in line.text and "cache share 10.0% -> 10.0%" in line.text
    assert "llm_tokens median paired change +0.0%" in line.text


def test_non_numeric_llm_tokens_in_an_old_csv_read_as_not_available(tmp_path):
    cap_rows = {(q, r): {"llm_tokens": "n/a"} for q in [f"q{i:02d}" for i in range(1, 11)] for r in (1, 2, 3)}
    line = reported_cost_lines(runs_of(tmp_path, "base"), slots_of(tmp_path, overrides=cap_rows))[0]
    assert "llm_tokens median paired change n/a" in line.text


class TestWindow:
    def test_local_started_at_becomes_utc(self, tmp_path):
        runs = runs_of(tmp_path, "c", overrides={("q01", 1): {"started_at": "2026-09-24T14:41:43",
                                                              "total_s": "60"}})
        start, end = bench_window(runs, IST)
        assert start.isoformat() == "2026-09-24T09:11:43+00:00"
        assert end.isoformat() == "2026-09-24T14:30:30+00:00"  # 20:00:00 IST + 30 s on the other rows

    def log_line(self, stamp: str, event: str) -> str:
        return json.dumps({"event": event, "level": "info", "timestamp": stamp + "Z"}) + "\n"

    def test_counts_signatures_across_a_mid_bench_rotation(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "application.log.1").write_text(
            self.log_line("2026-09-24T14:29:00.1", "before the window 429 Too Many Requests")
            + self.log_line("2026-09-24T14:30:05.742932", 'litellm.RateLimitError: {"code": 429}')
            + self.log_line("2026-09-24T14:30:06.1", "tokens=4290 run 429abc"), encoding="utf-8")
        (logs / "application.log").write_text(
            self.log_line("2026-09-24T14:30:20.5", "Vertex 503 Service Unavailable")
            + self.log_line("2026-09-24T14:30:25.1", "RESOURCE_EXHAUSTED")
            + self.log_line("2026-09-24T14:31:00.1", "after the window 429 Too Many Requests"), encoding="utf-8")
        runs = runs_of(tmp_path, "c", overrides={(q, r): {"started_at": "2026-09-24T20:00:00"}
                                                 for q in ["q01"] for r in (1,)})
        line = window_line(runs, logs, IST)
        assert line.status == "INFO"
        assert "2 lines match the 429 signature, 1 the 503 signature, 4 log lines in the window" in line.text

    def test_an_empty_window_is_said_to_be_empty(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "application.log").write_text(self.log_line("2026-09-20T00:00:00.1", "x"), encoding="utf-8")
        line = window_line(runs_of(tmp_path, "c"), logs, IST)
        assert "EMPTY window" in line.text

    def test_logs_that_start_inside_the_window_are_flagged(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "application.log").write_text(self.log_line("2026-09-24T14:30:10.0", "x"), encoding="utf-8")
        line = window_line(runs_of(tmp_path, "c"), logs, IST)
        assert "partly rotated away" in line.text

    def test_no_logs_is_not_checked(self, tmp_path):
        assert "no application.log" in window_line(runs_of(tmp_path, "c"), tmp_path / "nologs", IST).text
