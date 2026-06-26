"""
Deterministic post-processor for Robot Framework code emitted by the Code Assembler.

Two problems motivate this pass:

1. Parse-level (`#`): Robot Framework treats `#` at the start of a cell as a
   comment marker. A bare CSS selector like `#searchBox` becomes an empty
   string at parse time, and tests fail with
   `locator.fill: Unexpected token "" while parsing css selector ""`.

2. Strategy-level (`.`): Locators without an explicit strategy prefix fall
   back to each library's default strategy. SeleniumLibrary's implicit
   strategy tries id/name/identifier first and may mis-route `.button`;
   Browser Library defaults to CSS but gains nothing from ambiguity either.
   Prefixing with `css=` makes the strategy explicit for both libraries.

This module deterministically prefixes bare CSS selectors (`#id`, `.class`, or combined
forms like `#id.active`, `.btn.primary`, `#id[attr='v']`) with `css=` — but ONLY at
cell positions that are actually locator arguments. Non-locator data such as tag
values, assertion values, or the text argument of `Fill Text` is never rewritten.

Scope rules (conservative: no rewrite beats a wrong rewrite):
    - Keyword calls: rewrite only the arg positions declared in _LOCATOR_KEYWORDS.
      Unknown keywords cause the line to be left untouched.
    - Variable declarations (assignment prefix with no recognized keyword): rewrite
      any bare-CSS cell after the assignment. Matches the common pattern
      `${SEARCH}    #searchBox` in *** Variables *** sections.
    - Setting markers ([Tags], [Documentation], ...): always skipped.
    - Full-line comments and `...` continuations: preserved verbatim.

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

# Robot Framework cell boundary: two-or-more spaces, or one-or-more tabs — RF's
# actual separator rule. Branches are disjoint character classes (space vs tab)
# with possessive quantifiers, so the alternation cannot backtrack and has no
# overlapping match (the old `\s`-based form let a tab match both branches, a
# polynomial-ReDoS shape on the LLM-generated input this runs over per line).
# re.split keeps the separators, so every line is rejoined byte-for-byte.
_CELL_SPLIT_RE = re.compile(r"( {2,}+|\t++)")

# Variable-assignment prefix: a cell that is exactly a scalar/list/dict variable
# (`${x}`, `@{list}`, `&{dict}`) with an optional trailing `=`. Used to detect
# lines of the form `${x}=    Keyword    ...` or `${X}    value` in Variables.
# Possessive quantifiers ([^}]++ / \s*+) make this non-backtracking: the prior
# `\s*=?\s*$` tail could take seconds on a long whitespace run that fails the
# end-anchor (a ReDoS reachable from LLM-generated input); possessive forms run
# in linear time with an identical matched language (verified by fuzzing).
_ASSIGN_PREFIX_RE = re.compile(r"^[$@&]\{[^}]++\}\s*+=?\s*+$")

# Setting markers. When these are the first cell of a line, the rest of the line
# holds literal values, never locators — never rewrite.
_SETTING_MARKERS = frozenset({
    "[tags]", "[documentation]", "[arguments]", "[setup]", "[teardown]",
    "[timeout]", "[template]", "[return]",
})


def _canon_keyword(name: str) -> str:
    """Canonicalize an RF keyword cell: lowercase, single-space between words,
    library prefix stripped. RF itself is case- and space-insensitive for keyword
    names, so `Fill Text`, `fill_text`, and `fill  text` all map to `fill text`.
    """
    stripped = re.sub(r"[\s_]+", " ", name.strip().lower())
    # Strip library prefix (e.g., "browser.click" -> "click"). A leading `.` is
    # a bare CSS selector — not a library prefix — so guard against that.
    if "." in stripped and not stripped.startswith("."):
        stripped = stripped.rsplit(".", 1)[-1].strip()
    return stripped


# Keyword -> tuple of 0-indexed argument positions that accept a locator.
# Any argument at a position NOT listed here (e.g., the text arg at position 1
# of Fill Text) is left untouched. Keywords not in this dict cause the whole
# line to skip keyword-path rewriting (see normalize_robot_code for the variable-
# declaration fallback).
_LOCATOR_KEYWORDS: dict[str, tuple[int, ...]] = {
    # Browser Library (Playwright-based)
    "click": (0,),
    "click with options": (0,),
    "fill text": (0,),
    "fill secret": (0,),
    "type text": (0,),
    "type secret": (0,),
    "check checkbox": (0,),
    "uncheck checkbox": (0,),
    "select options by": (0,),
    "hover": (0,),
    "focus": (0,),
    "press keys": (0,),
    "get text": (0,),
    "get attribute": (0,),
    "get property": (0,),
    "get style": (0,),
    "get element": (0,),
    "get elements": (0,),
    "get element count": (0,),
    "get element states": (0,),
    "get select options": (0,),
    "wait for elements state": (0,),
    "scroll to element": (0,),
    "upload file by selector": (0,),
    # SeleniumLibrary
    "click element": (0,),
    "click button": (0,),
    "click link": (0,),
    "click image": (0,),
    "input text": (0,),
    "input password": (0,),
    "select checkbox": (0,),
    "unselect checkbox": (0,),
    "select from list by label": (0,),
    "select from list by index": (0,),
    "select from list by value": (0,),
    "mouse over": (0,),
    "mouse out": (0,),
    "submit form": (0,),
    "choose file": (0,),
    "clear element text": (0,),
    "scroll element into view": (0,),
    "wait until element is visible": (0,),
    "wait until element is enabled": (0,),
    "wait until element is not visible": (0,),
    "wait until element contains": (0,),
    "wait until page contains element": (0,),
    "wait until page does not contain element": (0,),
    "element should be visible": (0,),
    "element should be enabled": (0,),
    "element should be disabled": (0,),
    "element should contain": (0,),
    "element should not contain": (0,),
    "element should not be visible": (0,),
    "get webelement": (0,),
    "get webelements": (0,),
    "get element attribute": (0,),
    # Two-locator keywords
    "drag and drop": (0, 1),
}


def normalize_robot_code(robot_code: str) -> str:
    """
    Prefix bare CSS selectors in Robot Framework code with `css=`.

    Rewrites only cells that are actually locator arguments:
      - Locator args of known keywords (per _LOCATOR_KEYWORDS)
      - Values of variable declarations (`${X}    #foo` and `${x}=    <unknown-kw>    #foo`)

    Leaves untouched:
      - Non-locator args (e.g., the text arg of `Fill Text`)
      - Setting marker lines (`[Tags]`, `[Documentation]`, ...)
      - Assertion/log values (`Should Be Equal    ${v}    .NET`)
      - Full-line comments and `...` continuations
      - Custom/unknown keywords (conservative — no rewrite)

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
        stripped = line.lstrip()

        # Full-line comment or continuation — preserve verbatim.
        if stripped.startswith("#") or stripped.startswith("..."):
            out_lines.append(line)
            continue

        parts = _CELL_SPLIT_RE.split(line)
        # Indices of content cells (non-empty, non-separator).
        cell_positions = [
            i for i, p in enumerate(parts)
            if p and not _CELL_SPLIT_RE.fullmatch(p)
        ]
        if not cell_positions:
            out_lines.append(line)
            continue

        first_cell = parts[cell_positions[0]]

        # Setting marker lines never contain locators.
        if first_cell.lower() in _SETTING_MARKERS:
            out_lines.append(line)
            continue

        # Walk past leading assignment cells to find the keyword cell.
        keyword_pos_in_cells = 0
        for k, ci in enumerate(cell_positions):
            if _ASSIGN_PREFIX_RE.match(parts[ci]):
                keyword_pos_in_cells = k + 1
                continue
            break

        # --- Case A: recognized keyword call ---
        if keyword_pos_in_cells < len(cell_positions):
            keyword_cell = parts[cell_positions[keyword_pos_in_cells]]
            locator_arg_positions = _LOCATOR_KEYWORDS.get(_canon_keyword(keyword_cell))
            if locator_arg_positions is not None:
                arg_cells_start = keyword_pos_in_cells + 1
                for offset in locator_arg_positions:
                    arg_pos = arg_cells_start + offset
                    if arg_pos >= len(cell_positions):
                        continue
                    target_idx = cell_positions[arg_pos]
                    cell = parts[target_idx]
                    if _BARE_CSS_RE.match(cell):
                        parts[target_idx] = f"css={cell}"
                        rewrote += 1
                out_lines.append("".join(parts))
                continue

        # --- Case B: variable declaration (assignment prefix, no known keyword) ---
        # Common in *** Variables *** sections: `${SEARCH}    #searchBox`.
        # Also covers `${x}=    Set Variable    #foo` (Set Variable isn't in the
        # allowlist, but the value being stored is a locator).
        if keyword_pos_in_cells > 0:
            for ci in cell_positions[keyword_pos_in_cells:]:
                cell = parts[ci]
                if _BARE_CSS_RE.match(cell):
                    parts[ci] = f"css={cell}"
                    rewrote += 1
            out_lines.append("".join(parts))
            continue

        # --- Case C: unrecognized line (custom keyword or section header) ---
        # Conservative: leave untouched. A custom user-defined keyword whose
        # first arg is a locator will miss rewriting here, but that's safer
        # than corrupting legitimate non-locator data.
        out_lines.append(line)

    if rewrote:
        logger.info(f"Locator normalizer: prefixed {rewrote} bare CSS selector(s) with `css=`")
    return "\n".join(out_lines)
