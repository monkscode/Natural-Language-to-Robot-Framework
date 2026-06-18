"""LocalArtifactStore-independent unit tests for report screenshot inlining."""
import base64
import logging
import uuid
from pathlib import Path
from unittest.mock import patch

from src.backend.services.report_inliner import inline_report_screenshots

# A real 1x1 PNG (bytes are encoded back to base64 by the inliner under test).
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "robot_log_with_screenshot.html"


def _make_run(tmp_path, html: str, screenshots=()):
    run = tmp_path / str(uuid.uuid4())
    run.mkdir()
    (run / "log.html").write_text(html, encoding="utf-8")
    for rel in screenshots:
        p = run / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_PNG)
    return run


def test_real_fixture_inlines_both_refs(tmp_path):
    html = _FIXTURE.read_text(encoding="utf-8")
    run = _make_run(tmp_path, html, ["browser/screenshot/fail-screenshot-1.png"])
    n = inline_report_screenshots(run)
    assert n == 2  # the <a href> and the <img src>
    out = (run / "log.html").read_text(encoding="utf-8")
    assert "data:image/png;base64," in out
    assert "browser/screenshot/fail-screenshot-1.png" not in out


def test_no_temp_sidecar_left_after_inlining(tmp_path):
    # The atomic rewrite (sibling temp + os.replace) must leave no .inlining.tmp.
    html = 'x<img src=\\"browser/screenshot/fail-screenshot-1.png\\"/>y'
    run = _make_run(tmp_path, html, ["browser/screenshot/fail-screenshot-1.png"])
    assert inline_report_screenshots(run) == 1
    assert not list(run.glob("*.inlining.tmp"))
    assert (run / "log.html").is_file()


def test_rewrite_failure_swallowed_leaves_original(tmp_path):
    # A failed os.replace (e.g. a Windows reader holding the file open) must not
    # raise, must leave the complete original in place, must clean up the temp,
    # and must count 0 inlined (nothing landed).
    html = 'x<img src=\\"browser/screenshot/fail-screenshot-1.png\\"/>y'
    run = _make_run(tmp_path, html, ["browser/screenshot/fail-screenshot-1.png"])
    with patch("src.backend.services.report_inliner.os.replace",
               side_effect=OSError("file in use")):
        n = inline_report_screenshots(run)
    assert n == 0
    assert (run / "log.html").read_text(encoding="utf-8") == html
    assert not list(run.glob("*.inlining.tmp"))


def test_idempotent(tmp_path):
    html = _FIXTURE.read_text(encoding="utf-8")
    run = _make_run(tmp_path, html, ["browser/screenshot/fail-screenshot-1.png"])
    inline_report_screenshots(run)
    out1 = (run / "log.html").read_text(encoding="utf-8")
    n2 = inline_report_screenshots(run)
    out2 = (run / "log.html").read_text(encoding="utf-8")
    assert n2 == 0 and out1 == out2


def test_selenium_layout_root_screenshot(tmp_path):
    html = 'x<img src=\\"selenium-screenshot-1.png\\" />y'
    run = _make_run(tmp_path, html, ["selenium-screenshot-1.png"])
    assert inline_report_screenshots(run) == 1
    assert "data:image/png;base64," in (run / "log.html").read_text(encoding="utf-8")


def test_multiple_screenshots_all_inlined(tmp_path):
    html = ('a<img src=\\"browser/screenshot/fail-screenshot-1.png\\"/>'
            'b<img src=\\"browser/screenshot/fail-screenshot-2.png\\"/>')
    run = _make_run(tmp_path, html, [
        "browser/screenshot/fail-screenshot-1.png",
        "browser/screenshot/fail-screenshot-2.png",
    ])
    assert inline_report_screenshots(run) == 2


def test_missing_png_left_unchanged(tmp_path):
    html = 'x<img src=\\"browser/screenshot/missing.png\\"/>y'
    run = _make_run(tmp_path, html)  # no file created
    assert inline_report_screenshots(run) == 0
    assert (run / "log.html").read_text(encoding="utf-8") == html


def test_traversal_ref_not_inlined(tmp_path):
    (tmp_path / "secret.png").write_bytes(_PNG)
    html = 'x<img src=\\"../secret.png\\"/>y'
    run = _make_run(tmp_path, html)
    assert inline_report_screenshots(run) == 0
    out = (run / "log.html").read_text(encoding="utf-8")
    assert "data:" not in out and "../secret.png" in out


def test_drift_warning_when_screenshot_not_inlined(tmp_path, caplog):
    # File exists, but it is referenced via an attribute the matcher does not
    # target (data-foo, not src/href) -> not inlined -> filename remains -> WARN.
    html = 'x<span data-foo=\\"browser/screenshot/fail-screenshot-1.png\\">y</span>'
    run = _make_run(tmp_path, html, ["browser/screenshot/fail-screenshot-1.png"])
    with caplog.at_level(logging.WARNING):
        n = inline_report_screenshots(run)
    assert n == 0
    assert any("may have changed" in r.message for r in caplog.records)


def test_drift_warning_fires_for_non_png_too(tmp_path, caplog):
    # The drift detector scans every extension the matcher accepts, not just png,
    # so a jpg screenshot that fails to inline still warns (matcher/detector parity).
    html = 'x<span data-foo=\\"browser/screenshot/fail-screenshot-1.jpg\\">y</span>'
    run = _make_run(tmp_path, html, ["browser/screenshot/fail-screenshot-1.jpg"])
    with caplog.at_level(logging.WARNING):
        n = inline_report_screenshots(run)
    assert n == 0
    assert any("may have changed" in r.message for r in caplog.records)


def test_dryrun_screenshots_ignored_by_drift_check(tmp_path, caplog):
    run = _make_run(tmp_path, "no screenshots here")
    d = run / "dryrun" / "browser" / "screenshot"
    d.mkdir(parents=True)
    (d / "fail-screenshot-1.png").write_bytes(_PNG)
    with caplog.at_level(logging.WARNING):
        assert inline_report_screenshots(run) == 0
    assert not any("may have changed" in r.message for r in caplog.records)


def test_non_utf8_html_degrades_without_raising(tmp_path):
    # A run that overwrote log.html with non-UTF-8 bytes (paste-and-execute Robot
    # code can) must NOT break the module's never-raises contract: UnicodeDecodeError
    # is a ValueError, not an OSError, so it has to be caught explicitly. The png
    # on disk also forces _warn_on_uninlined to read the bad html (second site).
    run = tmp_path / str(uuid.uuid4())
    run.mkdir()
    (run / "log.html").write_bytes(b"\xff\xfe not valid utf-8 <img src=\\\"x.png\\\"/>")
    shots = run / "browser" / "screenshot"
    shots.mkdir(parents=True)
    (shots / "fail-screenshot-1.png").write_bytes(_PNG)
    assert inline_report_screenshots(run) == 0  # no exception, nothing inlined
