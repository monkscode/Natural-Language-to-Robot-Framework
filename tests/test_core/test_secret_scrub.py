"""Task 6 of #131: a secret's value is masked in a finished run's report folder."""

import base64
import json
import re
import zlib
from pathlib import Path

from src.backend.core.secret_scrub import mask_values, scrub_run_dir
from src.backend.crew_ai.optimization.failure_analyzer import OutputXmlParser

MARK = "pw_marker_131"
_ARRAY = re.compile(
    r'window\.output\["strings"\]\.concat\((\[(?:"(?:[^"\\]|\\.)*"\s*,?\s*)*\])\);'
)


def _xml(value: str, tag_text: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<robot generator="Robot 7.4.2" generated="2026-10-23T10:00:00.000000" rpa="false" schemaversion="5">\n'
        '<suite id="s1" name="T" source="/app/t.robot">\n'
        '<test id="s1-t1" name="Login" line="3">\n'
        f'<kw name="Fill Secret" owner="Browser" note="{value}">\n'
        f'<arg>{value}</arg>\n'
        f'<msg time="2026-10-23T10:00:01.000000" level="INFO">Typing {value} {tag_text}</msg>\n'
        '<status status="FAIL" start="2026-10-23T10:00:01.000000" elapsed="0.5">boom</status>\n'
        '</kw>\n'
        '<status status="FAIL" start="2026-10-23T10:00:00.000000" elapsed="1.0">boom</status>\n'
        '</test>\n'
        '<status status="FAIL" start="2026-10-23T10:00:00.000000" elapsed="1.0"/>\n'
        '</suite>\n'
        '</robot>\n'
    )


def _z(text: str) -> str:
    return base64.b64encode(zlib.compress(text.encode("utf-8"), 9)).decode("ascii")


def _html(entries: list[str]) -> str:
    arr = json.dumps(entries).replace("<", "\\x3c").replace(">", "\\x3e")
    return (
        "<html><body><script>\nwindow.output = {};\n"
        'window.output["strings"] = [];\n'
        f'window.output["strings"] = window.output["strings"].concat({arr});\n'
        "</script></body></html>\n"
    )


def _decoded_strings(html_text: str) -> list[str]:
    arr = json.loads(
        re.sub(
            r"\\(\\|x([0-9a-fA-F]{2}))",
            lambda m: m.group(0) if m.group(2) is None else "\\u00" + m.group(2),
            _ARRAY.search(html_text).group(1),
        )
    )
    return [
        s[1:] if s.startswith("*") else zlib.decompress(base64.b64decode(s)).decode("utf-8")
        for s in arr
    ]


def _all_text(run_dir: Path) -> str:
    parts = []
    for p in sorted(run_dir.rglob("*")):
        if p.is_file() and p.suffix != ".png":
            text = p.read_text(encoding="utf-8")
            parts.append(text)
            if p.suffix == ".html":
                parts.extend(_decoded_strings(text))
    return "\n".join(parts)


def _make_run(tmp_path: Path, value: str = MARK) -> Path:
    (tmp_path / "output.xml").write_text(_xml(value), encoding="utf-8")
    (tmp_path / "log.html").write_text(
        _html(["*", f"*Typed {value} here", _z(f"<b>{value}</b> compressed"), "*plain", "*a\\x41b"]),
        encoding="utf-8",
    )
    (tmp_path / "playwright-log.txt").write_text(f'fill("{value}")\n', encoding="utf-8")
    (tmp_path / "shot.png").write_bytes(b"\x89PNG" + value.encode() + b"\x00")
    return tmp_path


def test_scrub_removes_value_everywhere_including_compressed_strings(tmp_path):
    run = _make_run(tmp_path)
    assert MARK in _all_text(run)
    changed = scrub_run_dir(run, [MARK])
    assert sorted(changed) == ["log.html", "output.xml", "playwright-log.txt"]
    assert MARK not in _all_text(run)
    assert MARK.encode() in (run / "shot.png").read_bytes()  # images untouched


def test_scrubbed_output_xml_still_parses_with_same_status(tmp_path):
    run = _make_run(tmp_path)
    before = OutputXmlParser().parse(str(run / "output.xml"))
    scrub_run_dir(run, [MARK])
    after = OutputXmlParser().parse(str(run / "output.xml"))
    assert after["test_status"] == before["test_status"] == "failed"
    assert before["failed_keyword_args"] == [MARK]
    assert after["failed_keyword_args"] == ["***"]


def test_log_html_array_keeps_length_and_script_safe(tmp_path):
    run = _make_run(tmp_path)
    scrub_run_dir(run, [MARK])
    text = (run / "log.html").read_text(encoding="utf-8")
    arr = _ARRAY.search(text).group(1)
    assert "<" not in arr and ">" not in arr
    decoded = _decoded_strings(text)
    assert len(decoded) == 5
    assert decoded[1] == "Typed *** here"
    assert decoded[2] == "<b>***</b> compressed"
    assert decoded[4] == "a\\x41b"  # an escaped backslash before x41 is not a \x escape


def test_unchanged_array_left_byte_for_byte(tmp_path):
    src = _html(["*", "*nothing here", _z("also nothing")])
    (tmp_path / "log.html").write_text(src, encoding="utf-8")
    assert scrub_run_dir(tmp_path, [MARK]) == []
    assert (tmp_path / "log.html").read_text(encoding="utf-8") == src


def test_scrub_value_that_is_a_tag_name(tmp_path):
    (tmp_path / "output.xml").write_text(_xml("status", "status"), encoding="utf-8")
    entries = ["*", "*status", _z("<status>status</status>")]
    (tmp_path / "log.html").write_text(_html(entries), encoding="utf-8")
    scrub_run_dir(tmp_path, ["status"])
    parsed = OutputXmlParser().parse(str(tmp_path / "output.xml"))
    assert parsed["test_status"] == "failed"
    text = (tmp_path / "log.html").read_text(encoding="utf-8")
    decoded = _decoded_strings(text)
    assert len(decoded) == 3
    assert not any("status" in s for s in decoded)
    assert (tmp_path / "output.xml").read_text(encoding="utf-8").count("<status ") == 3


def test_nested_values_leave_no_tail():
    out = mask_values("x abcd1234 y abcd z", ["abcd", "abcd1234"])
    assert out == "x *** y *** z"
    assert "1234" not in out


def test_special_characters_masked_in_escaped_forms():
    v = 'pw&"<x'
    text = 'raw pw&"<x | html pw&amp;&quot;&lt;x | json pw&\\"<x'
    assert mask_values(text, [v]) == "raw *** | html *** | json ***"


def test_empty_values_change_nothing(tmp_path):
    run = _make_run(tmp_path)
    snap = {p.name: p.read_bytes() for p in run.iterdir()}
    assert scrub_run_dir(run, []) == []
    assert scrub_run_dir(run, [""]) == []
    assert {p.name: p.read_bytes() for p in run.iterdir()} == snap
    assert mask_values("abc", [""]) == "abc"
