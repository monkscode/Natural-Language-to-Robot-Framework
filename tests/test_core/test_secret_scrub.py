"""Task 6 of #131: a secret's value is masked in a finished run's report folder."""

import base64
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from unittest.mock import patch

import pytest

from src.backend.core.secret_scrub import ScrubError, mask_values, scrub_run_dir
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
        '<status status="FAIL" start="2026-10-23T10:00:01.123400" elapsed="0.5">boom</status>\n'
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
    # RF 7.4.2's own shape: only "</" is escaped, so "<p>" stays raw and "</p>" is "\x3c/p>".
    arr = json.dumps(entries).replace("</", "\\x3c/")
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
        _html([
            "*",
            f"*Typed {value} here",
            _z(f"<b>{value}</b> compressed"),
            "*plain",
            "*a\\x41b",
            f"*<b>{value}</b>",  # lands in the file as "*<b>VALUE\x3c/b>", RF's mixed form
        ]),
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
    assert "\\x3c/b\\x3e" in arr  # the closing tag is written back in the escaped form
    decoded = _decoded_strings(text)
    assert len(decoded) == 6
    assert decoded[1] == "Typed *** here"
    assert decoded[2] == "<b>***</b> compressed"
    assert decoded[4] == "a\\x41b"  # an escaped backslash before x41 is not a \x escape
    assert decoded[5] == "<b>***</b>"  # a raw entry read through the \x3c escape


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


def test_one_unscrubbable_file_does_not_stop_the_others(tmp_path):
    (tmp_path / "output.xml").write_text(_xml(MARK), encoding="utf-8")
    (tmp_path / "log.html").write_text(_html(["*", "*x", "AAAA"]), encoding="utf-8")  # not zlib
    (tmp_path / "playwright-log.txt").write_text(f'fill("{MARK}")\n', encoding="utf-8")
    with pytest.raises(ScrubError) as err:
        scrub_run_dir(tmp_path, [MARK])
    assert err.value.files == ["log.html"]
    assert "log.html" in str(err.value) and MARK not in str(err.value)
    assert MARK not in (tmp_path / "output.xml").read_text(encoding="utf-8")
    assert MARK not in (tmp_path / "playwright-log.txt").read_text(encoding="utf-8")


def test_returned_names_are_relative_paths(tmp_path):
    (tmp_path / "dryrun").mkdir()
    (tmp_path / "dryrun" / "playwright-log.txt").write_text(MARK, encoding="utf-8")
    (tmp_path / "playwright-log.txt").write_text(MARK, encoding="utf-8")
    assert sorted(scrub_run_dir(tmp_path, [MARK])) == ["dryrun/playwright-log.txt", "playwright-log.txt"]


def test_xml_bookkeeping_attributes_are_never_masked(tmp_path):
    (tmp_path / "output.xml").write_text(_xml("FAIL"), encoding="utf-8")
    scrub_run_dir(tmp_path, ["FAIL"])
    root = ET.parse(tmp_path / "output.xml").getroot()
    assert {s.get("status") for s in root.iter("status")} == {"FAIL"}
    parsed = OutputXmlParser().parse(str(tmp_path / "output.xml"))
    assert parsed["test_status"] == "failed"
    assert parsed["keyword_chain"][0].status == "FAIL"
    assert root.find(".//arg").text == "***"  # text is still masked


def test_xml_timestamp_survives_a_numeric_value(tmp_path):
    (tmp_path / "output.xml").write_text(_xml("1234"), encoding="utf-8")
    scrub_run_dir(tmp_path, ["1234"])
    root = ET.parse(tmp_path / "output.xml").getroot()
    assert root.find(".//kw/status").get("start") == "2026-10-23T10:00:01.123400"
    assert root.find(".//kw").get("note") == "***"  # a non-bookkeeping attribute is masked


def test_xml_name_attribute_is_still_masked(tmp_path):
    (tmp_path / "output.xml").write_text(
        _xml(MARK).replace('name="Login"', f'name="Login {MARK}"'), encoding="utf-8"
    )
    scrub_run_dir(tmp_path, [MARK])
    root = ET.parse(tmp_path / "output.xml").getroot()
    assert root.find(".//test").get("name") == "Login ***"


@pytest.mark.parametrize("value,attr", [
    ("AR", "type"), ("Browser", "owner"), ("1", "pass"), ("1", "fail"), ("0", "skip"),
    ("true", "html"), ("TEST", "flavor"),
])
def test_xml_robot_attributes_the_parsers_read_are_never_masked(tmp_path, value, attr):
    (tmp_path / "output.xml").write_text(
        '<robot><kw name="K" owner="Browser" type="TEARDOWN"><msg html="true" level="INFO">'
        f'{value}</msg></kw><stat pass="1" fail="1" skip="0" flavor="TEST">{value}</stat></robot>',
        encoding="utf-8",
    )
    scrub_run_dir(tmp_path, [value])
    root = ET.parse(tmp_path / "output.xml").getroot()
    kw, msg, stat = root.find("kw"), root.find(".//msg"), root.find("stat")
    assert (kw.get("type"), kw.get("owner")) == ("TEARDOWN", "Browser")
    assert (msg.get("html"), stat.get("pass"), stat.get("fail"), stat.get("skip"), stat.get("flavor")) == (
        "true", "1", "1", "0", "TEST")
    assert msg.text == "***" and stat.text == "***"  # text is still masked (positive control)


def _link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this host cannot create symlinks without a privilege")


def test_scrub_never_follows_a_symlink_out_of_the_run_folder(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text(f"keep {MARK}\n", encoding="utf-8")
    before = outside.read_bytes()
    _link(run / "x.log", outside)
    (run / "real.log").write_text(f"fill {MARK}\n", encoding="utf-8")
    changed = scrub_run_dir(run, [MARK])
    assert changed == ["real.log"]  # the link is neither changed nor failed
    assert outside.read_bytes() == before
    assert (run / "real.log").read_text(encoding="utf-8") == "fill ***\n"


def test_scrub_skips_a_path_that_resolves_outside_even_without_a_symlink_flag(tmp_path):
    """The branch the privilege-less hosts cannot reach through os.symlink: simulate it."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "a.log").write_text(MARK, encoding="utf-8")
    (run / "b.log").write_text(MARK, encoding="utf-8")
    outside = str(tmp_path / "elsewhere" / "b.log")
    real = os.path.realpath

    def fake_realpath(p, *a, **k):
        return outside if str(p).endswith("b.log") else real(p, *a, **k)

    with patch("src.backend.core.secret_scrub.os.path.realpath", fake_realpath):
        changed = scrub_run_dir(run, [MARK])
    assert changed == ["a.log"]
    assert (run / "b.log").read_text(encoding="utf-8") == MARK


def test_scrub_skips_a_path_reported_as_a_symlink(tmp_path):
    (tmp_path / "a.log").write_text(MARK, encoding="utf-8")
    with patch.object(Path, "is_symlink", lambda self: self.name == "a.log"):
        assert scrub_run_dir(tmp_path, [MARK]) == []
    assert (tmp_path / "a.log").read_text(encoding="utf-8") == MARK


def test_logged_file_names_are_masked(tmp_path, caplog):
    (tmp_path / f"{MARK}.log").write_text(MARK, encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="src.backend.core.secret_scrub"):
        scrub_run_dir(tmp_path, [MARK])
    assert MARK not in caplog.text
    assert "***.log" in caplog.text
