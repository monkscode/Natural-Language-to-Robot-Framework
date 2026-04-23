"""
Deterministic post-processor for Robot Framework code emitted by the Code Assembler.

Robot Framework treats `#` as a comment marker wherever it appears at the start of a
cell (variable value or keyword argument). A bare CSS selector like `#searchBox`
therefore becomes an empty string at parse time, and tests fail with
`locator.fill: Unexpected token "" while parsing css selector ""`.

This module deterministically prefixes bare CSS selectors (`#id`, `.class`, or combined
forms like `#id.active`, `.btn.primary`, `#id[attr='v']`) with `css=` so RF parses
them as values rather than comments.

Runs AFTER the Code Assembler returns. Complements the prompt guidance in
`library_context/browser_context.py` and `library_context/selenium_context.py` as a
belt-and-suspenders guarantee, since the LLM occasionally ignores prompt rules.

Referenced by:
    src.backend.services.workflow_service._run_crew_thread (after tasks[2] extraction)
"""

import logging
import re

logger = logging.getLogger(__name__)


# A cell qualifies as a bare CSS selector when it starts with `#` or `.` followed
# by a CSS identifier character. Anchoring on the start is enough: a cell can
# legitimately contain CSS combinators with single spaces (e.g. `#id > .child`)
# because RF cell boundaries require 2+ spaces or a tab, so single spaces live
# inside one cell. Enumerating all valid CSS syntax in a regex would be brittle;
# start-of-cell anchoring avoids that and still rejects comments like `# note`
# (space after `#` fails the second-char test).
_BARE_CSS_RE = re.compile(r"^[#.][A-Za-z_]")

# Robot Framework cell boundary: two-or-more spaces, or one-or-more tabs.
_CELL_SPLIT_RE = re.compile(r"(\s{2,}|\t+)")


def normalize_robot_code(robot_code: str) -> str:
    """
    Prefix bare CSS selectors in a Robot Framework file with `css=`.

    Only rewrites cells that look like bare CSS selectors. Leaves untouched:
      - Already-prefixed locators (css=, xpath=, id=, name=, text=, role=, ...)
      - Attribute selectors starting with `[` (already Playwright-native)
      - Full-line comments (lines whose first non-whitespace char is `#`)
      - Tag-only selectors (e.g. `body`, `input`) — ambiguous with keyword
        names, intentionally skipped

    Args:
        robot_code: The Robot Framework source as a string.

    Returns:
        The same string with bare CSS selectors prefixed. Returns the input
        unchanged if it contains no `#` or `.` characters at all (fast path).
    """
    if not robot_code or ("#" not in robot_code and "." not in robot_code):
        return robot_code

    rewrote = 0
    out_lines = []
    for line in robot_code.split("\n"):
        if line.lstrip().startswith("#"):
            # Full-line comment — preserve verbatim.
            out_lines.append(line)
            continue

        parts = _CELL_SPLIT_RE.split(line)
        for i, cell in enumerate(parts):
            if cell and _BARE_CSS_RE.match(cell):
                parts[i] = f"css={cell}"
                rewrote += 1
        out_lines.append("".join(parts))

    if rewrote:
        logger.info(f"Locator normalizer: prefixed {rewrote} bare CSS selector(s) with `css=`")
    return "\n".join(out_lines)
