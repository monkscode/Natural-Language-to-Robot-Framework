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

The same cleanup step also applies five smaller deterministic rules defined
here: `ensure_browser_timeout`, `strip_redundant_css_prefix`,
`rewrite_visibility_checks_to_wait`, `rewrite_select_text_reads` and
`rewrite_typed_value_reads`.

Referenced by:
    src.backend.services.dryrun_service.extract_and_normalize_robot_code
"""

import logging
import re
from dataclasses import dataclass

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


# Get Element States operator cells, with case, spaces and underscores removed as
# Robot Framework does when converting them, mapped to the Wait For Elements State
# state that means the same thing for a `visible` check. `contains hidden` is not
# here on purpose: a missing element reports only `detached`, yet
# `Wait For Elements State    hidden` passes for it.
_VISIBILITY_OPERATORS = {"contains": "visible", "*=": "visible", "notcontains": "hidden"}

# The `validate` operator takes a Python expression over the states list. Browser's own
# libdoc gives `Get Element States    h1    validate    value & visible` as the way to fail
# on an invisible element (data/libdocs/browser.json), so the assembler emits it — and it is
# the same check as `contains visible`. Only this exact expression converts: every other one
# either returns a value, names another state, or says something a single
# Wait For Elements State cannot. At most one ordinary space may sit either side of `&`
# (`value&visible` is the same Python expression); a space inside a word, a non-breaking space
# or a form feed is not the idiom and stays unchanged. Case is kept — `VISIBLE` is not a state
# Get Element States itself accepts.
_VALIDATE_VISIBLE_RE = re.compile(r"value ?& ?visible")

# Keywords that run another keyword and swallow or retry its failure. A check
# inside one of the file's own keywords can be reached through them, and there a
# 30s wait instead of a ~1s check changes the outcome (a probe that used to
# return False quickly now waits, or flips to True when the element is late).
# `Run Keyword And Continue On Failure` is left out on purpose: the failure still
# fails the test, so a longer wait there only removes false failures.
_ERROR_CATCHING_WRAPPERS = frozenset({
    "run keyword and return status",
    "run keyword and ignore error",
    "run keyword and expect error",
    "run keyword and warn on failure",
    "wait until keyword succeeds",
})

# Section names Robot Framework recognizes that can never define a keyword. Anything
# else — a Keywords/Keyword header (any case, singular/plural, with or without a
# trailing "*"), an empty name, or an unrecognized/localized header — is treated as
# "could define keywords": a `Language:` setting can make an unfamiliar spelling a
# valid, localized Keywords header, so an unrecognized name is not proof it isn't one.
_NON_KEYWORD_SECTIONS = frozenset({
    "Settings", "Setting", "Variables", "Variable",
    "Test Cases", "Test Case", "Tasks", "Task", "Comments", "Comment",
})


def _section_header_name(line: str) -> str | None:
    """Return the normalized name of a section-header line, or None when the line
    is not a header.

    Robot Framework recognizes a line as a section header when its first cell
    starts with `*` (`*** Settings ***`, `*Keywords`, `| *** Variables *** |`, ...),
    regardless of case, of how many `*` follow, and of whether pipe or space
    separation is used; a trailing cell after the header cell does not matter.
    This returns that first cell with its leading/trailing `*` and spaces
    stripped, internal whitespace collapsed to single spaces, and title-cased —
    what this comparison needs, not a claim that it is Robot's own normalization.
    """
    if line[:1] == "|" and line[:2].strip() == "|":
        cell = line.split("|")[1].strip()
    else:
        cell = _CELL_SPLIT_RE.split(line)[0]
    if not cell.startswith("*"):
        return None
    return " ".join(cell.split()).strip("* ").title()


def _names_keyword(line: str, first_part: str, keyword: str) -> bool:
    """True when a non-indented line's name is (or could resolve to) `keyword`.

    `keyword` is given the way Robot compares names: lowercase, no spaces, no
    underscores (`getelementstates`). Robot picks the separator format per line,
    so a pipe-separated definition (`| Get Element States | ... |`) can mix
    legally with space-separated steps elsewhere in the same file; there the name
    is the first pipe cell, not the whole line. Robot also matches an
    embedded-argument name (`Get ${what} States`) against the literal call, so
    `${...}` segments are treated as a wildcard — greedily, so a custom embedded
    regex containing `}` can only over-match, never under-match.
    """
    if line[:1] == "|" and line[:2].strip() == "|":
        cell = line.split("|")[1].strip()
        if not cell:
            return False
        name = cell
    else:
        name = first_part
    if not name:
        return False
    canon = re.sub(r"[\s_]", "", name.lower())
    pattern = ".*".join(re.escape(piece) for piece in re.split(r"\$\{.*\}", canon))
    return re.fullmatch(pattern, keyword) is not None


# Robot's tokenizer splits physical lines on more than "\n" — also a lone "\r"
# (one not paired into "\r\n"), and the rarer separator characters below. A
# section header that lands after one of those, glued onto whatever line we
# split on "\n", would be invisible to a header scan — meaning the section
# state (and every guard gated on it) could end up wrong in the direction that
# matters: rewriting a call Robot would actually resolve to the file's own
# keyword.
_OTHER_LINEBREAK_CHARS = "\r\v\f\x1c\x1d\x1e\x85" + chr(0x2028) + chr(0x2029)


@dataclass(frozen=True)
class _FileGuard:
    """What a rewrite must know about the whole file before it changes one line.

    own_keywords: which of the asked-for keyword names the file defines itself.
        Robot resolves such a call to the file's own keyword before any library.
    catches_errors: the file has a `TRY`, or its own keywords together with an
        error-catching wrapper — a changed check there can be caught and turn
        into a pass/fail flip or a stall the line cannot see.
    unsafe_section_state: the file holds a line break Robot honours but a "\n"
        split does not, so section state cannot be trusted anywhere in it.
    """

    own_keywords: frozenset[str]
    catches_errors: bool
    unsafe_section_state: bool


def _scan_file_guard(robot_code: str, keywords: tuple[str, ...]) -> _FileGuard:
    """Scan the whole file once for the facts every Robot-code rewrite guards on.

    `keywords` are names in `_names_keyword` form. When section state is unsafe
    (see `_OTHER_LINEBREAK_CHARS`), the own-keyword check falls back to every
    non-indented line regardless of section, and the TRY/wrapper check treats the
    file as if it had a Keywords section, since a hidden header could be one.
    """
    unsafe_section_state = any(
        c in robot_code.replace("\r\n", "\n") for c in _OTHER_LINEBREAK_CHARS
    )
    own = set()
    has_try = False
    has_keywords_section = False
    has_catching_wrapper = False
    in_non_keyword_section = False
    for line in robot_code.split("\n"):
        section_name = _section_header_name(line)
        if section_name is not None:
            in_non_keyword_section = section_name in _NON_KEYWORD_SECTIONS
            if not in_non_keyword_section:
                has_keywords_section = True
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith("..."):
            continue
        parts = _CELL_SPLIT_RE.split(line)
        cells = [i for i, p in enumerate(parts) if p and not _CELL_SPLIT_RE.fullmatch(p)]
        # A non-indented line names a test or a keyword. Robot resolves a keyword name
        # to the file's own keyword before any library, ignoring case, spaces and
        # underscores, and also matching an embedded-argument name or a pipe-separated
        # definition. Only a section that could define a keyword counts: a
        # *** Variables *** entry is `${name}    value`, a non-indented line whose first
        # cell is an embedded-argument wildcard that over-matches every keyword name;
        # a test, a setting and a comment cannot define keywords either. An
        # unrecognized header might be a localized Keywords header, so it counts as
        # "could". When section state is unsafe, the section is ignored entirely.
        if unsafe_section_state or not in_non_keyword_section:
            for keyword in keywords:
                if keyword not in own and _names_keyword(line, parts[0], keyword):
                    own.add(keyword)
        # parts[0] is empty only for an indented line; anything else is a
        # section header or a test/keyword name, never a step.
        if not cells or parts[0]:
            continue
        first = 0
        while first < len(cells) and _ASSIGN_PREFIX_RE.match(parts[cells[first]]):
            first += 1
        if first < len(cells):
            step_keyword = parts[cells[first]]
            if step_keyword.strip() == "TRY":
                has_try = True
            elif _canon_keyword(step_keyword) in _ERROR_CATCHING_WRAPPERS:
                has_catching_wrapper = True
    return _FileGuard(
        own_keywords=frozenset(own),
        catches_errors=has_try or (
            has_catching_wrapper and (has_keywords_section or unsafe_section_state)
        ),
        unsafe_section_state=unsafe_section_state,
    )


def rewrite_visibility_checks_to_wait(robot_code: str) -> str:
    """Rewrite `Get Element States    <loc>    contains    visible` and
    `Get Element States    <loc>    validate    value & visible` to
    `Wait For Elements State    <loc>    visible` (`not contains` -> `hidden`).

    `Get Element States` looks for the element for only 250ms and retries its
    assertion for `retry_assertions_for` (1s); the Browser import's `timeout`
    never applies to it, so a visibility check right after a page-changing
    action fails before the element appears. `Wait For Elements State` waits
    under the library timeout. It is a plain keyword with fixed arguments, so
    Robot resolves its arguments once (backslash escapes survive) and dryrun
    still checks them — `Wait For Condition` does neither.

    Only an indented step that is exactly that check is rewritten; anything
    whose meaning could change is left alone: an assigned result (WFES returns
    nothing), any state other than lowercase `visible`, any operator other than
    `contains`/`not contains` (except `validate` with exactly `value & visible`,
    which converts), any extra cell except a `message=` without `{}`
    placeholders (WFES formats the message with only selector/function/timeout)
    and a trailing comment, wrapped calls, settings, comments and continuation
    lines. Every other `validate` expression is left alone.

    The whole file is left unchanged when it catches errors around code the
    line cannot see: a `TRY` block, or its own keywords together with an
    error-catching wrapper. There a longer wait can turn a caught failure into
    a pass/fail flip or a 30s stall. It is also left unchanged when the file
    defines its own keyword named `Get Element States`, in a Keywords section,
    under a header this function cannot name as a non-keyword section, or before
    the first header — Robot resolves that name to the file's own keyword before
    the Browser library, so rewriting the call would swap in a different
    keyword. Idempotent.

    Args:
        robot_code: The Robot Framework source as a string.

    Returns:
        The same string with matching lines rewritten. Returns the input
        unchanged when it contains no "element states" text (fast path).
    """
    if not robot_code:
        return robot_code
    lower = robot_code.lower()
    if "element states" not in lower and "element_states" not in lower:
        return robot_code

    guard = _scan_file_guard(robot_code, ("getelementstates",))
    rewrote = 0
    out_lines = []
    for line in robot_code.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith("..."):
            out_lines.append(line)
            continue

        parts = _CELL_SPLIT_RE.split(line)
        cells = [i for i, p in enumerate(parts) if p and not _CELL_SPLIT_RE.fullmatch(p)]
        # parts[0] is empty only for an indented line; anything else is a
        # section header or a test/keyword name, never a step.
        if not cells or parts[0]:
            out_lines.append(line)
            continue

        first = 0
        while first < len(cells) and _ASSIGN_PREFIX_RE.match(parts[cells[first]]):
            first += 1
        if first or len(cells) < 4:
            out_lines.append(line)
            continue

        keyword_idx, _, operator_idx, state_idx = cells[:4]
        keyword_cell = parts[keyword_idx]
        if _canon_keyword(keyword_cell) != "get element states":
            out_lines.append(line)
            continue

        operator = re.sub(r"[\s_]+", "", parts[operator_idx].lower())
        state_cell = parts[state_idx]
        if operator == "validate":
            target_state = (
                "visible" if _VALIDATE_VISIBLE_RE.fullmatch(state_cell.strip()) else None
            )
        else:
            target_state = _VISIBILITY_OPERATORS.get(operator)
            if state_cell.strip() != "visible":
                target_state = None
        if target_state is None:
            out_lines.append(line)
            continue

        extras_ok = True
        for i in cells[4:]:
            cell = parts[i].strip()
            if cell.startswith("#"):
                break
            if not cell.startswith("message=") or "{" in cell or "}" in cell:
                extras_ok = False
                break
        if not extras_ok:
            out_lines.append(line)
            continue

        name = keyword_cell.strip()
        prefix = (
            name.rsplit(".", 1)[0].strip() + "."
            if "." in name and not name.startswith(".")
            else ""
        )
        parts[keyword_idx] = f"{prefix}Wait For Elements State"
        # The whole cell becomes the state: for `contains`/`not contains` the cell is already
        # `visible`, but a `validate` cell is the expression `value & visible`. Replacing the
        # stripped content keeps any whitespace the cell carries.
        parts[state_idx] = state_cell.replace(state_cell.strip(), target_state, 1)
        # Cells sit at even indices with their separators between them, so the
        # separator in front of the operator is the part just before it.
        del parts[operator_idx - 1 : operator_idx + 1]
        out_lines.append("".join(parts))
        rewrote += 1

    if not rewrote:
        return robot_code
    if guard.own_keywords:
        logger.info(
            f"Visibility check normalizer: left {rewrote} Get Element States line(s) "
            "unchanged — the file defines its own keyword of that name, which Robot "
            "resolves ahead of the Browser library"
        )
        return robot_code
    if guard.catches_errors:
        logger.info(
            f"Visibility check normalizer: left {rewrote} Get Element States line(s) "
            "unchanged — the file catches errors (TRY, or its own keywords under an "
            "error-catching wrapper), where a longer wait could change the outcome"
        )
        return robot_code
    logger.info(
        f"Visibility check normalizer: rewrote {rewrote} Get Element States "
        "line(s) to Wait For Elements State"
    )
    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# Reads that cannot see what the test just did to the same element:
# rewrite_select_text_reads and rewrite_typed_value_reads. Both follow each
# test top to bottom, so they share one reader of the suite.
# ---------------------------------------------------------------------------

_TEST_SECTIONS = frozenset({"Test Cases", "Test Case", "Tasks", "Task"})
_VARIABLE_SECTIONS = frozenset({"Variables", "Variable"})
_SETTING_SECTIONS = frozenset({"Settings", "Setting"})

# A test that uses one of these is left alone by both rewrites: a step inside a
# branch or a loop may run zero, one or many times, so it cannot be followed
# line by line. Robot only recognizes these markers in upper case. (`VAR` is
# refused for the whole file instead — see _SCOPED_SETTERS.)
_UNFOLLOWED_MARKERS = frozenset({
    "FOR", "WHILE", "IF", "ELSE IF", "ELSE", "END", "TRY", "EXCEPT", "FINALLY",
    "BREAK", "CONTINUE", "RETURN", "GROUP",
})

# A file where a content cell anywhere names one of these keywords (compared
# lower-cased without spaces or underscores, anywhere in the cell), or is
# exactly `VAR` (Robot only recognizes it in upper case), is left alone as a
# whole by both rewrites. The scoped setters and `Import Variables` / `Import
# Resource` set variables beyond the line they sit on, and `Set Selector
# Prefix` changes the element every later selector names, in later tests too —
# as a step, as a wrapper's argument, in the file's own keyword, in another
# test, in a setup, or as a Python call inside `Evaluate` — so no test-by-test
# reading can tell which element a locator names when a read runs.
_SCOPED_SETTERS = ("settestvariable", "settaskvariable", "setsuitevariable",
                   "setglobalvariable", "setlocalvariable",
                   "importvariables", "importresource", "setselectorprefix")

_BROWSER_PREFIXES = frozenset({"", "browser"})
_BUILTIN_PREFIXES = frozenset({"", "builtin"})

# One cell that is exactly one scalar variable, e.g. `${dropdown_locator}`.
_SCALAR_CELL_RE = re.compile(r"\$\{([^{}]+)\}")
# Anything that is still a variable after resolution: the value is unknown.
_ANY_VARIABLE_RE = re.compile(r"[$@&%]\{")
# Every `${name}` / `@{name}` / `&{name}` / `%{name}` in a line, and every `$name`
# inside an evaluated expression (`Should Be True    $x == 'a'`).
_VARIABLE_REF_RE = re.compile(r"[$@&%]\{([^{}]*)\}")
_EXPRESSION_VAR_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
# A variable reference inside another one (`${a${b}}`, or `$a${b}` in an expression): a name Robot computes at
# run time.
_NESTED_VARIABLE_RE = re.compile(r"[$@&%]\{[^{}]*[$@&%]\{|\$\w*[$@&%]\{")
# A *** Variables *** entry's name cell with a plain name: `${x}`, `@{x}`, `&{x}`, `${x: str}`, `${x}=`.
_VARIABLE_ENTRY_RE = re.compile(r"([$@&])\{([^{}]+)\}\s*=?")
# A leading assignment cell, capturing the variable name.
_ASSIGNED_NAME_RE = re.compile(r"[$@&]\{([^}]+)\}")
# `id=x` and `css=#x` name the same element when x is a plain CSS identifier (no
# `.`: `css=#a.b` is id `a` with class `b`, `id=a.b` is id `a.b`). A bare `#x` is
# not listed: Robot reads a cell starting with `#` as a comment, and
# normalize_robot_code has already turned every locator `#x` into `css=#x`.
_ID_LOCATOR_RE = re.compile(r"(?:id=|css=#)([A-Za-z_][\w-]*)")


def _canon_variable(name: str) -> str:
    """A variable name the way Robot compares it: case, spaces and underscores ignored."""
    return re.sub(r"[\s_]", "", name.lower())


def _keyword_prefix(cell: str) -> str:
    """The library prefix of a keyword cell (`Browser.Get Text` -> `browser`), or ''."""
    name = cell.strip()
    if "." in name and not name.startswith("."):
        return re.sub(r"[\s_]", "", name.rsplit(".", 1)[0].lower())
    return ""


def _canon_locator(value: str | None) -> str | None:
    """A resolved locator in a form where `id=x` and `css=#x` compare equal."""
    if value is None:
        return None
    text = value.strip()
    match = _ID_LOCATOR_RE.fullmatch(text)
    return f"id={match.group(1)}" if match else text


def _mentions_variable(text: str, canon: str) -> bool:
    """True when `text` refers to the variable `canon` in any form Robot reads:
    `${x}`, `@{x}`, `&{x}`, `${x}[0]`, `${x.attr}`, `${ X }` or `$x` in an
    expression — and whenever it holds a name built from another variable
    (`${x${EMPTY}}`), which may resolve to `x`. Over-matching only makes a
    rewrite skip, never fire."""
    if _NESTED_VARIABLE_RE.search(text):
        return True
    for ref in _VARIABLE_REF_RE.findall(text):
        if canon in (_canon_variable(ref), _canon_variable(re.split(r"[.\[]", ref, maxsplit=1)[0])):
            return True
    return any(_canon_variable(name) == canon for name in _EXPRESSION_VAR_RE.findall(text))


def _resolve(cell: str, state: dict[str, str | None]) -> str | None:
    """The literal a cell stands for: the cell itself, or the known value of a
    whole-cell `${var}`; None when it cannot be known without running the test."""
    match = _SCALAR_CELL_RE.fullmatch(cell)
    if match:
        return state.get(_canon_variable(match.group(1)))
    return None if _ANY_VARIABLE_RE.search(cell) else cell


@dataclass
class _Step:
    """One line of a test body that a rewrite may read or change."""

    line_no: int
    parts: list[str]
    eol: str
    assigns: list[str]
    scalar_assign: bool
    keyword_idx: int | None
    keyword: str
    prefix: str
    arg_idx: list[int]
    args: list[str]
    setting: bool
    continued: bool = False


@dataclass
class _Test:
    steps: list[_Step]
    line_nos: list[int]
    followable: bool = True


@dataclass
class _Suite:
    lines: list[str]
    variables: dict[str, str | None]
    settings_line_nos: list[int]
    tests: list[_Test]


def _content_cells(parts: list[str]) -> list[int]:
    """Indices of the content cells of a split line, up to a comment cell."""
    cells = []
    for i, part in enumerate(parts):
        if not part or _CELL_SPLIT_RE.fullmatch(part):
            continue
        if part.lstrip().startswith("#"):
            break
        cells.append(i)
    return cells


def _line_content(line: str) -> str:
    """A line's content cells joined, without separators or a trailing comment."""
    parts = _CELL_SPLIT_RE.split(line)
    return " ".join(parts[i] for i in _content_cells(parts))


def _parse_step(line_no: int, line: str, eol: str) -> _Step | None:
    """One indented test-body line as a _Step, or None when this reader cannot
    read it as one: no separator before its first cell (Robot reads a line
    indented by one space as a NEW test), no keyword after its assignments
    (`${x}=` alone, its keyword on the next `...` line), a typed assignment
    (`${x: str}=`: Robot assigns `${x}`), or a nested or item assignment
    target (`${a${b}}=`, `${x}[0]=`: Robot assigns, it never calls a keyword)."""
    parts = _CELL_SPLIT_RE.split(line)
    cells = _content_cells(parts)
    # parts[0] is empty only for a properly indented line.
    if parts[0] or not cells:
        return None
    first = 0
    assigns = []
    while first < len(cells) and _ASSIGN_PREFIX_RE.match(parts[cells[first]]):
        name = _ASSIGNED_NAME_RE.match(parts[cells[first]]).group(1)
        if ":" in name:
            return None  # a typed assignment: Robot strips the type, so `${x: str}=` re-points `${x}`
        assigns.append(_canon_variable(name))
        first += 1
    if first == len(cells):
        return None
    head = parts[cells[first]].strip()
    if re.match(r"[$@&%]\{", head):
        return None  # a nested or item assignment target: Robot assigns it, so its name is not a keyword
    setting = head.startswith("[") and head.endswith("]")
    return _Step(
        line_no=line_no, parts=parts, eol=eol, assigns=assigns,
        scalar_assign=first == 1 and parts[cells[0]].lstrip().startswith("${"),
        keyword_idx=None if setting else cells[first],
        keyword=head.lower() if setting else _canon_keyword(head),
        prefix="" if setting else _keyword_prefix(head),
        arg_idx=cells[first + 1:],
        args=[parts[i].strip() for i in cells[first + 1:]],
        setting=setting,
    )


def _read_variable(line: str, variables: dict[str, str | None], last: str | None) -> str | None:
    """Record one *** Variables *** line; return the base name it defined (for `...`).

    A list (`@{x}`), a dict (`&{x}`) or a typed value (`${x: str}`) makes the
    base name `x` unknown, and so does any later definition of it: Robot keeps
    the first of `${x}` / `@{x}`, and `${x}` then names that list."""
    parts = _CELL_SPLIT_RE.split(line)
    cells = [parts[i].strip() for i in _content_cells(parts)]
    if not cells:
        return last
    if cells[0].startswith("..."):
        if last is not None:
            variables[last] = None  # a value continued on a `...` line is not one plain literal
        return last
    match = _VARIABLE_ENTRY_RE.fullmatch(cells[0])
    if not match:
        return None
    name = _canon_variable(match.group(2).split(":", 1)[0])
    values = cells[1:]
    if name in variables or match.group(1) != "$" or ":" in match.group(2):
        variables[name] = None  # defined twice, or not one plain scalar: which value `${x}` has is unknown
    elif not values:
        variables[name] = ""
    elif len(values) == 1 and not _ANY_VARIABLE_RE.search(values[0]) and "\\" not in values[0]:
        variables[name] = values[0]
    else:
        variables[name] = None
    return name


def _parse_suite(robot_code: str) -> _Suite | None:
    """Read the suite as a top-to-bottom list of tests, or None when this reader
    cannot be trusted with the file: a pipe-separated line, a template, a line
    break Robot honours that a "\\n" split does not (`_OTHER_LINEBREAK_CHARS`),
    a *** Variables *** entry whose name holds another variable (`${a${b}}`:
    Robot defines a name this reader cannot compute), or — on any line of any
    section — a scoped setter, `Import Variables`, `Import Resource`, `Set
    Selector Prefix` or `VAR` (`_SCOPED_SETTERS`). A test is read but marked
    not followable when it holds
    a control structure or `[Template]`, when its name line carries more than
    the name or a `...` line continues its name line (Robot runs that as the
    test's first step), when an unindented `...` line follows (Robot continues
    the previous step with it: it starts no test), or when any other body line
    is not a step this reader can read (`_parse_step`: an assignment-only line,
    a typed assignment, a nested or item assignment target, a line Robot reads
    as a new test)."""
    if any(c in robot_code.replace("\r\n", "\n") for c in _OTHER_LINEBREAK_CHARS):
        return None
    lines = robot_code.split("\n")
    variables: dict[str, str | None] = {}
    settings_line_nos: list[int] = []
    tests: list[_Test] = []
    section = None
    current = None
    last_variable = None
    for line_no, raw in enumerate(lines):
        line, eol = (raw[:-1], "\r") if raw.endswith("\r") else (raw, "")
        if line[:1] == "|" and line[:2].strip() == "|":
            return None
        header = _section_header_name(line)
        if header is not None:
            section, current, last_variable = header, None, None
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = _CELL_SPLIT_RE.split(line)
        content = [parts[i].strip() for i in _content_cells(parts)]
        if any(setter in re.sub(r"[\s_]", "", cell.lower()) for cell in content for setter in _SCOPED_SETTERS):
            return None
        if "VAR" in content:
            return None
        if section in _SETTING_SECTIONS:
            settings_line_nos.append(line_no)
            if _canon_keyword(_CELL_SPLIT_RE.split(stripped)[0]) in ("test template", "task template"):
                return None
        elif section in _VARIABLE_SECTIONS:
            if re.match(r"[$@&]\{", content[0]) and not _VARIABLE_ENTRY_RE.fullmatch(content[0]):
                return None  # a name built from another variable: which name Robot defines is unknown here
            last_variable = _read_variable(line, variables, last_variable)
        elif section in _TEST_SECTIONS:
            if not line[:1].isspace():
                if content[0] == "...":
                    if current is not None:
                        current.followable = False  # Robot continues the previous step with it, even unindented
                    continue
                current = _Test(steps=[], line_nos=[])
                tests.append(current)
                if len(content) > 1:
                    current.followable = False  # Robot runs the rest of a test-name line as its first step
                continue
            if current is None:
                continue
            current.line_nos.append(line_no)
            if stripped.startswith("..."):
                if current.steps:
                    current.steps[-1].continued = True
                else:
                    current.followable = False  # it continues the test-name line: Robot runs it as the first step
                continue
            step = _parse_step(line_no, line, eol)
            if step is None:
                current.followable = False  # a line this reader cannot read as a step (see _parse_step)
                continue
            if (step.keyword == "[template]"
                    or (step.keyword_idx is not None and step.parts[step.keyword_idx].strip() in _UNFOLLOWED_MARKERS)):
                current.followable = False
            current.steps.append(step)
    return _Suite(lines=lines, variables=variables, settings_line_nos=settings_line_nos, tests=tests)


def _follow(test: _Test, variables: dict[str, str | None]) -> list[list[str | None]]:
    """Each step's arguments resolved with the variables as they stand when the step runs."""
    state = dict(variables)
    resolved_steps = []
    for step in test.steps:
        resolved = [_resolve(arg, state) for arg in step.args]
        resolved_steps.append(resolved)
        for name in step.assigns:
            state[name] = None
        if (len(step.assigns) == 1 and step.keyword == "set variable" and step.prefix in _BUILTIN_PREFIXES
                and len(step.args) == 1 and not step.continued):
            state[step.assigns[0]] = resolved[0]
    return resolved_steps


def _rebuild(step: _Step, keyword: str, *, insert_after_locator: str | None = None,
             replace_second_arg: str | None = None) -> str:
    """The step's line with its keyword renamed (library prefix kept) and one cell
    inserted after the locator or the second argument replaced; separators kept."""
    parts = list(step.parts)
    name = parts[step.keyword_idx].strip()
    prefix = name.rsplit(".", 1)[0].strip() + "." if "." in name and not name.startswith(".") else ""
    parts[step.keyword_idx] = parts[step.keyword_idx].replace(name, prefix + keyword, 1)
    if replace_second_arg is not None:
        cell = parts[step.arg_idx[1]]
        parts[step.arg_idx[1]] = cell.replace(cell.strip(), replace_second_arg, 1)
    if insert_after_locator is not None:
        locator_idx = step.arg_idx[0]
        parts[locator_idx + 1:locator_idx + 1] = [parts[locator_idx - 1], insert_after_locator]
    return "".join(parts) + step.eol


def _apply_rewrites(robot_code: str, suite: _Suite, rewrites: dict[int, str],
                    guard_keywords: tuple[str, ...], label: str, noun: str, target: str) -> str:
    """Apply line rewrites unless a whole-file guard says the file must stay as it is."""
    if not rewrites:
        return robot_code
    guard = _scan_file_guard(
        robot_code, tuple(re.sub(r"[\s_]", "", k.lower()) for k in guard_keywords)
    )
    count = len(rewrites)
    if guard.own_keywords:
        own = ", ".join(k for k in guard_keywords if re.sub(r"[\s_]", "", k.lower()) in guard.own_keywords)
        logger.info(
            f"{label}: left {count} {noun} unchanged — the file defines its own "
            f"keyword ({own}), which Robot resolves ahead of the Browser library"
        )
        return robot_code
    if guard.catches_errors:
        logger.info(
            f"{label}: left {count} {noun} unchanged — the file catches errors (TRY, "
            "or its own keywords under an error-catching wrapper), where a check that "
            "can now fail could change the outcome"
        )
        return robot_code
    lines = list(suite.lines)
    for line_no, text in rewrites.items():
        lines[line_no] = text
    logger.info(f"{label}: rewrote {count} {noun} to {target}")
    return "\n".join(lines)


_SELECT_ATTRIBUTES = frozenset({"label", "value", "text"})
_CONTAINS_OPERATORS = frozenset({"contains", "*="})
# Get Text / Get Selected Options / Select Options By are the rewrite's own evidence; Set Variable, Should Contain
# and Log are the keywords whose BuiltIn meaning the reader relies on (_follow, the use check).
_SELECT_READ_GUARD = ("Get Text", "Get Selected Options", "Select Options By", "Set Variable", "Should Contain", "Log")
# The keywords that change a <select>'s selection, compared lower-cased without spaces or underscores.
_SELECTION_CHANGES = ("selectoptionsby", "deselectoptions")


def _only_checked_as_selected(suite: _Suite, test: _Test, read_at: int, name: str,
                              values: frozenset[str], resolved: list[list[str | None]]) -> bool:
    """True when every later mention of `${name}` is `Should Contain ${name} V`
    with V a selected value, or a `Log` of the bare `${name}`, and at least one
    is the Should Contain.

    Mentions are looked for on every line of the test after the read (a `...`
    line too), on the test's own [Setting] lines wherever they sit ([Teardown]
    runs last), and in the Settings section (a Test Teardown sees the test's
    variables). A later line that only re-assigns `${name}` ends the scan.
    """
    by_line = {step.line_no: (step, resolved[i]) for i, step in enumerate(test.steps)}
    read_line = test.steps[read_at].line_no
    checked = False

    def allowed(line_no: int) -> str:
        """'skip' (no mention), 'check', 'log', 'reassign' or 'no'."""
        content = _line_content(suite.lines[line_no])
        if not _mentions_variable(content, name):
            return "skip"
        step, args = by_line.get(line_no, (None, None))
        if step is None or step.setting or step.continued:
            return "no"
        if step.assigns == [name] and not any(_mentions_variable(a, name) for a in step.args):
            return "reassign"
        if step.assigns:
            return "no"
        if step.keyword == "log" and step.prefix in _BUILTIN_PREFIXES:
            # Only the bare `${name}`, outside any other `{…}`: `${x.upper()}`, `${x}[0]`, `$x`, `@{x}` or
            # `${{ … }}` would work on the list the rewrite returns, not on the text Get Text returned.
            rest = re.sub(r"\$\{([^{}]*)\}(?!\[)", lambda m: "" if (
                _canon_variable(m.group(1)) == name
                and m.string.count("{", 0, m.start()) == m.string.count("}", 0, m.start())) else m.group(0), content)
            return "no" if _mentions_variable(rest, name) else "log"
        target = _SCALAR_CELL_RE.fullmatch(step.args[0]) if step.args else None
        if (step.keyword == "should contain" and step.prefix in _BUILTIN_PREFIXES and len(step.args) == 2
                and target and _canon_variable(target.group(1)) == name and args[1] in values):
            return "check"
        return "no"

    for line_no in (no for no in test.line_nos if no > read_line):
        verdict = allowed(line_no)
        if verdict == "reassign":
            break  # a fresh value: later mentions are about it, not about this read
        if verdict == "no":
            return False
        checked = checked or verdict == "check"
    elsewhere = [s.line_no for s in test.steps if s.setting and s.line_no < read_line] + suite.settings_line_nos
    if any(allowed(line_no) != "skip" for line_no in elsewhere):
        return False
    return checked


def _update_selected(step: _Step, args: list[str | None],
                     selected: dict[str, tuple[str, frozenset[str]]]) -> bool:
    """Apply a step that may change a selection to `selected` (canonical locator
    -> the select's attribute and values); True when the step is one.

    A select or deselect this reader cannot follow forgets every selection: one
    named as another step's argument (`Run Keyword    Select Options By …`), one
    spelled without spaces (`SelectOptionsBy`), another library's, or one on an
    element it cannot name. A select or deselect on element L also forgets every
    other selection unless both are plain `id=` locators with different ids: an
    xpath or css spelling may name the same element.
    """
    if any(name in re.sub(r"[\s_]", "", arg.lower()) for arg in step.args for name in _SELECTION_CHANGES):
        selected.clear()
        return True
    if step.keyword.replace(" ", "") not in _SELECTION_CHANGES:
        return False
    locator = _canon_locator(args[0]) if args else None
    if (step.keyword not in ("select options by", "deselect options") or step.prefix not in _BROWSER_PREFIXES
            or locator is None):
        selected.clear()  # a select the reader cannot follow may have changed any of them
        return True
    for other in [key for key in selected if key != locator]:
        if not (_ID_LOCATOR_RE.fullmatch(other) and _ID_LOCATOR_RE.fullmatch(locator)):
            del selected[other]
    if step.keyword == "deselect options":
        selected.pop(locator, None)
        return True
    attribute = step.args[1] if len(step.args) > 1 else ""
    values = args[2:]
    if step.continued or attribute.lower() not in _SELECT_ATTRIBUTES or not values or None in values:
        selected.pop(locator, None)
    else:
        selected[locator] = (attribute, frozenset(values))
    return True


def _select_read_rewrites(suite: _Suite) -> dict[int, str]:
    """line number -> rewritten line, for every Get Text read the select rule accepts."""
    rewrites = {}
    for test in suite.tests:
        if not test.followable:
            continue
        resolved = _follow(test, suite.variables)
        selected: dict[str, tuple[str, frozenset[str]]] = {}
        for i, step in enumerate(test.steps):
            args = resolved[i]
            if step.setting or _update_selected(step, args, selected):
                continue
            if step.keyword != "get text" or step.prefix not in _BROWSER_PREFIXES or step.continued or not args:
                continue
            locator = _canon_locator(args[0])
            if locator not in selected:
                continue
            attribute, values = selected[locator]
            if step.assigns:
                if (step.scalar_assign and len(step.args) == 1
                        and _only_checked_as_selected(suite, test, i, step.assigns[0], values, resolved)):
                    rewrites[step.line_no] = _rebuild(step, "Get Selected Options", insert_after_locator=attribute)
            elif (len(step.args) == 3 and re.sub(r"[\s_]+", "", step.args[1].lower()) in _CONTAINS_OPERATORS
                  and args[2] in values):
                rewrites[step.line_no] = _rebuild(step, "Get Selected Options", insert_after_locator=attribute)
    return rewrites


def rewrite_select_text_reads(robot_code: str) -> str:
    """Rewrite a `Get Text` read of a native `<select>` the test just set, into
    `Get Selected Options` on the same attribute — q08's check that cannot fail.

    `Get Text` on a `<select>` returns every option's label ("Please select an
    option\\nOption 1\\nOption 2"), so `Should Contain    ${x}    Option 2` after
    selecting Option 2 passes whatever is selected. `Get Selected Options    <L>
    <attr>` returns the selected options as a list, and Robot's `Should Contain`
    checks list membership, so the same assertion now fails when the selection
    did not hold.

    Rewritten, inside one test: `${x}=    Get Text    <L>` when an earlier
    `Select Options By    <L>    label|value|text    <V>...` targets the same
    element — the same text once `${var}`s are resolved (from *** Variables ***
    or an earlier `Set Variable`), with `id=x` and `css=#x` equal — and
    every later mention of `${x}` is `Should Contain    ${x}    <V>` (V one of
    the selected values; at least one such line) or a `Log` of the bare
    `${x}`. Also the inline form `Get Text    <L>    contains|*=    <V>`.
    `<attr>` is the attribute the select used; the `Browser.` prefix is kept.
    Nothing else: `index` selects, custom dropdowns (no `Select Options By`), a
    read of the selected `<option>` itself, `Should Be Equal` or any other use
    of `${x}`, a V that is not selected (a "still listed" check), a different
    element, a `...` continuation on either line, a read before the select, a
    select or deselect in between that this reader cannot follow (behind a
    wrapper, spelled without spaces, on an element it cannot name, or on another
    element not named by a plain `id=`), a locator that *** Variables ***
    defines as a list, a dict or a typed value, a later line holding a name
    built from another variable (`${selected_${EMPTY}option}`), a test with a
    control structure, a template, a step on its name line or a line this
    reader cannot read as a step (a nested or item assignment target among
    them), and whole files that define their own `Get Text` / `Get Selected
    Options` / `Select Options By` / `Set Variable` / `Should Contain` / `Log`,
    catch errors (TRY, or own keywords under an error-catching wrapper), use
    pipe-separated lines, name a *** Variables *** entry through another
    variable, or hold a scoped `Set … Variable`, `Import Variables`, `Import
    Resource`, `Set Selector Prefix` or `VAR` anywhere. Idempotent.

    Covers the generation path and every dryrun repair round (both run
    `extract_and_normalize_robot_code`); pasted code and re-runs of stored code
    never pass through here.
    """
    if not robot_code:
        return robot_code
    squashed = re.sub(r"[\s_]", "", robot_code.lower())
    if "selectoptionsby" not in squashed or "gettext" not in squashed:
        return robot_code
    suite = _parse_suite(robot_code)
    if suite is None:
        return robot_code
    return _apply_rewrites(
        robot_code, suite, _select_read_rewrites(suite), _SELECT_READ_GUARD,
        "Select read normalizer", "Get Text line(s) on a select", "Get Selected Options",
    )


# Keywords that write a field's value AND fail on an element that is not a field
# (measured, Browser 19.14.2: each raises "Element is not an <input>, <textarea>,
# <select> or [contenteditable]" on a <div value>, <li value> or custom element).
# Type Text / Type Secret only fail there because they clear the field first; with
# `clear` switched off they type into anything, as `Press Keys` does — so neither
# counts as proof that the element is a field.
_TYPING_KEYWORDS = frozenset({"fill text", "fill secret", "clear text", "type text", "type secret"})
# Set Variable: the shared reader trusts BuiltIn's meaning of it (_follow).
_TYPED_READ_GUARD = ("Get Attribute", "Get Property", "Fill Text", "Fill Secret", "Clear Text",
                     "Type Text", "Type Secret", "Set Variable")


def _proves_a_field(step: _Step) -> bool:
    """True when the typing step could only have succeeded on a field."""
    if step.keyword in ("type text", "type secret"):
        if any(a.startswith(("@{", "&{")) for a in step.args):
            return False  # an expanded list or dict may carry `clear`
        rest = step.args[1:]  # `clear=` may be named before the text
        # Robot resolves an argument name at run time (`${n}=`, `cl\ear=`): deny every name but these.
        return len(rest) <= 2 and not any(
            "=" in a and a.split("=", 1)[0] not in ("txt", "secret", "delay") for a in rest
        )
    return True


def _typed_value_rewrites(suite: _Suite) -> dict[int, str]:
    """line number -> rewritten line, for every Get Attribute read the typed-value rule accepts."""
    rewrites = {}
    for test in suite.tests:
        if not test.followable:
            continue
        resolved = _follow(test, suite.variables)
        typed: set[str] = set()
        for i, step in enumerate(test.steps):
            args = resolved[i]
            if step.setting or step.prefix not in _BROWSER_PREFIXES or step.continued or not args:
                continue
            if step.keyword in _TYPING_KEYWORDS:
                locator = _canon_locator(args[0])
                if locator is not None and _proves_a_field(step):
                    typed.add(locator)
                continue
            if (step.keyword == "get attribute" and len(step.args) >= 2
                    and step.args[1] in ("value", "attribute=value")
                    and _canon_locator(args[0]) in typed):
                rewrites[step.line_no] = _rebuild(
                    step, "Get Property",
                    replace_second_arg="property=value" if step.args[1] == "attribute=value" else None,
                )
    return rewrites


def rewrite_typed_value_reads(robot_code: str) -> str:
    """Rewrite `Get Attribute    <L>    value` to `Get Property    <L>    value`
    when the same test typed into <L> earlier — a read that cannot see the typing.

    The `value` ATTRIBUTE is the field's initial value; what the test typed
    lives in the `value` PROPERTY. So after `Fill Text    <L>    NewYork`,
    `Get Attribute    <L>    value` returns '' (or the prefilled default) and a
    correct test fails. Only after a typing step on the same element (same
    resolution rules as `rewrite_select_text_reads`) that proves the element is
    a field: Fill Text, Fill Secret, Clear Text, and Type Text / Type Secret
    unless an argument could switch `clear` off (a second argument after the
    text, `clear` by position; any cell after the locator holding `=` whose
    name before the first `=` is not `txt`, `secret` or `delay`; an expanded
    `@{…}` / `&{…}`). The named form `attribute=value` becomes
    `property=value` (swapping only the keyword would ask for a property
    literally named "attribute=value", which dryrun cannot catch); inline
    assertion arguments keep their positions. Nothing else: an element nothing
    typed into (`<div value>`, `<li value>`, custom elements, `<option>`, a
    prefilled field the test never typed into — there the property is None or
    an int), a read in another test, a `...` continuation, a locator that
    *** Variables *** defines as a list, a dict or a typed value, a test with a
    control structure, a template, a step on its name line or a line the reader
    cannot read as a step (a nested or item assignment target among them), and
    whole files that define their own `Get Attribute` / `Get Property` / typing
    keyword / `Set Variable`, catch errors, use pipe-separated lines, name a
    *** Variables *** entry through another variable, or hold a scoped `Set …
    Variable`, `Import Variables`, `Import Resource`, `Set Selector Prefix` or
    `VAR` anywhere. Idempotent.
    """
    if not robot_code:
        return robot_code
    if "getattribute" not in re.sub(r"[\s_]", "", robot_code.lower()):
        return robot_code
    suite = _parse_suite(robot_code)
    if suite is None:
        return robot_code
    return _apply_rewrites(
        robot_code, suite, _typed_value_rewrites(suite), _TYPED_READ_GUARD,
        "Typed value normalizer", "Get Attribute … value line(s)", "Get Property",
    )


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
