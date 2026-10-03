"""bench/gate_hollow.py: the (query, shape) hollow registry and the verified rate."""

from bench.gate_hollow import REGISTERED_HOLLOW, hollow_lines
from bench.gate_inputs import build_slots, load_runs
from src.backend.core.pass_quality import READ_LOCATOR_IS_THE_ANSWER, READ_NOT_PERFORMED
from tests.test_bench.gate_fixtures import (
    ANSWER_LOCATOR_Q05,
    COUNT_ONLY_Q10,
    EMPTY_TEST_CODE,
    HEADER,
    SELECT_BLIND_Q08,
    wf_id,
    write_bench,
)


def lines_for(tmp_path, codes=None, overrides=None):
    cand, _ = write_bench(tmp_path, "cand", codes=codes, overrides=overrides)
    lines = hollow_lines(build_slots(load_runs(cand, tmp_path / "bench" / "runs"), []))
    return {line.name: line for line in lines}


def test_the_registry_is_the_owners_two_pairs():
    assert REGISTERED_HOLLOW == {("q10", READ_NOT_PERFORMED), ("q05", READ_LOCATOR_IS_THE_ANSWER)}


def test_a_clean_bench_passes_with_a_full_verified_rate(tmp_path):
    lines = lines_for(tmp_path)
    assert lines["HOLLOW"].status == "PASS"
    assert lines["VERIFIED"].text.startswith("verified pass rate 30/30 = 100.0%")


def test_registered_pairs_pass_and_lower_the_verified_rate(tmp_path):
    lines = lines_for(tmp_path, codes={("q10", 1): COUNT_ONLY_Q10, ("q05", 2): ANSWER_LOCATOR_Q05})
    assert lines["HOLLOW"].status == "PASS"
    assert "(q05, READ_LOCATOR_IS_THE_ANSWER) x1, (q10, READ_NOT_PERFORMED) x1" in lines["HOLLOW"].text
    assert lines["VERIFIED"].text.startswith("verified pass rate 28/30 = 93.3% (30 passes minus 2 hollow)")
    assert lines["VERIFIED"].status == "INFO"


def test_an_unregistered_pair_fails_and_names_the_run(tmp_path):
    # the pre-q08-fix shape: every such bench fails here until the q08 normalizer ships
    lines = lines_for(tmp_path, codes={("q08", 3): SELECT_BLIND_Q08})
    assert lines["HOLLOW"].status == "FAIL"
    assert lines["HOLLOW"].details == [
        f"(q08, SELECT_CHECK_CANNOT_FAIL) q08 r3 {wf_id('cand', 'q08', 3)} [id=dropdown]"]


def test_an_empty_test_is_hollow_and_unregistered(tmp_path):
    lines = lines_for(tmp_path, codes={("q04", 3): EMPTY_TEST_CODE})
    assert lines["HOLLOW"].status == "FAIL"
    assert lines["HOLLOW"].details[0].startswith("(q04, EMPTY_TEST) q04 r3")


def test_a_hollow_failed_run_is_not_a_hollow_pass(tmp_path):
    lines = lines_for(tmp_path, codes={("q08", 3): SELECT_BLIND_Q08},
                      overrides={("q08", 3): {"test_status": "failed"}})
    assert lines["HOLLOW"].status == "PASS"
    assert lines["VERIFIED"].text.startswith("verified pass rate 29/30")


def test_a_numeric_id_read_is_reported_not_hollow(tmp_path):
    fragile = HEADER + "    ${n}=    Get Text    id=880667900\n    Log    ${n}\n    Close Browser\n"
    lines = lines_for(tmp_path, codes={("q05", 1): fragile})
    assert lines["HOLLOW"].status == "PASS"
    assert lines["FRAGILE"].text.startswith("1 passes read by a numeric id")


def test_the_report_says_what_it_cannot_see(tmp_path):
    assert "3555f609-1258-48df-8188-ecf1a07fa12a" in lines_for(tmp_path)["NOT CHECKED"].text
