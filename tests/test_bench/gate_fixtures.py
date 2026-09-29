"""Synthetic bench trees for the bench-gate tests: a CSV + .meta.json + bench/runs/<id>/ captures.

Nothing here is bench data: every query, token count, error and Robot file is hand-written
to reproduce one captured shape, and each helper says which. Used by tests/test_bench/test_gate_*.py.
"""

import csv
import json
import uuid
from pathlib import Path

from bench.bench_lib import CSV_COLUMNS

PINS = {
    "git_sha": "0123456789abcdef0123456789abcdef01234567",
    "nlrf_pins": {"optimization_enabled": False, "model_provider": "vertex",
                  "online_model": "gemini-3.5-flash", "dryrun_enabled": True},
    "browser_service": {"model_provider": "vertex", "headless": True},
}
Q05 = ("Go to https://the-internet.herokuapp.com/tables and get the text from the first row, "
       "second column of Table 1")
Q08 = ("Go to https://the-internet.herokuapp.com/dropdown, select Option 2 from the dropdown, "
       "and verify Option 2 is selected")
Q10 = ("Go to https://books.toscrape.com, get the titles of all books on the first page, and "
       "verify there are 20 books")
QUERIES = {f"q{i:02d}": f"Go to https://example.com/q{i:02d} and click the Next button"
           for i in range(1, 11)}
QUERIES.update({"q05": Q05, "q08": Q08, "q10": Q10})

HEADER = ("*** Settings ***\nLibrary    Browser\n\n*** Test Cases ***\nGenerated Test\n"
          "    New Browser    chromium    headless=True\n    New Page    https://example.com\n")
CLICK_TEST = HEADER + "    Click    id=next\n    Close Browser\n"
GOOD_TESTS = {
    "q05": HEADER + "    ${t}=    Get Text    xpath=//table[@id='table1']/tbody/tr[1]/td[2]\n"
                    "    Log    ${t}\n    Close Browser\n",
    "q08": HEADER + "    Select Options By    id=dropdown    label    Option 2\n"
                    "    ${s}=    Get Selected Options    id=dropdown\n"
                    "    Should Be Equal    ${s}[0]    Option 2\n    Close Browser\n",
    "q10": HEADER + "    ${titles}=    Get Texts    css=h3 a\n"
                    "    Length Should Be    ${titles}    20\n    Close Browser\n",
}
COUNT_ONLY_Q10 = HEADER + ("    @{b}=    Get Elements    css=h3 a\n    ${n}=    Get Length    ${b}\n"
                           "    Should Be True    ${n} == 20\n    Close Browser\n")
SELECT_BLIND_Q08 = HEADER + ("    Select Options By    id=dropdown    label    Option 2\n"
                             "    ${x}=    Get Text    css=#dropdown\n"
                             "    Should Contain    ${x}    Option 2\n    Close Browser\n")
ANSWER_LOCATOR_Q05 = HEADER + ("    ${t}=    Get Text    text=\"John\" >> nth=0\n"
                               "    Log    ${t}\n    Close Browser\n")
EMPTY_TEST_CODE = HEADER + "    Close Browser\n"
TOKENS = {"crewai_prompt_tokens": 8000, "crewai_completion_tokens": 500,
          "browser_use_prompt_tokens": 30000, "browser_use_completion_tokens": 400,
          "browser_use_cached_tokens": 3000}
PLANNER_ROW = {"id": "p1", "name": "gemini-3.5-flash.litellm", "start_time_ns": 200,
               "prompt_text": json.dumps([{"role": "system", "content": "You are Test Automation Planner. ..."},
                                          {"role": "user", "content": "plan it"}]),
               "response_text": json.dumps({"steps": [{"step_description": "Open", "keyword": "New Page"}]})}
ROOT_ROW = {"id": "r1", "name": "test-generation-workflow", "start_time_ns": 100,
            "prompt_text": None, "response_text": None}
ASSEMBLER_ROW = {"id": "a1", "name": "gemini-3.5-flash.litellm", "start_time_ns": 300,
                 "prompt_text": json.dumps([{"role": "system", "content": "You are Robot Framework Code Generator."},
                                            {"role": "user", "content": "assemble"}]),
                 "response_text": json.dumps({"code": "*** Settings ***"})}
PROVIDER_503 = ('An error occurred: litellm.ServiceUnavailableError: VertexAIException - {\n  "error": '
                '{\n    "code": 503,\n    "message": "The service is currently unavailable.",\n'
                '    "status": "UNAVAILABLE"\n  }\n}\n')  # the 9c983e90-4b50-48a0-b255-fefdde5f584f shape


def wf_id(tag: str, qid: str, repeat: int) -> str:
    """A stable, unique UUID per (tag, query, repeat)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"bench-gate-test/{tag}/{qid}/{repeat}"))


def row(qid: str, repeat: int, workflow_id: str, *, test_status: str = "passed",
        generation_status: str = "complete", started_at: str = "2026-09-24T20:00:00",
        **overrides) -> dict:
    values = {"query_id": qid, "query": QUERIES[qid], "repeat": repeat, "workflow_id": workflow_id,
              "started_at": started_at, "generation_status": generation_status,
              "test_status": test_status, "dryrun_status": "", "total_s": "30",
              "llm_tokens": "40000", "llm_cost_usd": "0.05",
              "locator_success_rate": "1.0" if generation_status == "complete" else "",
              "flake_retries": "0" if generation_status == "complete" else "",
              "dryrun_repairs": "0" if generation_status == "complete" else ""}
    values.update(overrides)
    return values


def write_capture(runs_dir: Path, workflow_id: str, *, code: str | None = CLICK_TEST,
                  error_message: str | None = None, status: str = "passed",
                  tokens: dict | None = None, traces: list | None = None,
                  skip: tuple[str, ...] = ()) -> Path:
    """bench/runs/<id>/ as run_bench.capture_evidence writes it."""
    d = runs_dir / workflow_id
    (d / "artifacts").mkdir(parents=True, exist_ok=True)
    files = {
        "test_runs.json": [{"run_id": workflow_id, "status": status, "error_message": error_message}],
        "llm_traces.json": [ROOT_ROW, PLANNER_ROW, ASSEMBLER_ROW] if traces is None else traces,
        "workflow_metrics.json": [] if tokens == {} else [{"workflow_id": workflow_id,
                                                           "data": dict(TOKENS if tokens is None else tokens)}],
    }
    for name, payload in files.items():
        if name not in skip:
            (d / name).write_text(json.dumps(payload), encoding="utf-8")
    if code is not None and "test.robot" not in skip:
        (d / "artifacts" / "test.robot").write_text(code, encoding="utf-8")
    return d


def write_csv(path: Path, rows: list[dict], meta: dict | None = PINS) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in CSV_COLUMNS})
    if meta is not None:
        Path(f"{path}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return path


def write_bench(root: Path, name: str, *, tag: str | None = None, codes: dict | None = None,
                overrides: dict | None = None, capture_kw: dict | None = None,
                meta: dict | None = PINS) -> tuple[Path, list[dict]]:
    """A clean 10-query x 3-repeat bench: every run passes with a non-hollow test.

    codes: {(qid, repeat): robot code}; overrides: {(qid, repeat): row fields};
    capture_kw: {(qid, repeat): write_capture kwargs}. Returns (csv path, rows).
    """
    tag = tag or name
    rows = []
    for qid in QUERIES:
        for rep in (1, 2, 3):
            wf = wf_id(tag, qid, rep)
            r = row(qid, rep, wf, **(overrides or {}).get((qid, rep), {}))
            rows.append(r)
            kw = {"code": (codes or {}).get((qid, rep), GOOD_TESTS.get(qid, CLICK_TEST))}
            kw.update((capture_kw or {}).get((qid, rep), {}))
            write_capture(root / "bench" / "runs", wf, **kw)
    return write_csv(root / "bench" / "baselines" / f"{name}.csv", rows, meta), rows
