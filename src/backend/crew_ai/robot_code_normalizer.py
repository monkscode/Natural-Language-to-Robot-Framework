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
`library_context/browser_context.py` as a belt-and-suspenders guarantee, since
the LLM occasionally ignores prompt rules.

Referenced by:
    src.backend.services.workflow_service._run_crew_thread (after tasks[-1] extraction)
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

# Robot Framework cell boundary: two-or-more spaces, or one-or-more tabs (a lone
# tab is a valid RF separator — see the tab-separated-cells test). Possessive
# quantifiers (\s{2,}+ / \t++) make every quantifier non-backtracking, so the
# match runs in linear time (verified: 0.017ms on an 80k-char whitespace run)
# with a matched language identical to the original `(\s{2,}|\t+)`.
#
# NOTE: SonarCloud python:S5852 flags this as a polynomial-ReDoS hotspot. That
# is a false positive — it fires on the `|` alternation regardless of the
# possessive quantifiers that make backtracking impossible, and the alternation
# is unavoidable (tab separators need their own branch). Resolve it by marking
# the hotspot "Safe" in SonarCloud, not by removing tab support.
_CELL_SPLIT_RE = re.compile(r"(\s{2,}+|\t++)")

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


# Browser Library's import default is `timeout=10s` (verified against the runner
# image's libdoc, Browser 19.14.2). Every timeout in the bench failure corpus is
# exactly 10000ms, and 0 of 797 generated tests set a timeout, so the default
# stands everywhere. 30s is a ceiling, not a wait: `get_timeout()` returns it as an
# upper bound and Playwright proceeds the instant the element is actionable, so
# passing runs are unaffected.
#
# The import is the only lever that reaches navigation: `New Page(url, wait_until)`
# has no timeout parameter and accounts for 26 of 33 navigation-timeout failures.
_BROWSER_TIMEOUT = "30s"

# Matches ONLY a bare `Library  Browser` import — the line must end right after the
# library name. An import that already carries arguments (`timeout=`, `AS`, or
# anything else) is left untouched, which makes the injection idempotent across the
# dryrun repair path's second pass. `Browser.Playwright` is a different library and
# is excluded by the end-of-line anchor. `\r` is captured so CRLF files survive.
#
# The casing is asymmetric on purpose — measured in the runner image, not assumed:
#   `library    Browser`  imports fine (the SETTING name is case-insensitive)
#   `Library    browser`  dies with ModuleNotFoundError: No module named 'browser'
#                         (the LIBRARY name is a Python module — case-sensitive)
# The separator is RF's cell rule (2+ spaces or a tab), the same rule _CELL_SPLIT_RE
# encodes below. `Library Browser` with one space is not an import at all — RF reads
# it as a setting literally named "Library Browser" and errors — so it is left alone
# rather than rewritten into something equally broken.
# Possessive quantifiers per this module's convention (see _CELL_SPLIT_RE): every
# quantifier is non-backtracking, so the match is linear regardless of input.
_BARE_BROWSER_IMPORT_RE = re.compile(
    r"^((?i:Library)(?:[ \t]{2,}+|\t++)Browser)[ \t]*+(\r?)$", re.MULTILINE
)


def ensure_browser_timeout(robot_code: str) -> str:
    """Emit `timeout=30s` on a bare `Library    Browser` import.

    Deterministic rather than a prompt rule: an assembler instruction would cost
    tokens on every run and be honoured inconsistently.

    Leaves the code unchanged when the import already carries any argument, when
    the import is absent, or when the suite uses SeleniumLibrary.

    Args:
        robot_code: The Robot Framework source as a string.

    Returns:
        The same string with the timeout argument appended to a bare Browser import.
    """
    if not robot_code:
        return robot_code

    injected, count = _BARE_BROWSER_IMPORT_RE.subn(
        rf"\1    timeout={_BROWSER_TIMEOUT}\2", robot_code
    )
    if count:
        logger.info(f"Browser timeout: set timeout={_BROWSER_TIMEOUT} on the Browser import")
    return injected


# Robot Framework locator strategies that the identify stage and the assembler
# both emit. A cell of the form `css=<strategy>=value` is the assembler applying
# the "always prefix CSS selectors with css=" prompt rule to a locator that
# already carries a strategy prefix. The result is never valid CSS — Playwright
# rejects it with `Unexpected token "=" while parsing css selector` — so the
# redundant `css=` is always safe to drop.
#
# The strategy name must sit immediately after `css=` and be followed by `=`,
# which is what keeps genuine CSS untouched: `css=[data-x=y]` starts with `[`,
# `css=input[id=foo]` starts with `input`, and `css=idx=5` fails because `id`
# is not followed by `=`. `css=` is included in the alternation because
# `css=css=#foo` is the same mistake applied to an already-css locator.
#
# `(?:css=)+` consumes the WHOLE run of prefixes in one match, which single
# `css=` could not: `re.subn` does not rescan replaced text, so stripping only
# the outermost left the next `css=` preceded by `=`, where the boundary rule
# below rejects it — `css=css=id=searchBox` came out as `css=id=searchBox`,
# still not valid CSS. The repetition is greedy and the lookahead still has to
# hold after it, so backtracking leaves exactly one `css=` when the locator
# underneath is genuinely css: `css=css=css=#foo` -> `css=#foo`, while
# `css=css=css=xpath=//tbody/tr` -> `xpath=//tbody/tr`.
#
# The match must also START a cell — line start, or immediately after a Robot
# cell separator (tab, or two spaces). A stacked prefix is only ever the first
# thing in the locator cell; the same sequence further in is part of a value
# that was written correctly, and rewriting it silently changes what the test
# selects. `css=[data-value="css=id=x"]` and `xpath=//div[@a="css=id=y"]` are
# both valid and are both left alone by the boundary requirement — the run
# length makes no difference to that, `css=[data-value="css=css=id=x"]` is
# left alone for the same reason.
_REDUNDANT_CSS_PREFIX_RE = re.compile(
    r"(?:^|(?<=\t)|(?<= {2}))(?:css=)+(?=(?:id|xpath|text|role|data-testid|css)=)",
    re.MULTILINE,
)


def strip_redundant_css_prefix(robot_code: str) -> str:
    """Drop a `css=` prefix that was stacked onto an already-prefixed locator.

    Deterministic counterpart to the prompt carve-out in
    `library_context/browser_context.py`: the prompt lowers how often the model
    makes this mistake, this guarantees the mistake never reaches the runner.
    The `robot --dryrun` gate cannot cover it — dryrun validates keyword names
    and arity without resolving selectors, so `css=id=searchBox` passes the gate
    and fails only at runtime.

    Scope note — this does not walk cells the way `normalize_robot_code` does,
    because it does not need to: `css=<strategy>=` is not valid CSS, not a
    valid Robot locator and not plausible prose, so a bare match is far less
    ambiguous than the bare `#` that forces the cell walk there. It does still
    require the match to START a cell, which is where a stacked prefix can
    only ever appear. Mid-cell the same sequence is part of a value that was
    already written correctly, and rewriting it would silently change what the
    test selects — `css=[data-value="css=id=x"]` is valid CSS and
    `xpath=//div[@a="css=id=y"]` is a valid xpath.

    What the boundary rule still cannot separate is a cell that is genuinely
    prose yet starts with the sequence, e.g. a `[Documentation]` value of
    exactly `css=id=foo`. Telling that from a locator needs the row's keyword,
    which is the cell walk. Accepted: generated output does not contain it.

    Args:
        robot_code: The Robot Framework source as a string.

    Returns:
        The same string with every `css=<strategy>=` collapsed to `<strategy>=`.
        Returns the input unchanged when it contains no `css=` at all.
    """
    if not robot_code or "css=" not in robot_code:
        return robot_code

    stripped, cells = _REDUNDANT_CSS_PREFIX_RE.subn("", robot_code)
    if cells:
        # One match can now carry several prefixes, so the match count is
        # locators, not prefixes. Every character the pattern removes belongs
        # to a `css=` — the boundary alternatives and the lookahead are all
        # zero-width — so the length delta divides exactly into the real total.
        dropped = (len(robot_code) - len(stripped)) // len("css=")
        logger.info(
            f"Locator normalizer: dropped {dropped} redundant `css=` prefix(es) "
            f"from {cells} already-prefixed locator(s)"
        )
    return stripped


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
