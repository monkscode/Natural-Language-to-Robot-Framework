"""Put the held-out sweep's judge line into a generated single-test Robot file.

The judge is one Browser keyword line that reads the element the query is about.
It goes before the test's closing `Close Browser` (the page is still open), or
after the test's last line when the test never closes the browser.

Referenced by: bench/sweep.py
Depends on: src/backend/crew_ai/robot_code_normalizer.py (_parse_suite, _BROWSER_PREFIXES)
"""

from src.backend.crew_ai.robot_code_normalizer import _BROWSER_PREFIXES, _parse_suite


_BLOCK_OPENERS = frozenset({"FOR", "WHILE", "TRY", "GROUP"})  # each is closed by END


class JudgeRefused(ValueError):
    """The file cannot take a judge line safely; the message says why."""


def _block_change(step) -> int:
    """+1 when the step opens a FOR/WHILE/TRY/block-IF, -1 for END, else 0."""
    if step.setting:
        return 0
    word = step.parts[step.keyword_idx].strip()
    if word in _BLOCK_OPENERS or (word == "IF" and len(step.args) == 1):  # more cells = inline IF, no END
        return 1
    return -1 if word == "END" else 0


def _top_level_closes(test) -> list:
    """The test's Close Browser steps at block depth 0 (inside a loop one runs per pass).
    Refuses a test whose blocks do not balance."""
    depth = 0
    closes = []
    for step in test.steps:
        change = _block_change(step)
        depth += change
        if depth < 0:
            break
        # shortcut: a continued Close Browser (a "..." line after it) is not an anchor, so the judge goes after
        # the test's last line; upgrade if a real generated test shows it
        is_close = (not step.setting and not step.continued and change == 0 and depth == 0
                    and step.keyword.replace(" ", "") == "closebrowser" and step.prefix in _BROWSER_PREFIXES)
        if is_close:
            closes.append(step)
    if depth != 0:
        raise JudgeRefused("the test's FOR/WHILE/TRY/IF blocks are unbalanced")
    return closes


def insert_judge(robot_code: str, judge_line: str) -> str:
    """robot_code with judge_line added to its one test, in the file's own line ending."""
    suite = _parse_suite(robot_code)
    if suite is None:
        raise JudgeRefused("the file does not parse with the normalizer's reader")
    if len(suite.tests) != 1:
        raise JudgeRefused(f"expected exactly one test, found {len(suite.tests)} tests")
    test = suite.tests[0]
    if not test.line_nos:
        raise JudgeRefused("the test has no body line to place the judge next to")

    closes = _top_level_closes(test)
    lines = list(suite.lines)
    if closes:
        at = closes[-1].line_no  # insert before this line
    else:
        at = test.line_nos[-1] + 1  # insert after the test's last line
    neighbour = lines[at if closes else at - 1]
    indent = neighbour[: len(neighbour) - len(neighbour.lstrip())]
    crlf = "\r\n" in robot_code
    if at == len(lines):  # the test's last line ends the file with no line break
        lines[-1] += "\r" if crlf else ""
        lines.append(indent + judge_line)
    else:
        lines.insert(at, indent + judge_line + ("\r" if crlf else ""))
    return "\n".join(lines)
