"""Browser-service log parsers.

browser-service logs to logs/browser_use.log via structlog:
- default: JSON lines  {"event": "...", "level": "...", "logger": "...", "timestamp": "ISO Z"}
- LOG_FORMAT=console: `<iso-ts> [level   ] <event, padded> [logger.name]`

The parsers must handle both, line by line. Timestamped markers used:
- LOCATOR_TIMER element_id=... duration_ms=... found=...   (per-element latency)
- LOCATOR_PROBE workflow_id=... stable_hash=... element_id=...  (dup-lookup rate)
- "🚀 Starting browser session..." → "✅ Browser session started successfully" (cold start)
- "🧹 Starting browser cleanup..." → "🧹 Cleanup complete" (cleanup)
"""

import json
from datetime import datetime

from bench.bench_lib import (
    duplicate_lookup_rate,
    parse_locator_timers,
    parse_log_line,
    span_durations,
)


def _json_line(msg, ts="2026-07-03T10:00:00.000000Z", level="info"):
    return json.dumps({"event": msg, "level": level,
                       "logger": "browser_service.test", "timestamp": ts})


def _console_line(msg, ts="2026-07-03T10:00:00.000000Z", level="info"):
    return f"{ts} [{level:<8}] {msg:<30} [browser_service.test]"


class TestParseLogLine:
    def test_json_line(self):
        ts, msg = parse_log_line(_json_line("hello world"))
        assert isinstance(ts, datetime)
        assert ts.year == 2026 and ts.month == 7
        assert msg == "hello world"

    def test_json_line_with_unicode_escapes(self):
        # Real files store emoji as \ud83d... escapes — json.loads restores them.
        raw = ('{"event": "\\ud83d\\ude80 Starting browser session...", '
               '"level": "info", "logger": "x", '
               '"timestamp": "2026-07-03T10:00:01.500000Z"}')
        ts, msg = parse_log_line(raw)
        assert msg == "🚀 Starting browser session..."
        assert ts.second == 1

    def test_console_line(self):
        ts, msg = parse_log_line(
            _console_line("🚀 Starting browser session...",
                          ts="2026-07-03T10:00:02.250000Z"))
        assert isinstance(ts, datetime)
        assert ts.microsecond == 250000
        assert "🚀 Starting browser session..." in msg

    def test_garbage_line_has_no_timestamp_but_keeps_text(self):
        ts, msg = parse_log_line("Traceback (most recent call last):")
        assert ts is None
        assert "Traceback" in msg

    def test_empty_line(self):
        ts, msg = parse_log_line("")
        assert ts is None
        assert msg == ""


class TestLocatorTimers:
    def test_extracts_from_json_and_console(self):
        lines = [
            _json_line("LOCATOR_TIMER element_id=elem_1 duration_ms=123.4 found=True"),
            _console_line("LOCATOR_TIMER element_id=elem_2 duration_ms=56.0 found=False"),
            _json_line("unrelated line"),
        ]
        timers = parse_locator_timers(lines)
        assert len(timers) == 2
        assert timers[0] == {"element_id": "elem_1", "duration_ms": 123.4, "found": True}
        assert timers[1] == {"element_id": "elem_2", "duration_ms": 56.0, "found": False}

    def test_no_timers(self):
        assert parse_locator_timers([_json_line("nothing here")]) == []


class TestDuplicateLookupRate:
    def test_repeat_hash_same_workflow_is_a_duplicate(self):
        lines = [
            _json_line("📊 LOCATOR_PROBE workflow_id=wf-1 stable_hash=aaa element_id=e1"),
            _json_line("📊 LOCATOR_PROBE workflow_id=wf-1 stable_hash=bbb element_id=e2"),
            _json_line("📊 LOCATOR_PROBE workflow_id=wf-1 stable_hash=aaa element_id=e3"),
            _json_line("📊 LOCATOR_PROBE workflow_id=wf-1 stable_hash=ccc element_id=e4"),
        ]
        r = duplicate_lookup_rate(lines)
        assert r["total"] == 4
        assert r["unique"] == 3
        assert r["duplicate_rate"] == 0.25

    def test_same_hash_across_workflows_is_not_a_duplicate(self):
        lines = [
            _json_line("📊 LOCATOR_PROBE workflow_id=wf-1 stable_hash=aaa element_id=e1"),
            _json_line("📊 LOCATOR_PROBE workflow_id=wf-2 stable_hash=aaa element_id=e1"),
        ]
        r = duplicate_lookup_rate(lines)
        assert r["total"] == 2
        assert r["unique"] == 2
        assert r["duplicate_rate"] == 0.0

    def test_no_probes(self):
        r = duplicate_lookup_rate([_json_line("nothing")])
        assert r == {"total": 0, "unique": 0, "duplicate_rate": 0.0}


class TestSpanDurations:
    START = "🚀 Starting browser session..."
    END = "✅ Browser session started successfully"

    def test_single_cold_start_span(self):
        lines = [
            _json_line(self.START, ts="2026-07-03T10:00:00.000000Z"),
            _json_line("noise in between", ts="2026-07-03T10:00:01.000000Z"),
            _json_line(self.END, ts="2026-07-03T10:00:03.500000Z"),
        ]
        assert span_durations(lines, self.START, self.END) == [3.5]

    def test_two_spans_pair_sequentially(self):
        lines = [
            _json_line(self.START, ts="2026-07-03T10:00:00.000000Z"),
            _json_line(self.END, ts="2026-07-03T10:00:02.000000Z"),
            _json_line(self.START, ts="2026-07-03T10:05:00.000000Z"),
            _json_line(self.END, ts="2026-07-03T10:05:01.000000Z"),
        ]
        assert span_durations(lines, self.START, self.END) == [2.0, 1.0]

    def test_unclosed_span_is_dropped(self):
        lines = [_json_line(self.START, ts="2026-07-03T10:00:00.000000Z")]
        assert span_durations(lines, self.START, self.END) == []

    def test_end_without_start_is_ignored(self):
        lines = [_json_line(self.END, ts="2026-07-03T10:00:00.000000Z")]
        assert span_durations(lines, self.START, self.END) == []

    def test_cleanup_markers_console_format(self):
        lines = [
            _console_line("🧹 Starting browser cleanup...",
                          ts="2026-07-03T11:00:00.000000Z"),
            _console_line("🧹 Cleanup complete", ts="2026-07-03T11:00:04.000000Z"),
        ]
        got = span_durations(lines, "🧹 Starting browser cleanup...",
                             "🧹 Cleanup complete")
        assert got == [4.0]


class TestLogMetricsAbsentLog:
    def test_no_lines_leaves_every_column_empty(self):
        """BROWSER_SERVICE_LOG unset → run_bench promises empty columns, not
        zeros that read as measurements (and would skew report medians)."""
        from bench.run_bench import log_metrics
        metrics = log_metrics([])
        assert all(v is None for v in metrics.values())
