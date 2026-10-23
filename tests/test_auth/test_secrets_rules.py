"""Pure rule tests for the org secret store (no DB): names, values, .env parsing."""
import pytest

from src.backend.auth.secrets import (
    SecretRuleError,
    check_value,
    normalize_name,
    parse_env_file,
    secret_names_in_code,
    secret_names_in_query,
)

MARKER = "pw_marker_131"

_PROBE_LINES = [
    "# the probe file",                      # 1
    f"SAUCE_PASSWORD={MARKER}",              # 2
    'QUOTED="two words"',                    # 3
    "export EXPORTED=exp1",                  # 4
    "BAD LINE WITHOUT EQUALS",               # 5
    "HOME_REF=${HOME}",                      # 6
    "",                                      # 7
    "  SPACED = sp1  ",                      # 8
    "LAST=end1",                             # 9
]
_GOOD_PAIRS = [
    ("SAUCE_PASSWORD", MARKER),
    ("QUOTED", "two words"),
    ("EXPORTED", "exp1"),
    ("HOME_REF", "${HOME}"),
    ("SPACED", "sp1"),
    ("LAST", "end1"),
]


def test_normalize_name_uppercases():
    assert normalize_name("sauce_password") == "SAUCE_PASSWORD"


@pytest.mark.parametrize("bad", ["9X", "A-B", "", "A" * 65])
def test_normalize_name_rejects(bad):
    with pytest.raises(SecretRuleError):
        normalize_name(bad)


def test_normalize_name_accepts_64_chars():
    assert normalize_name("a" * 64) == "A" * 64


def test_check_value_bounds():
    check_value("a")
    check_value("x" * 1000)
    for bad in ("", "x" * 1001):
        with pytest.raises(SecretRuleError):
            check_value(bad)


def test_parse_env_file_good_lines_in_order():
    lines = [ln for i, ln in enumerate(_PROBE_LINES, 1) if i != 5]
    assert parse_env_file("\n".join(lines)) == _GOOD_PAIRS


def test_parse_env_file_bad_line_names_line_only():
    with pytest.raises(SecretRuleError) as exc:
        parse_env_file("\n".join(_PROBE_LINES))
    assert str(exc.value).startswith("Line 5:")
    assert MARKER not in str(exc.value)


def test_parse_env_file_windows_bom_and_crlf():
    assert parse_env_file(f"﻿A_B={MARKER}\r\nC=xyz1\r\n") == [("A_B", MARKER), ("C", "xyz1")]


@pytest.mark.parametrize("text, line", [
    ("OK=1\nNOVALUE\n", 2),
    ("EMPTY=\n", 1),
    ("OK=1\n9BAD=2\n", 2),
    (f"OK=1\nBIG={'x' * 1001}\n", 2),
])
def test_parse_env_file_rejects_line(text, line):
    with pytest.raises(SecretRuleError) as exc:
        parse_env_file(text)
    assert str(exc.value).startswith(f"Line {line}:")


def test_parse_env_file_size_cap():
    big = "".join(f"K{i}={'x' * 100}\n" for i in range(700))
    assert len(big.encode()) > 65536
    with pytest.raises(SecretRuleError):
        parse_env_file(big)


def test_parse_env_file_duplicate_last_wins():
    assert parse_env_file("A=one\nB=b\nA=two\n") == [("A", "two"), ("B", "b")]


def test_secret_names_in_query():
    assert secret_names_in_query("type {{ sauce_password }} and {{X1}} and {{X1}}") == [
        "SAUCE_PASSWORD", "X1"]
    assert secret_names_in_query("no placeholders {here}") == []


def test_secret_names_in_code():
    code = "Fill Secret  id=pw  %NLRF_SECRET_A\nLog  %{NLRF_SECRET_B}"
    assert secret_names_in_code(code) == ["A", "B"]


@pytest.mark.parametrize("bad", [f"a\nb{MARKER}", f"a\r{MARKER}", f"{MARKER}\r\n"])
def test_check_value_refuses_a_line_break(bad):
    with pytest.raises(SecretRuleError) as exc:
        check_value(bad)
    assert "line break" in str(exc.value)
    assert MARKER not in str(exc.value)


def test_parse_env_file_refuses_a_quoted_value_spanning_lines():
    text = f'OK=1\nTWO_LINES="first\n{MARKER}"\nLAST=2\n'
    with pytest.raises(SecretRuleError) as exc:
        parse_env_file(text)
    assert str(exc.value).startswith("Line 2:")
    assert "line break" in str(exc.value)
    assert MARKER not in str(exc.value)
