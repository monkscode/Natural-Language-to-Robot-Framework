"""Preflight pin checks + pins metadata (bench profile spec 2026-07-04).

A baseline is only comparable to runs pinned the same way (bench/README.md).
preflight_violations() gates the runner before it spends anything;
build_meta()/load_meta() make the pins travel with each CSV as
<out>.csv.meta.json; compare_pins() lets report.py flag drift between
baseline and candidate.
"""

import json

from bench.bench_lib import (
    build_meta,
    compare_pins,
    load_meta,
    meta_path_for,
    preflight_violations,
)

PINNED_NLRF = {"status": "healthy", "pins": {
    "optimization_enabled": False, "model_provider": "gemini",
    "online_model": "gemini-3.5-flash", "dryrun_enabled": True}}
BROWSER_NO_HEADLESS = {"status": "healthy", "model_provider": "gemini"}


class TestPreflightViolations:
    def test_pinned_stack_passes_with_headless_warning(self):
        violations, warnings = preflight_violations(PINNED_NLRF, BROWSER_NO_HEADLESS)
        assert violations == []
        assert len(warnings) == 1 and "headless" in warnings[0]

    def test_learning_on_is_a_violation(self):
        nlrf = {"pins": {**PINNED_NLRF["pins"], "optimization_enabled": True}}
        violations, _ = preflight_violations(nlrf, BROWSER_NO_HEADLESS)
        assert any("OPTIMIZATION_ENABLED" in v for v in violations)

    def test_missing_pins_object_is_a_violation(self):
        violations, _ = preflight_violations({"status": "healthy"}, BROWSER_NO_HEADLESS)
        assert any("pins" in v for v in violations)

    def test_headless_reported_true_no_warning(self):
        browser = {**BROWSER_NO_HEADLESS, "headless": True}
        violations, warnings = preflight_violations(PINNED_NLRF, browser)
        assert violations == [] and warnings == []

    def test_headless_reported_false_is_a_violation(self):
        browser = {**BROWSER_NO_HEADLESS, "headless": False}
        violations, _ = preflight_violations(PINNED_NLRF, browser)
        assert any("headless" in v for v in violations)


class TestMeta:
    def test_build_meta_captures_pins_and_urls(self):
        meta = build_meta(PINNED_NLRF, {**BROWSER_NO_HEADLESS, "headless": True},
                          "http://127.0.0.1:5000", "http://127.0.0.1:4999")
        assert meta["nlrf_pins"] == PINNED_NLRF["pins"]
        assert meta["browser_service"] == {"model_provider": "gemini", "headless": True}
        assert meta["base_url"] == "http://127.0.0.1:5000"
        assert meta["captured_at"]

    def test_meta_path_is_csv_plus_meta_json(self, tmp_path):
        assert str(meta_path_for(tmp_path / "x.csv")).endswith("x.csv.meta.json")

    def test_load_meta_roundtrip_and_missing(self, tmp_path):
        csv_path = tmp_path / "run.csv"
        assert load_meta(csv_path) is None
        meta_path_for(csv_path).write_text(json.dumps({"nlrf_pins": {}}), encoding="utf-8")
        assert load_meta(csv_path) == {"nlrf_pins": {}}


class TestComparePins:
    BASE = {"nlrf_pins": PINNED_NLRF["pins"], "browser_service": {"model_provider": "gemini"}}

    def test_identical_pins_no_mismatch(self):
        assert compare_pins(self.BASE, json.loads(json.dumps(self.BASE))) == []

    def test_model_change_is_reported(self):
        cand = {"nlrf_pins": {**PINNED_NLRF["pins"], "online_model": "gemini-4.0-flash"},
                "browser_service": {"model_provider": "gemini"}}
        mismatches = compare_pins(self.BASE, cand)
        assert len(mismatches) == 1
        assert "online_model" in mismatches[0]
        assert "gemini-3.5-flash" in mismatches[0] and "gemini-4.0-flash" in mismatches[0]

    def test_browser_provider_change_is_reported(self):
        cand = {"nlrf_pins": PINNED_NLRF["pins"], "browser_service": {"model_provider": "vertex"}}
        assert any("browser_service.model_provider" in m for m in compare_pins(self.BASE, cand))


class TestAppendPinConflict:
    """--out APPENDS, but the sidecar is a single file: re-using a path under
    different pins would leave one meta describing rows it did not produce."""

    NEW_META = {"nlrf_pins": PINNED_NLRF["pins"],
                "browser_service": {"model_provider": "gemini"}}

    def test_fresh_csv_has_no_conflict(self, tmp_path):
        from bench.bench_lib import append_pin_conflict
        assert append_pin_conflict(tmp_path / "new.csv", self.NEW_META) == []

    def test_existing_csv_with_matching_pins_has_no_conflict(self, tmp_path):
        from bench.bench_lib import append_pin_conflict
        csv_path = tmp_path / "run.csv"
        csv_path.write_text("query_id\n", encoding="utf-8")
        meta_path_for(csv_path).write_text(
            json.dumps(self.NEW_META), encoding="utf-8")
        assert append_pin_conflict(csv_path, self.NEW_META) == []

    def test_existing_csv_with_different_model_conflicts(self, tmp_path):
        from bench.bench_lib import append_pin_conflict
        csv_path = tmp_path / "run.csv"
        csv_path.write_text("query_id\n", encoding="utf-8")
        meta_path_for(csv_path).write_text(json.dumps(
            {"nlrf_pins": {**PINNED_NLRF["pins"], "online_model": "gemini-2.5-flash"},
             "browser_service": {"model_provider": "gemini"}}), encoding="utf-8")
        conflicts = append_pin_conflict(csv_path, self.NEW_META)
        assert len(conflicts) == 1 and "online_model" in conflicts[0]

    def test_existing_csv_without_meta_cannot_conflict(self, tmp_path):
        """Nothing to compare against — the runner warns instead of blocking."""
        from bench.bench_lib import append_pin_conflict
        csv_path = tmp_path / "run.csv"
        csv_path.write_text("query_id\n", encoding="utf-8")
        assert append_pin_conflict(csv_path, self.NEW_META) == []


class TestGatePinsSidecar:
    """gate_pins records a sidecar only for CSVs it can vouch for entirely."""

    def _gate(self, out_path):
        from types import SimpleNamespace
        from unittest.mock import patch
        from bench.run_bench import gate_pins
        args = SimpleNamespace(base_url="http://nlrf", browser_url="http://bs",
                               allow_unpinned=False)
        browser = {**BROWSER_NO_HEADLESS, "headless": True}
        with patch("bench.run_bench.fetch_health",
                   side_effect=[PINNED_NLRF, browser]):
            gate_pins(args, out_path)

    def test_fresh_path_records_sidecar(self, tmp_path):
        out = tmp_path / "run.csv"
        self._gate(out)
        assert meta_path_for(out).exists()

    def test_metaless_existing_csv_gets_no_sidecar(self, tmp_path):
        """Stamping pins mid-file would vouch for earlier rows recorded under
        unknown pins on the NEXT append — keep warning instead."""
        out = tmp_path / "run.csv"
        out.write_text("query_id\n", encoding="utf-8")
        self._gate(out)
        assert not meta_path_for(out).exists()


class TestReportPinCheck:
    def test_mismatch_prints_loud_warning(self, tmp_path, capsys):
        from bench.report import print_pin_check
        base, cand = tmp_path / "base.csv", tmp_path / "cand.csv"
        meta_path_for(base).write_text(json.dumps(
            {"nlrf_pins": {"online_model": "gemini-3.5-flash"}}), encoding="utf-8")
        meta_path_for(cand).write_text(json.dumps(
            {"nlrf_pins": {"online_model": "gemini-4.0-flash"}}), encoding="utf-8")
        print_pin_check(str(base), str(cand))
        out = capsys.readouterr().out
        assert "NOT comparable" in out and "online_model" in out

    def test_matching_pins_stay_quiet(self, tmp_path, capsys):
        from bench.report import print_pin_check
        base, cand = tmp_path / "base.csv", tmp_path / "cand.csv"
        for p in (base, cand):
            meta_path_for(p).write_text(json.dumps(
                {"nlrf_pins": {"online_model": "gemini-3.5-flash"}}), encoding="utf-8")
        print_pin_check(str(base), str(cand))
        assert "NOT comparable" not in capsys.readouterr().out

    def test_missing_meta_prints_note(self, tmp_path, capsys):
        from bench.report import print_pin_check
        print_pin_check(str(tmp_path / "a.csv"), str(tmp_path / "b.csv"))
        assert "comparability not verified" in capsys.readouterr().out
