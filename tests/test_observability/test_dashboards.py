"""Static guards over the committed Grafana dashboard JSON.

These run without a database or a Grafana instance. They encode the rules
that are easy to violate by hand and expensive to notice in a panel:
the naive-ts time trap, the one-table spine rule, the granted-table list,
and the standing rule that run ids are never truncated.

Referenced by: nothing — pytest entry point.
Depends on: observability/grafana/dashboards/*.json
"""
import json
import re
from pathlib import Path

import pytest

DASHBOARD_DIR = Path("observability/grafana/dashboards")
ROLE_SCRIPT = Path("observability/postgres/create_readonly_role.sql")

GRANTED_TABLES = {
    "workflow_metrics",
    "test_runs",
    "llm_traces",
    "execution_records",
    "learning_metrics",
    "nl_feedback_corrections",
    "trigger_events",
}

BENCH_TABLES = {
    "bench.sweeps",
    "bench.runs",
}

# Dashboards reading the bench corpus. The detachment rule — bench data must
# never reach History or the production metrics dashboards — is enforced here
# rather than left to convention, because grafana_ro can read both schemas.
BENCH_DASHBOARDS = {
    "bench-weakest-now.json",
    "bench-change-impact.json",
}

# A schema-qualified bench.<table> reference. Tolerant of the quoting and
# whitespace variants real SQL tools emit around a qualified identifier —
# "bench"."runs", bench . runs, bench."runs", "bench".runs — because mutation
# testing showed a production dashboard reading any of those forms passed
# every check in this suite: this regex (via _bench_refs, below) is the only
# thing keeping bench data out of the production dashboards.
_SCHEMA_TABLE_RE = re.compile(
    r'\b(?:FROM|JOIN)\s+"?bench"?\s*\.\s*"?([a-z_][a-z0-9_]*)"?', re.IGNORECASE)


def _bench_refs(sql: str) -> set[str]:
    """Schema-qualified bench.<table> references in `sql`, normalised to
    `bench.<table>` regardless of which quoting/whitespace variant was used."""
    return {f"bench.{t.lower()}" for t in _SCHEMA_TABLE_RE.findall(sql)}


# A maximal run of 5 or more digits — an account number, a phone number, any
# other numeric identifier. `(?<!\d)`/`(?!\d)` keep the run maximal so a
# longer number is not undercounted as a shorter one.
_LONG_DIGIT_RUN_RE = re.compile(r"(?<!\d)\d{5,}(?!\d)")

# A hostname: a name, a literal dot, then a TLD. Broad enough to catch a real
# customer domain without this file ever naming one.
_HOSTLIKE_RE = re.compile(
    r"\b[a-z0-9][a-z0-9-]*\.(?:com|net|org|io|in|co)\b", re.IGNORECASE)

# Any FROM/JOIN target that is not a CTE name and not granted is a defect.
_TABLE_RE = re.compile(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)", re.IGNORECASE)
_CTE_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\s+AS\s*\(", re.IGNORECASE)

# Catches LEFT(...)/SUBSTRING(...)/SUBSTR(...) on run_id or workflow_id whether
# or not the column is table-qualified (e.g. LEFT(m.workflow_id, 8)) — an alias
# in between defeats a plain substring check on "left(workflow_id".
_TRUNCATE_CALL_RE = re.compile(
    r"\b(?:left|substring|substr)\s*\(\s*(?:[a-z_][a-z0-9_]*\.)?(?:run_id|workflow_id)\b",
    re.IGNORECASE,
)
# Catches ::varchar(n)/::char(n) narrowing on the same columns, aliased or not.
_TRUNCATE_CAST_RE = re.compile(
    r"\b(?:[a-z_][a-z0-9_]*\.)?(?:run_id|workflow_id)\s*::\s*(?:varchar|char)\s*\(\s*\d+\s*\)",
    re.IGNORECASE,
)

# jsonb_each/jsonb_each_text/jsonb_array_elements error outright — not NULL,
# an ERROR — when the target value is a JSON null rather than an object/array.
# `data ? 'key'` is true for a JSON null, so it is not a guard; only
# jsonb_typeof(...) actually is. F1 shipped exactly this as a raw Postgres
# error on ~5% of real run ids.
_SRF_RE = re.compile(r"\bjsonb_(?:each|each_text|array_elements)\s*\(", re.IGNORECASE)

# A dashboard template variable inside rawSql. `$__timeFrom()`/`$__timeTo()`/
# `$__timeFilter()` are Grafana MACROS, not variables — they expand to SQL the
# datasource builds itself and are never user text, so `$__` is excluded.
# Matches `$name`, `${name}` and `${name:format}`, capturing the format.
_SQL_TEMPLATE_VAR_RE = re.compile(
    r"\$(?!__)\{?([a-zA-Z0-9_]+)(?::([a-zA-Z0-9_]+))?\}?"
)
# A variable reference wrapped in literal single quotes, in either spelling.
# Grafana's own docs call this out: :sqlstring already supplies the quotes, so
# hand-wrapping produces ''value'' and breaks the query.
_QUOTED_TEMPLATE_VAR_RE = re.compile(r"'\s*\$(?!__)\{?[a-zA-Z0-9_]+[^']*'")

# A bare `ts` reference anywhere — column, alias target, inside date_trunc,
# inside $__timeFilter — not just the four literal spellings a substring
# check would need to enumerate by hand.
_BARE_TS_RE = re.compile(r"\bts\b", re.IGNORECASE)

# FROM-clause table/comma parsing, used by both the granted-tables check and
# the inner-join check so a comma-join (`FROM a, b`) is recognised as a real
# multi-table join rather than the legitimate `FROM t, jsonb_each(t.col)`
# lateral-expansion idiom this codebase uses throughout.
_FROM_CLAUSE_RE = re.compile(
    r"\bFROM\s+(.*?)(?=\bWHERE\b|\bGROUP\s+BY\b|\bORDER\s+BY\b|\bJOIN\b|\bLIMIT\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_FUNCTION_CALL_RE = re.compile(r"^[a-z_][a-z0-9_]*\s*\(", re.IGNORECASE)
_LEADING_IDENT_RE = re.compile(r"^([a-z_][a-z0-9_]*)", re.IGNORECASE)


def _split_top_level_commas(clause: str) -> list[str]:
    """Split on commas that sit outside every parenthesis.

    `str.split(",")` cuts inside subqueries too: `FROM (SELECT a, b FROM
    test_runs) x` became the segments `(SELECT a` and `b FROM test_runs) x`,
    and the second one reads as a table named `b`. No granted-table list will
    ever hold `b`, so a correct panel failed with a message naming a table
    that does not exist.
    """
    parts, depth, start = [], 0, 0
    for i, char in enumerate(clause):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)   # tolerate a stray ) rather than going negative
        elif char == "," and depth == 0:
            parts.append(clause[start:i])
            start = i + 1
    parts.append(clause[start:])
    return parts


def _from_clause_real_tables(sql: str) -> list[str]:
    """Bare table identifiers in a single FROM clause, comma-separated or not.

    A `jsonb_each(...)`-shaped segment — an identifier followed by an opening
    paren, with or without whitespace between them — is excluded: that is the
    lateral expansion idiom, not a second table. A parenthesised subquery is
    excluded too, by starting with `(` rather than an identifier.
    """
    match = _FROM_CLAUSE_RE.search(sql)
    if not match:
        return []
    tables = []
    for segment in _split_top_level_commas(match.group(1)):
        segment = segment.strip()
        if not segment or _FUNCTION_CALL_RE.match(segment):
            continue
        ident = _LEADING_IDENT_RE.match(segment)
        if ident:
            tables.append(ident.group(1).lower())
    return tables


def _dashboards() -> list[Path]:
    return sorted(DASHBOARD_DIR.glob("*.json"))


def _sql_targets(dashboard: dict) -> list[str]:
    out = []
    for panel in dashboard.get("panels", []):
        for target in panel.get("targets", []):
            sql = target.get("rawSql")
            if sql:
                out.append(sql)
    return out


def test_dashboard_dir_is_not_empty():
    assert _dashboards(), "no dashboard JSON found"


class TestFromClauseParsing:
    """_from_clause_real_tables underpins two guards, so its own edges matter.

    Both the granted-tables check and the inner-join check treat whatever it
    returns as real tables. A phantom name there fails a panel that is
    perfectly correct, and the failure message names a table that does not
    exist — which is a long way from the actual mistake for whoever hits it.
    """

    def test_lateral_expansion_is_not_a_second_table(self):
        sql = "SELECT 1 FROM workflow_metrics m, jsonb_each(m.data) AS s"
        assert _from_clause_real_tables(sql) == ["workflow_metrics"]

    def test_comma_join_of_two_real_tables_is_reported(self):
        sql = "SELECT 1 FROM workflow_metrics m, test_runs r WHERE m.x = r.x"
        assert _from_clause_real_tables(sql) == ["workflow_metrics", "test_runs"]

    def test_subquery_select_list_is_not_a_table(self):
        """A comma inside the parentheses belongs to the subquery, not the
        FROM clause. Splitting on it made `b` look like a second table, and
        no granted-table list will ever contain `b`."""
        sql = "SELECT x FROM (SELECT a, b FROM test_runs) x"
        assert _from_clause_real_tables(sql) == []

    def test_function_call_with_a_space_is_still_a_function(self):
        sql = "SELECT 1 FROM workflow_metrics m, jsonb_each (m.data) AS s"
        assert _from_clause_real_tables(sql) == ["workflow_metrics"]


class TestBenchRefDetection:
    """Mutation testing found that a production dashboard reading
    `"bench"."runs"` (quoted identifiers) or `bench . runs` (whitespace
    around the dot) passed every check in this suite — both are ordinary SQL
    that a client tool commonly emits on copy. These tests prove _bench_refs
    actually detects each mutated form, not merely that the dashboards
    shipped today happen to pass.
    """

    def test_detects_plain_form(self):
        assert _bench_refs("SELECT 1 FROM bench.runs") == {"bench.runs"}

    def test_detects_fully_quoted_form(self):
        assert _bench_refs('SELECT 1 FROM "bench"."runs"') == {"bench.runs"}

    def test_detects_whitespace_around_the_dot(self):
        assert _bench_refs("SELECT 1 FROM bench . runs") == {"bench.runs"}

    def test_detects_partially_quoted_forms(self):
        assert _bench_refs('SELECT 1 FROM bench."runs"') == {"bench.runs"}
        assert _bench_refs('SELECT 1 FROM "bench".runs') == {"bench.runs"}

    def test_ignores_unrelated_tables(self):
        assert _bench_refs("SELECT 1 FROM workflow_metrics") == set()


def test_readonly_role_grants_match_the_dashboards():
    """The GRANT list in the shipped .sql must equal GRANTED_TABLES.

    test_readonly_role.py can only run against a live nlrf-postgres container
    holding the app schema, so it skips in CI — the script grants on seven app
    tables and errors on a database that lacks them. That leaves the realistic
    regression uncovered: someone adds a panel on an eighth table and forgets
    the GRANT, and the panel fails at read time with a permission error no test
    saw. This closes it with no database at all.

    Set equality in BOTH directions is the point. Missing a grant breaks a
    panel; granting a table no dashboard reads widens the role past least
    privilege, which is the property observability/README.md advertises by
    name. Combined with test_every_sql_target_queries_only_granted_tables
    (dashboards are a subset of GRANTED_TABLES), this pins the whole chain:
    what panels query == what the constant lists == what the role is granted.
    """
    sql = ROLE_SCRIPT.read_text(encoding="utf-8")
    match = re.search(
        r"\bGRANT\s+SELECT\s+ON\s+(.*?)\s+TO\s+grafana_ro\b", sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert match, f"{ROLE_SCRIPT.name} has no `GRANT SELECT ON ... TO grafana_ro`"
    granted = {t.strip().lower() for t in match.group(1).split(",") if t.strip()}
    assert granted == GRANTED_TABLES, (
        f"{ROLE_SCRIPT.name} grants {sorted(granted)}, "
        f"GRANTED_TABLES lists {sorted(GRANTED_TABLES)}"
    )


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_dashboard_parses_and_has_identity(path: Path):
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    assert dashboard.get("uid"), f"{path.name} has no uid"
    assert dashboard.get("title"), f"{path.name} has no title"
    assert dashboard.get("panels"), f"{path.name} has no panels"


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_every_sql_target_queries_only_granted_tables(path: Path):
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    for sql in _sql_targets(dashboard):
        ctes = {name.lower() for name in _CTE_RE.findall(sql)}
        # LATERAL expansions alias jsonb functions, not tables. `bench` is the
        # SCHEMA qualifier of bench.<table>, which both collection paths below
        # see as a bare identifier; the bench tables themselves are checked by
        # test_bench_and_production_dashboards_never_mix.
        ignored = {"lateral", "jsonb_each", "jsonb_array_elements",
                   "jsonb_each_text", "bench"}
        referenced = {
            t.lower() for t in _TABLE_RE.findall(sql) if t.lower() not in ignored
        }
        # A comma-join (`FROM a, b`) names its second table without a FROM
        # or JOIN keyword in front of it, which _TABLE_RE never sees.
        referenced |= {
            t.lower() for t in _from_clause_real_tables(sql)
            if t.lower() not in ignored
        }
        unknown = referenced - GRANTED_TABLES - ctes
        assert not unknown, f"{path.name} queries ungranted tables: {sorted(unknown)}"


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_sql_panels_escape_template_variables(path: Path):
    """Every template variable in rawSql must carry the :sqlstring format.

    `WHERE run_id = '$run_id'` pastes a textbox value straight into the
    statement. The blast radius is small — Grafana is loopback-only and
    grafana_ro can only SELECT on seven tables — but it is still a value the
    reader controls closing the string and appending predicates, and a
    shareable dashboard URL (`?var-run_id=...`) makes it someone else's
    browser that runs it. It also breaks on any legitimate value containing a
    quote.

    `${run_id:sqlstring}` is Grafana's own answer: it quotes the value and
    doubles embedded single quotes. Verified present as VariableFormatID
    .SQLString in the pinned grafana/grafana:11.6.0 image.

    Both halves are asserted. The format check catches a raw `$run_id`; the
    quote check catches `'${run_id:sqlstring}'`, which Grafana's docs warn
    yields ''value'' because the formatter already supplies the quotes.
    """
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    for sql in _sql_targets(dashboard):
        assert not _QUOTED_TEMPLATE_VAR_RE.search(sql), (
            f"{path.name} wraps a template variable in literal single quotes; "
            f":sqlstring already quotes it: {sql[:200]}"
        )
        for name, fmt in _SQL_TEMPLATE_VAR_RE.findall(sql):
            assert fmt == "sqlstring", (
                f"{path.name} interpolates ${name} into SQL with format "
                f"{fmt or 'none'!r} — use ${{{name}:sqlstring}}: {sql[:200]}"
            )


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_no_panel_time_filters_on_naive_ts(path: Path):
    """workflow_metrics.ts is naive, and not consistently naive. Bucketing or
    windowing on it silently shifts results by an amount that varies row to
    row.

    Re-measured 2026-08-10 against llm_traces.created_at, which is tz-aware,
    over the 146 rows that join: 143 sit 5:30:13 ahead of UTC — written by the
    host process in local time — and 3 sit within seconds of UTC, written by
    the containerised backend, whose clock is UTC. So `ts` is not one column
    with one offset; it records whatever the writing process thought local
    meant. No `AT TIME ZONE` correction can repair that, which makes the rule
    below stronger than the constant-5.5h story it replaces: the column is not
    merely shifted, it is ambiguous.

    A four-literal-substring check (date_trunc('day', m.ts, $__timeFilter(ts))
    only catches those exact spellings — an unaliased date_trunc('hour', ts)
    or a bare `WHERE ts BETWEEN ...` both slip past it. No panel legitimately
    reads `ts` at all, so assert the bare token never appears as a column
    reference, in any spelling."""
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    for sql in _sql_targets(dashboard):
        assert not _BARE_TS_RE.search(sql), (
            f"{path.name} references the naive ts column: {sql[:200]}"
        )


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_panels_ignoring_the_time_picker_say_so(path: Path):
    """A panel the time picker does not move has to admit it.

    Every dashboard ships a picker, and on the aggregate boards most panels
    cannot honour it. workflow_metrics has exactly one time column, `ts`, and
    it is unusable: naive, and not even consistently naive. Measured against
    llm_traces.created_at on 2026-08-10 — 143 rows written by the host process
    sit 5:30:13 ahead of UTC, 3 rows written by the containerised backend sit
    within seconds of it. There is no fixed offset that repairs a column whose
    skew depends on which process wrote the row, which is why
    test_no_panel_time_filters_on_naive_ts forbids touching it at all.

    That leaves a real trap: narrow the picker to six hours and one panel
    moves while eight do not, with nothing on screen saying which is which.
    Requiring the words in the description is crude, but it puts the fact in
    the ⓘ tooltip the reader is already looking at, and it fails loudly when
    someone adds a ninth unfiltered panel and says nothing.

    trace-one-run.json is exempt: it selects one run by id, so time is not a
    dimension there at all.
    """
    if path.name == "trace-one-run.json":
        return
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    for panel in dashboard.get("panels", []):
        sqls = [t["rawSql"] for t in panel.get("targets", []) if t.get("rawSql")]
        if not sqls or any("$__time" in sql for sql in sqls):
            continue
        description = panel.get("description") or ""
        assert "All-time" in description, (
            f"{path.name} panel {panel.get('title')!r} applies no time filter "
            f"but its description does not say so"
        )


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_aggregate_dashboards_use_no_inner_join(path: Path):
    """Cross-table history is sparse: 30/429 metrics rows have a test_runs
    row, 9/118 execution records join to metrics. A bare or INNER JOIN in an
    aggregate panel silently drops most of the data.

    Every occurrence of the JOIN keyword must be qualified LEFT JOIN
    (or LEFT OUTER JOIN / CROSS JOIN) — a bare JOIN or an explicit INNER
    JOIN fails. A comma-join (`FROM a, b`) is also an inner join and has no
    LEFT-JOIN spelling, so two or more real tables in one FROM clause fail
    outright regardless of the JOIN-keyword scan below. trace-one-run.json
    is exempt: on a per-id dashboard a missing partner row is informative,
    not a silently dropped population.
    """
    if path.name == "trace-one-run.json":
        return  # per-id dashboard: a missing partner row is informative
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    for sql in _sql_targets(dashboard):
        comma_tables = _from_clause_real_tables(sql)
        assert len(comma_tables) < 2, (
            f"{path.name} comma-joins real tables in one FROM clause, which "
            f"is always an unqualified inner join: {sorted(set(comma_tables))}"
        )
        collapsed = " ".join(sql.split()).lower()
        words = collapsed.split(" ")
        for i, word in enumerate(words):
            if word != "join":
                continue
            prev1 = words[i - 1] if i >= 1 else ""
            prev2 = words[i - 2] if i >= 2 else ""
            qualified = prev1 in ("left", "cross") or (prev1 == "outer" and prev2 == "left")
            assert qualified, (
                f"{path.name} uses an unqualified or inner join in an "
                f"aggregate panel: ...{' '.join(words[max(0, i - 3):i + 1])}..."
            )


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_run_ids_are_never_truncated(path: Path):
    """Standing owner rule: full uuid, always.

    A plain substring check on "left(workflow_id" misses a table-qualified
    call like LEFT(m.workflow_id, 8) — the alias sits between the function
    and the column. Use regexes that match with or without a qualifying
    alias, covering LEFT/SUBSTRING/SUBSTR calls and ::varchar(n)/::char(n)
    narrowing casts.
    """
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    for sql in _sql_targets(dashboard):
        collapsed = " ".join(sql.split()).lower()
        assert not _TRUNCATE_CALL_RE.search(collapsed), (
            f"{path.name} truncates a run/workflow id via LEFT/SUBSTRING/SUBSTR"
        )
        assert not _TRUNCATE_CAST_RE.search(collapsed), (
            f"{path.name} truncates a run/workflow id via ::varchar(n)/::char(n)"
        )


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_jsonb_expansion_is_type_guarded(path: Path):
    """jsonb_each/jsonb_each_text/jsonb_array_elements raise a hard Postgres
    error — not a NULL, an ERROR — when the target value is a JSON null
    rather than an object/array. `data ? 'key'` is true for a JSON null, so
    it does not guard against this; only jsonb_typeof(...) does. This is the
    exact defect F1 shipped as a raw error on ~5% of real run ids on
    trace-one-run.json, and the same root cause behind F2 and F3."""
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    for sql in _sql_targets(dashboard):
        if _SRF_RE.search(sql):
            assert "jsonb_typeof" in sql, (
                f"{path.name} expands a jsonb field via jsonb_each/"
                f"jsonb_each_text/jsonb_array_elements with no jsonb_typeof "
                f"guard: {sql[:200]}"
            )


def test_trace_dashboard_has_a_run_id_variable():
    """A bare check that a variable named run_id exists is not enough to pin
    the defect Task 5's review called CRITICAL: LogQL's `|= ""` line filter
    matches every line, so an empty $run_id would render the entire
    unfiltered log stream — the aggregate panel this dashboard's own
    Global Constraints forbid by name. That fix survives only because a
    UUID-shaped sentinel sits in BOTH the variable's `query` and its
    `current.value` — deleting either reopens the hole while this bare
    existence check keeps passing. Assert both equal the sentinel actually
    committed in the JSON, and that every Loki target's expression actually
    interpolates $run_id."""
    path = DASHBOARD_DIR / "trace-one-run.json"
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    variables = {v["name"]: v for v in dashboard.get("templating", {}).get("list", [])}
    assert "run_id" in variables

    run_id_var = variables["run_id"]
    sentinel = run_id_var.get("query")
    assert sentinel, "run_id variable has no default query value to act as a sentinel"
    assert run_id_var.get("current", {}).get("value") == sentinel, (
        "run_id variable's current.value must match its query sentinel — "
        "otherwise a cleared box does not fall back to the empty-safe default"
    )

    loki_exprs = [
        target.get("expr", "")
        for panel in dashboard.get("panels", [])
        if panel.get("datasource", {}).get("type") == "loki"
        for target in panel.get("targets", [])
    ]
    assert loki_exprs, "no Loki target found on the trace dashboard"
    for expr in loki_exprs:
        assert "$run_id" in expr, f"Loki target does not interpolate $run_id: {expr}"


def test_bench_grant_is_present_and_exact():
    """A second GRANT block, matched separately from the seven-table app
    grant, so neither can silently absorb the other."""
    sql = ROLE_SCRIPT.read_text(encoding="utf-8")
    match = re.search(
        r"\bGRANT\s+SELECT\s+ON\s+((?:bench\.[a-z_]+\s*,?\s*)+)TO\s+grafana_ro\b",
        sql, re.IGNORECASE)
    assert match, "create_readonly_role.sql has no GRANT SELECT on bench tables"
    granted = {t.strip().lower() for t in match.group(1).split(",") if t.strip()}
    assert granted == BENCH_TABLES, f"grants {sorted(granted)}"
    assert re.search(r"GRANT\s+USAGE\s+ON\s+SCHEMA\s+bench\s+TO\s+grafana_ro",
                     sql, re.IGNORECASE), "no USAGE grant on schema bench"


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_bench_and_production_dashboards_never_mix(path: Path):
    """The detachment requirement, enforced statically.

    A bench dashboard may read only the bench schema; a production dashboard
    may read only the granted app tables. Blending them would put bench runs
    into a History-facing number, which is the one thing bench data must never
    do — and grafana_ro can read both, so nothing else stops it.
    """
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    is_bench = path.name in BENCH_DASHBOARDS
    for sql in _sql_targets(dashboard):
        ctes = {name.lower() for name in _CTE_RE.findall(sql)}
        bench_refs = _bench_refs(sql)
        app_refs = {
            t.lower() for t in _TABLE_RE.findall(sql)
        } & GRANTED_TABLES
        app_refs -= ctes
        if is_bench:
            assert not app_refs, (
                f"{path.name} is a bench dashboard but reads production "
                f"tables: {sorted(app_refs)}")
            assert bench_refs <= BENCH_TABLES, (
                f"{path.name} reads ungranted bench tables: "
                f"{sorted(bench_refs - BENCH_TABLES)}")
        else:
            assert not bench_refs, (
                f"{path.name} is a production dashboard but reads bench "
                f"data: {sorted(bench_refs)}")


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_no_dashboard_hardcodes_an_identifying_value(path: Path):
    """The repository is public. No committed dashboard JSON may hardcode a
    value shaped like an identifier — a long number or a hostname — no
    matter which specific value it is. A fixed list of known strings only
    catches the ones someone already found and typed in; a shape check also
    catches the one nobody has listed yet.

    A run of 5+ digits is flagged unless every digit is 0:
    trace-one-run.json's run_id variable ships a UUID built from
    00000000/000000000000 as its empty-safe default sentinel, and that is a
    placeholder, not an identifying number. A hostname is a name, a literal
    dot, then a TLD (com/net/org/io/in/co) — broad enough to catch a real
    domain without this file ever having to name one.
    """
    text = path.read_text(encoding="utf-8")
    long_digit_runs = {m for m in _LONG_DIGIT_RUN_RE.findall(text) if set(m) != {"0"}}
    assert not long_digit_runs, (
        f"{path.name} hardcodes a 5+ digit number: {sorted(long_digit_runs)}")
    hostlike = set(_HOSTLIKE_RE.findall(text))
    assert not hostlike, f"{path.name} hardcodes a hostname: {sorted(hostlike)}"
