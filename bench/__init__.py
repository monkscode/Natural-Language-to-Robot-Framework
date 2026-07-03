"""Benchmark harness for the NL-to-RF pipeline (Task 2, locator-enhancement plan).

Modules:
    bench_lib  — pure, importable logic (SSE stage mapping, log parsers, report math)
    run_bench  — the runner: POSTs frozen queries, records metrics, detaches evidence
    report     — median/p90 report + baseline-vs-candidate compare
"""
