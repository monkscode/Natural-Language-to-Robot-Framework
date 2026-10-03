"""Static pass-quality check: does a generated Robot test do what its query asked?

A green run can be hollow: it passes whatever the page shows, or it never checks
what the user asked for. Over the 114 bench gate passes since E2 (2026-09-16),
8 were hollow: q08 cannot-fail select check 5, q05 answer-in-the-locator 2, q05
wrong column 1. The last one is not statically detectable (the right and the
wrong cell are both a positional `td`). 7 more were q10 count-only tests, which
are not hollow (owner, 2026-10-01: the count the query asks to verify is
asserted). This module flags the detectable shapes from two strings, the Robot
source and the user's query. It reads no database, no network and no file.

Shapes. Every shape in HOLLOW_SHAPES makes a pass hollow; FRAGILE_NUMERIC_ID is
reported and never hollow.
- READ_NOT_PERFORMED: the query asks to get / read / extract / retrieve / fetch
  a value, and the test runs no reader keyword. Counting keywords (Get Elements,
  Get Length, Get Element Count) are deliberately NOT readers. A count request
  whose count is checked is not reported: the query's verify clause (what
  follows the verify word, up to a comma, a semicolon, a sentence end, a line
  break, "and" or "then") names an integer N (q10: "get the titles ... verify
  there are 20 books") and the test asserts a count against N — a counting
  keyword's own inline assertion (`Get Element Count  L  ==  20`), or an
  assertion that uses a variable a counting keyword filled (`Should Be True
  ${n} == 20`), with N as a whole number ("1920", "20.5" and "20s" hold no 20).
  A number word ("twenty"), another number, or no assertion on the count: still
  reported.
- SELECT_CHECK_CANNOT_FAIL: `Select Options By  L  <attr>  V`, then `Get Text`
  on the same <select> checked with `Should Contain  <that text>  V` (or inline
  `contains V`). A whole <select>'s text lists every option, so the check passes
  whatever is selected.
- READ_LOCATOR_IS_THE_ANSWER: a read query whose reader finds its element by a
  human-text literal (text=, has-text, aria-label, title, alt, role name) that
  the query does not contain, so the read returns the literal it searched for
  (q05: `text="John" >> nth=0`). A bare `name=` is NOT such a literal. Only the
  value read counts. Only the LAST element part of a `>>` / `>>>` chain is
  searched (earlier parts are ancestors or frames; a trailing `nth=N` refines
  the part before it): `css=table:has-text("Last Name") >> ... >> td >> nth=1`
  reads a `td`, not "Last Name". Get Text / Get Texts count any literal kind;
  Get Attribute / Get Property count only a title, aria-label or alt literal,
  and only when they read that same attribute (`Get Attribute  [title="A Light
  in the Attic"]  title` is flagged; `Get Attribute  text="X"  href` is not).
- VERIFY_WITHOUT_ASSERTION: the query says verify / check / confirm / ensure /
  validate / assert / make sure, and the test asserts nothing.
- EMPTY_TEST: the test only starts a browser, opens or navigates pages, logs
  and closes, while the query asks for more than opening a page (or there is
  no query at all).
- FRAGILE_NUMERIC_ID (reported only): a read query's reader locates its element
  by a numeric id such as `id=880667900` (a repository id). It is right today
  and pinned to one record.

Pasted code (empty query): only the query-free rules run, SELECT_CHECK_CANNOT_FAIL
and EMPTY_TEST.

Referenced by: bench/gate_hollow.py (the bench gate's hollow registry). A later
History / SSE pass-quality note will read it too (not built yet).
Depends on: re, dataclasses. Standard library only: importing this module must
never load config, a database driver or an LLM client.
"""

import re
from dataclasses import dataclass

READ_NOT_PERFORMED = "READ_NOT_PERFORMED"
SELECT_CHECK_CANNOT_FAIL = "SELECT_CHECK_CANNOT_FAIL"
READ_LOCATOR_IS_THE_ANSWER = "READ_LOCATOR_IS_THE_ANSWER"
VERIFY_WITHOUT_ASSERTION = "VERIFY_WITHOUT_ASSERTION"
EMPTY_TEST = "EMPTY_TEST"
FRAGILE_NUMERIC_ID = "FRAGILE_NUMERIC_ID"

HOLLOW_SHAPES = frozenset({READ_NOT_PERFORMED, SELECT_CHECK_CANNOT_FAIL,
                           READ_LOCATOR_IS_THE_ANSWER, VERIFY_WITHOUT_ASSERTION,
                           EMPTY_TEST})
REPORTED_SHAPES = frozenset({FRAGILE_NUMERIC_ID})
SHAPE_ORDER = (READ_NOT_PERFORMED, SELECT_CHECK_CANNOT_FAIL, READ_LOCATOR_IS_THE_ANSWER,
               VERIFY_WITHOUT_ASSERTION, EMPTY_TEST, FRAGILE_NUMERIC_ID)


@dataclass(frozen=True)
class Finding:
    """One shape found in one test. `detail` names the literal or locator."""

    shape: str
    detail: str = ""

    @property
    def hollow(self) -> bool:
        return self.shape in HOLLOW_SHAPES


# The same read verbs as bench/bench_lib.py:query_requests_read (whole words:
# "target" contains "get" and must not count). Duplicated, not imported: this
# module must not depend on bench/.
_READ_Q = re.compile(r"\b(get|read|extract|retrieve|fetch)\b", re.IGNORECASE)
_VERIFY_Q = re.compile(
    r"\b(verify|verifies|check|checks|confirm|ensure|validate|assert|make sure)\b",
    re.IGNORECASE)
# Anything a query can ask beyond opening a page. Only guards EMPTY_TEST: a query
# that says nothing but "open X" is satisfied by a test that only opens X.
_ACTION_Q = re.compile(
    r"\b(click|clicks|type|types|enter|enters|fill|fills|select|selects|search|searches|"
    r"press|presses|submit|submits|log ?in|login|sign ?in|hover|scroll|upload|drag|choose|"
    r"add|remove|delete|sort|filter|count)\b", re.IGNORECASE)

# Reader keywords, matched as a prefix of the normalised keyword name ("get text"
# also covers "get texts", "get attribute" covers "get attribute names").
_READERS = ("get text", "get attribute", "get property", "get selected options",
            "get title", "get url", "get value", "get element states",
            "get checkbox state", "get style", "get classes")
# Counting keywords: they return how many (or the things counted), never a value.
_COUNTERS = ("get element count", "get elements", "get length")
_ASSERTION_PREFIXES = ("wait for elements state", "wait until", "wait for condition",
                       "fail", "run keyword if", "run keyword unless")
# "Should Be Equal", "Length Should Be", "Element Should Be Visible", "Page Should
# Contain", "List Should Contain Value": any keyword with the word "should".
_SHOULD = re.compile(r"(?:^|\s)should(?:\s|$)")
# Browser's inline assertion operators on a `Get ...` keyword: the member names of
# its AssertionOperator enum (committed libdoc data/libdocs/browser.json, typedocs)
# except `then` and `evaluate`, which return the evaluated expression and never
# fail. A test pins this set to the libdoc. Checked on the first four arguments:
# `Get Title  ==  X` puts the operator FIRST (no locator).
_OPERATORS = {"==", "equal", "equals", "should be", "!=", "inequal", "should not be",
              "<", "less than", ">", "greater than", "<=", ">=", "*=", "contains",
              "not contains", "^=", "starts", "should start with", "$=", "ends",
              "should end with", "matches", "validate"}
# Keywords that neither interact, read nor assert.
_SETUP_ONLY = {"new browser", "new context", "new page", "go to", "close browser",
               "close context", "close page", "log", "log to console",
               "set browser timeout"}
_TEXT_READERS = ("get text", "get texts")
_LOCATOR_READERS = (*_TEXT_READERS, "get attribute", "get property")
# Where a verify clause ends, i.e. where the next instruction starts: a comma, a
# semicolon, a sentence end, a line break, or "and" / "then".
_CLAUSE_END = re.compile(r",\s|;|\.(?:\s|$)|\n|\b(?:and|then)\b", re.IGNORECASE)
# The literal kinds that are an attribute of the element itself. Get Attribute /
# Get Property returns such a literal only when it reads that same attribute.
_ATTRIBUTE_LITERALS = {name: re.compile(p, re.IGNORECASE) for name, p in (
    ("aria-label", r"aria-label\s*[*^$]?=\s*[\"']([^\"']{3,})[\"']"),
    ("title", r"\btitle\s*[*^$]?=\s*[\"']([^\"']{3,})[\"']"),
    ("alt", r"\balt\s*[*^$]?=\s*[\"']([^\"']{3,})[\"']"),
)}
_LITERALS = (
    *(re.compile(p, re.IGNORECASE) for p in (
        r"text\s*=\s*\"([^\"]{3,})\"",
        r"text\s*=\s*'([^']{3,})'",
        r"^text\s*=\s*([^\"'>][^>]{2,}?)\s*(?:>>|$)",
        r"has-text\(\s*[\"']([^\"']{3,})[\"']",
    )),
    *_ATTRIBUTE_LITERALS.values(),
    re.compile(r"^role=\w+\[name\s*=\s*[\"']([^\"']{3,})[\"']", re.IGNORECASE),
)
_CHAIN_SPLIT = re.compile(r">>>?")
_NTH_PART = re.compile(r"nth\s*=\s*-?\d+")
_NUMERIC_ID = re.compile(r"(?:(?:^id=)|#|\[id=[\"']?)(\d{4,})")
_ID_FORMS = tuple(re.compile(p) for p in (
    r"(?:css=)?#([\w-]+)",
    r"(?:css=)?select#([\w-]+)",
    r"id=([\w-]+)",
    r"(?:css=)?\[id=['\"]?([\w-]+)['\"]?\]",
    r"xpath=//select\[@id=['\"]([\w-]+)['\"]\]",
))
_ASSIGN = re.compile(r"^[$@&]\{[^}]+\}\s*=?$")
_VARIABLE = re.compile(r"[$@&%]\{([^}]+)\}")
# Python's variable syntax inside an expression: `len($books) == 20`.
_PYTHON_VARIABLE = re.compile(r"\$(\w+)")
# A whole number: "== 20", "${20}" and "20." hold 20; "1920", "20.5", "20,000",
# "20s" and "item_20" do not.
_WHOLE_NUMBER = re.compile(r"(?<!\w)(?<!\d[.,])\d+(?![.,]?\d)(?!\w)")
_CELL_SPLIT = re.compile(r"\s{2,}|\t")
_LIBRARY_PREFIX = re.compile(r"^(browser|builtin|collections|string)\.")
_KEYWORD_SETTINGS = ("[setup]", "[teardown]")


@dataclass(frozen=True)
class _Statement:
    assigned: tuple[str, ...]
    keyword: str
    args: tuple[str, ...]


def _var_key(text: str) -> str:
    """Robot variable names ignore case, spaces and underscores."""
    m = _VARIABLE.search(text)
    inner = m.group(1) if m else text
    return re.sub(r"[\s_]", "", inner.lower())


def _keyword_key(text: str) -> str:
    name = re.sub(r"\s+", " ", text.replace("_", " ").strip().lower())
    return _LIBRARY_PREFIX.sub("", name)


def _cells(line: str) -> list[str]:
    """A line's cells; a cell starting with '#' starts a comment (Robot's rule)."""
    cells = [c.strip() for c in _CELL_SPLIT.split(line.strip()) if c.strip()]
    for i, cell in enumerate(cells):
        if cell.startswith("#"):
            return cells[:i]
    return cells


def _parse(code: str) -> tuple[dict[str, str], list[_Statement]]:
    """(variables, statements of every test case and user keyword body, in order)."""
    variables: dict[str, str] = {}
    statements: list[list[str]] = []
    section = ""
    last: list[str] | None = None
    last_var: str | None = None
    for raw in (code or "").splitlines():
        if raw.startswith("*"):
            section = raw.strip().strip("*").strip().lower()
            last, last_var = None, None
            continue
        cells = _cells(raw)
        if not cells:
            continue
        if section.startswith("variable"):
            if cells[0] == "..." and last_var is not None:
                variables[last_var] = " ".join([variables[last_var], *cells[1:]]).strip()
            elif _VARIABLE.match(cells[0]):
                last_var = _var_key(cells[0])
                variables[last_var] = " ".join(cells[1:])
            continue
        if not section.startswith(("test case", "task", "keyword")):
            continue
        if raw[:1] not in (" ", "\t"):
            last = None          # a test or keyword NAME line, not a statement
            continue
        if cells[0] == "..." and last is not None:
            last.extend(cells[1:])
            continue
        if cells[0].startswith("["):
            if cells[0].lower() not in _KEYWORD_SETTINGS or len(cells) < 2:
                last = None
                continue
            cells = cells[1:]
        last = list(cells)
        statements.append(last)
    parsed = []
    for cells in statements:
        assigned: list[str] = []
        i = 0
        while i < len(cells) - 1 and _ASSIGN.match(cells[i]):
            assigned.append(_var_key(cells[i]))
            i += 1
        parsed.append(_Statement(tuple(assigned), _keyword_key(cells[i]), tuple(cells[i + 1:])))
    return variables, parsed


def _resolve(text: str, variables: dict[str, str]) -> str:
    for _ in range(5):
        new = _VARIABLE.sub(lambda m: variables.get(_var_key(m.group(0)), m.group(0)), text)
        if new == text:
            return new.strip()
        text = new
    return text.strip()


def _norm_locator(locator: str) -> str:
    locator = locator.strip()
    for form in _ID_FORMS:
        m = form.fullmatch(locator)
        if m:
            return "id=" + m.group(1)
    return locator


def _is_assertion(stmt: _Statement) -> bool:
    if stmt.keyword.startswith(_ASSERTION_PREFIXES) or _SHOULD.search(stmt.keyword):
        return True
    return stmt.keyword.startswith("get ") and any(
        a.strip().lower() in _OPERATORS for a in stmt.args[:4])


def _select_check_cannot_fail(variables: dict[str, str], stmts: list[_Statement]) -> str | None:
    """The <select> locator a cannot-fail check reads, or None."""
    selected: dict[str, set[str]] = {}
    read_into: dict[str, str] = {}
    for stmt in stmts:
        if stmt.keyword == "select options by" and len(stmt.args) >= 3:
            locator = _norm_locator(_resolve(stmt.args[0], variables))
            selected[locator] = {_resolve(v, variables) for v in stmt.args[2:]}
        elif stmt.keyword == "get text" and stmt.args:
            locator = _norm_locator(_resolve(stmt.args[0], variables))
            if locator not in selected:
                continue
            if stmt.assigned:
                read_into[stmt.assigned[-1]] = locator
            if (len(stmt.args) >= 3 and stmt.args[1].strip().lower() in ("contains", "*=")
                    and _resolve(stmt.args[2], variables) in selected[locator]):
                return locator
        elif stmt.keyword == "should contain" and len(stmt.args) >= 2:
            if not _VARIABLE.fullmatch(stmt.args[0].strip()):
                continue
            locator = read_into.get(_var_key(stmt.args[0]))
            if locator is not None and _resolve(stmt.args[1], variables) in selected[locator]:
                return locator
    return None


def _locator_literals(locator: str, query: str,
                      patterns: tuple[re.Pattern[str], ...] = _LITERALS) -> list[str]:
    lowered = query.lower()
    found = [m.group(1).strip() for rx in patterns for m in rx.finditer(locator)]
    return [lit for lit in found if lit and lit.lower() not in lowered]


def _element_part(locator: str) -> str:
    """The part of a `>>` / `>>>` chain that finds the element read.

    Earlier parts are ancestors or frames. A trailing `nth=N` refines the part
    before it, so it is skipped: `text="John" >> nth=0` -> `text="John"`.
    """
    parts = [p.strip() for p in _CHAIN_SPLIT.split(locator) if p.strip()]
    while len(parts) > 1 and _NTH_PART.fullmatch(parts[-1]):
        parts.pop()
    return parts[-1] if parts else locator


def _answer_literals(stmt: _Statement, locator: str, query: str) -> list[str]:
    """The literals `stmt` reads back as its value, absent from the query.

    Get Text / Get Texts return the element's text, so any literal kind counts.
    Get Attribute / Get Property count only when they read the literal's own
    attribute: `Get Attribute  [title="X"]  title` returns X, `... href` does not.
    """
    part = _element_part(locator)
    if stmt.keyword in _TEXT_READERS:
        return _locator_literals(part, query)
    attribute = stmt.args[1].strip().lower() if len(stmt.args) > 1 else ""
    if attribute not in _ATTRIBUTE_LITERALS:
        return []
    return _locator_literals(part, query, (_ATTRIBUTE_LITERALS[attribute],))


def _verify_clauses(query: str) -> list[str]:
    """What the query asks to verify: the words after each verify word, lower-cased."""
    return [_CLAUSE_END.split(query[m.end():], maxsplit=1)[0].lower()
            for m in _VERIFY_Q.finditer(query)]


def _whole_numbers(text: str) -> set[int]:
    return {int(n) for n in _WHOLE_NUMBER.findall(text)}


def _used_variables(args: tuple[str, ...]) -> set[str]:
    """The variables a statement's arguments use: `${n}`, `@{items}`, Python's `$items`."""
    text = "  ".join(args)
    return ({_var_key(m.group(0)) for m in _VARIABLE.finditer(text)}
            | {_var_key(name) for name in _PYTHON_VARIABLE.findall(text)})


def _count_is_checked(query: str, variables: dict[str, str], stmts: list[_Statement]) -> bool:
    """True when the query asks to verify a number N and the test asserts a count against N.

    q10 ("get the titles of all books ... verify there are 20 books") answered by
    `Get Elements` / `Get Length` / `Should Be True  ${n} == 20`: the one thing the
    query lets pass or fail is checked, so the missing read is not reported.

    The assertion must be ON the count: a counting keyword's own inline assertion,
    or an assertion that uses a variable a counting keyword filled (directly, or
    through a keyword fed by one). An assertion that merely holds the number
    (`Wait For Elements State  xpath=(//li)[20]  visible`) checks no count. A
    variable assigned in the test body is never replaced by its declared start
    value: `${n}    0` under Variables is not what `${n} < 5` compares.
    """
    wanted = {n for clause in _verify_clauses(query) for n in _whole_numbers(clause)}
    if not wanted:
        return False
    assigned = {name for stmt in stmts for name in stmt.assigned}
    declared = {name: value for name, value in variables.items() if name not in assigned}
    counts: set[str] = set()
    for stmt in stmts:
        counter = stmt.keyword in _COUNTERS
        if not counter and not counts & _used_variables(stmt.args):
            continue
        # A counting keyword's first argument is what it counts, never the number expected.
        compared = "  ".join(stmt.args[1:] if counter else stmt.args)
        if _is_assertion(stmt) and wanted & _whole_numbers(_resolve(compared, declared)):
            return True
        counts.update(stmt.assigned)
    return False


def check_pass_quality(robot_code: str, user_query: str | None) -> list[Finding]:
    """Every pass-quality shape found in one generated test, one Finding per shape.

    Pure: two strings in, findings out, ordered as SHAPE_ORDER. An empty or None
    query runs only the query-free rules.
    """
    query = (user_query or "").strip()
    variables, stmts = _parse(robot_code)
    found: dict[str, str] = {}
    is_read = bool(query) and bool(_READ_Q.search(query))
    is_verify = bool(query) and bool(_VERIFY_Q.search(query))

    if (is_read and not any(s.keyword.startswith(_READERS) for s in stmts)
            and not _count_is_checked(query, variables, stmts)):
        found[READ_NOT_PERFORMED] = ""
    if is_verify and not any(_is_assertion(s) for s in stmts):
        found[VERIFY_WITHOUT_ASSERTION] = ""
    select_locator = _select_check_cannot_fail(variables, stmts)
    if select_locator is not None:
        found[SELECT_CHECK_CANNOT_FAIL] = select_locator
    if is_read:
        for stmt in stmts:
            if stmt.keyword not in _LOCATOR_READERS or not stmt.args:
                continue
            locator = _resolve(stmt.args[0], variables)
            literals = _answer_literals(stmt, locator, query)
            if literals:
                found.setdefault(READ_LOCATOR_IS_THE_ANSWER, literals[0][:40])
            if _NUMERIC_ID.search(locator):
                found.setdefault(FRAGILE_NUMERIC_ID, locator[:80])
    asks_more = not query or bool(_READ_Q.search(query) or _VERIFY_Q.search(query)
                                  or _ACTION_Q.search(query))
    if asks_more and all(s.keyword in _SETUP_ONLY for s in stmts):
        found[EMPTY_TEST] = ""
    return [Finding(shape, found[shape]) for shape in SHAPE_ORDER if shape in found]


def hollow_shapes(robot_code: str, user_query: str | None) -> set[str]:
    """The hollow shapes (never FRAGILE_NUMERIC_ID) of one test."""
    return {f.shape for f in check_pass_quality(robot_code, user_query) if f.hollow}
