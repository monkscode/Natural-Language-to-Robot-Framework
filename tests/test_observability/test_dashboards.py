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
from unittest.mock import patch

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
    "bench-time.json",
}

# Dashboards that show ONE run, selected by id. Three aggregate rules below
# exempt these — the time picker rule, the inner-join rule and the cost-split
# rule — and all three exemptions rest on the same premise: the panels are
# already narrowed to a single run, so a missing partner row is informative and
# time is not a dimension. That premise was asserted nowhere until
# test_every_per_run_panel_scopes_to_the_run_id below, which is why the name
# exists as a constant rather than as a filename repeated at four call sites:
# adding a second per-run dashboard here buys the exemptions AND the guard that
# makes them true, in one edit.
PER_RUN_DASHBOARDS = {
    "trace-one-run.json",
}

# A schema-qualified bench.<table> reference. Tolerant of the quoting and
# whitespace variants real SQL tools emit around a qualified identifier —
# "bench"."runs", bench . runs, bench."runs", "bench".runs — because mutation
# testing showed a production dashboard reading any of those forms passed
# every check in this suite: this regex (via _bench_refs, below) is the only
# thing keeping bench data out of the production dashboards.
_SCHEMA_TABLE_RE = re.compile(
    r'\b(?:FROM|JOIN)\s+(?:LATERAL\s+)?"?bench"?\s*\.\s*"?([a-z_][a-z0-9_]*)"?',
    re.IGNORECASE)


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


def _production_table_refs(sql: str, ctes: set[str]) -> set[str]:
    """Granted app tables this SQL reads, comma-joined ones included.

    _TABLE_RE alone sees only the identifier directly after FROM or JOIN, so
    the second table of `FROM bench.runs, workflow_metrics` is invisible to
    it — it captures `bench`, the schema qualifier, and stops. Merging
    _from_clause_real_tables closes that, exactly as the granted-tables guard
    already does for its own reader.
    """
    refs = {t.lower() for t in _TABLE_RE.findall(sql)}
    refs |= {t.lower() for t in _from_clause_real_tables(sql)}
    return (refs & GRANTED_TABLES) - ctes


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

    def test_detects_lateral_join_form(self):
        assert _bench_refs(
            "SELECT 1 FROM execution_records e "
            "CROSS JOIN LATERAL bench.runs r"
        ) == {"bench.runs"}


class TestProductionRefDetection:
    """The mirror of TestBenchRefDetection, for the other direction.

    _bench_refs keeps bench data out of the production dashboards. This helper
    keeps production data out of the BENCH dashboards, and it had the weaker
    reader: _TABLE_RE alone sees only the identifier directly after FROM or
    JOIN, so the second table of a comma-join was invisible to it. The
    granted-tables guard already compensates with _from_clause_real_tables and
    says so in a comment; the isolation guard did not, so a bench dashboard
    could read `FROM bench.runs, workflow_metrics` and pass the one check that
    enforces detachment.
    """

    def test_comma_joined_production_table_is_detected(self):
        """The gap. _TABLE_RE returns only ['bench'] here."""
        sql = "SELECT 1 FROM bench.runs, workflow_metrics WHERE 1=1"
        assert _TABLE_RE.findall(sql) == ["bench"], "premise changed"
        assert _production_table_refs(sql, set()) == {"workflow_metrics"}

    def test_plain_from_and_join_forms_still_detected(self):
        assert _production_table_refs("SELECT 1 FROM test_runs", set()) == {"test_runs"}
        assert _production_table_refs(
            "SELECT 1 FROM bench.runs r JOIN llm_traces t ON t.x = r.x", set()
        ) == {"llm_traces"}

    def test_cte_name_is_not_a_production_table(self):
        sql = "WITH test_runs AS (SELECT 1) SELECT 1 FROM test_runs"
        assert _production_table_refs(sql, {"test_runs"}) == set()

    def test_lateral_expansion_is_not_a_production_table(self):
        sql = "SELECT 1 FROM bench.runs r, jsonb_each(r.metrics) AS s"
        assert _production_table_refs(sql, set()) == set()

    def test_ungranted_table_is_not_reported(self):
        """The guard's job is detachment, not the granted-table check that
        test_every_sql_target_queries_only_granted_tables already performs."""
        assert _production_table_refs("SELECT 1 FROM audit_log", set()) == set()


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

    PER_RUN_DASHBOARDS are exempt: they select one run by id, so time is not a
    dimension there at all. That premise is not taken on trust —
    test_every_per_run_panel_scopes_to_the_run_id asserts every panel on those
    dashboards actually carries the id restriction this exemption assumes.
    """
    if path.name in PER_RUN_DASHBOARDS:
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
    outright regardless of the JOIN-keyword scan below. PER_RUN_DASHBOARDS are
    exempt: on a per-id dashboard a missing partner row is informative, not a
    silently dropped population — and that the panels really are per-id is
    asserted by test_every_per_run_panel_scopes_to_the_run_id, not assumed.
    """
    if path.name in PER_RUN_DASHBOARDS:
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


# A panel that aggregates over llm_traces without restricting to rows that
# carry a model is not counting LLM calls. Measured 2026-08-12: the table
# held 100,907 rows, of which 2,829 were model calls and the rest were
# OpenTelemetry HTTP/agent/task/orchestration spans — and every one of the
# 1,298 ERROR rows belonged to those spans, so the shipped "LLM call failure
# rate" panel reported 1,298 failures while the real LLM failure count was 0.
# A per-row panel that lists one run's calls (trace-one-run panel 6) does not
# aggregate and is deliberately out of scope.
_LLM_TRACES_FROM_RE = re.compile(r"\bFROM\s+llm_traces\b", re.IGNORECASE)
_AGGREGATE_RE = re.compile(
    r"\b(?:count|sum|avg|min|max|percentile_cont)\s*\(", re.IGNORECASE)
# Anchored to WHERE: a bare substring search over the whole SQL string is
# satisfied by an inert SELECT-list expression like `nullif(model,'') AS
# model` while the aggregate still counts every span row — that is a real
# shape a future author would write, since model values are unnormalised
# and a normalising nullif(model,'') in a SELECT list (e.g. to group by
# model) is the natural thing to reach for. Requiring the predicate inside
# a WHERE clause is what actually restricts the rows the aggregate counts.
#
# This guard needs no post-FROM bound the way Task 2's cost-split guard
# below does, and that is not an oversight — the two situations differ. On
# llm_traces, a select-list `count(*) FILTER (WHERE nullif(model,'') IS NOT
# NULL)` genuinely counts only the rows that carry a model: a FILTER clause
# restricts what its own aggregate sees, so accepting that form here is
# correct. On the cost-split medians the equivalent FILTER form is exactly
# the defect Task 2 exists to catch — `percentile_cont` carries no FILTER of
# its own, so it keeps running over every row no matter what a sibling
# `count(*) FILTER` restricts. Same surface shape, one safe and one not,
# because the aggregates in that query don't share a row set the way COUNT
# and its own FILTER do. Concretely: the shipped "LLM calls and failures per
# hour" panel is what satisfies this anchor's `\bWHERE\b` token today — its
# select-list `count(*) FILTER (WHERE status <> 'OK')` comes first in the
# rawSql, and the real `WHERE nullif(model, '') IS NOT NULL` that actually
# restricts the row set follows later in the same string.
_MODEL_RESTRICTION_RE = re.compile(
    r"\bWHERE\b.*(?:nullif\s*\(\s*model\s*,[^)]*\)\s*IS\s+NOT\s+NULL"
    r"|\bmodel\s+IS\s+NOT\s+NULL\b)",
    re.IGNORECASE | re.DOTALL)


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_llm_traces_aggregates_count_only_real_llm_calls(path: Path):
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    for sql in _sql_targets(dashboard):
        if not _LLM_TRACES_FROM_RE.search(sql) or not _AGGREGATE_RE.search(sql):
            continue
        assert _MODEL_RESTRICTION_RE.search(sql), (
            f"{path.name} aggregates over llm_traces without restricting to "
            f"rows that carry a model, so it counts OpenTelemetry spans as "
            f"LLM calls: {sql[:200]}"
        )


def test_llm_traces_guard_fires_on_a_select_list_model_filter(tmp_path):
    """The aggregate guard's own mutation test, in the style of
    test_bench_grant_guard_fires_on_a_commented_out_grant above.

    Proving _MODEL_RESTRICTION_RE matches a WHERE-clause predicate is not
    the same as proving it REJECTS one that sits somewhere else: a bare
    substring search over the whole SQL string is satisfied by
    `nullif(model,'') AS model` sitting in the SELECT list, with no
    restriction in the WHERE clause at all — the aggregate still counts
    every one of the table's 100,907 OpenTelemetry span rows. Mutates a tmp
    copy of cost-latency-capacity.json with exactly that shape; never
    touches the committed file.
    """
    source = DASHBOARD_DIR / "cost-latency-capacity.json"
    dashboard = json.loads(source.read_text(encoding="utf-8"))
    dashboard["panels"][0]["targets"][0]["rawSql"] = (
        "SELECT nullif(model,'') AS model, count(*) AS llm_calls "
        "FROM llm_traces GROUP BY 1"
    )

    mutant = tmp_path / source.name
    mutant.write_text(json.dumps(dashboard), encoding="utf-8")

    with pytest.raises(AssertionError, match="without restricting"):
        test_llm_traces_aggregates_count_only_real_llm_calls(mutant)


# A median over a column where most rows are zero lands in the zero mass and
# reports 0.0000 next to a non-zero total, which reads as a broken panel.
# Measured 2026-08-12: of 447 workflow_metrics rows, 157 carry crewai_cost > 0
# and 97 carry browser_use_cost > 0, while 335 carry total_cost > 0.
_COST_SPLIT_FIELD_RE = re.compile(
    r"data->>'(?:crewai_cost|browser_use_cost)'", re.IGNORECASE)

# Used by _after_from below and, for the complementary span, by
# _duration_aliases: one takes the text from the first FROM onward, the other
# the select list before it. Keyword-based on purpose: _after_from used to
# locate FROM with a literal `sql.upper().find(" FROM ")`, which requires a
# space on both sides and returns -1 — failing open, not loudly — on a rawSql
# with a newline or a `)` immediately before FROM.
_FROM_KEYWORD_RE = re.compile(r"\bFROM\b", re.IGNORECASE)


def _after_from(sql: str) -> str:
    """The text from the first FROM keyword onward — where a real WHERE clause lives.

    A select-list `count(*) FILTER (WHERE ...)` also contains the WHERE
    token, so anchoring on the token alone cannot tell a restriction that
    narrows the aggregate's row set from one that merely labels a column.
    Everything before the first FROM is the select list, so bounding the
    search after it excludes the FILTER form by construction. The split is
    keyword-based, not space-delimited: a literal " FROM " substring search
    returns -1 (failing open, handing back the whole statement — including
    the FILTER form this bound exists to exclude) on any rawSql with a
    newline or a closing paren immediately before FROM, which a hand-edited
    dashboard file can easily produce. _duration_aliases takes the mirror
    image of this split — the select list BEFORE the same FROM keyword, after
    any leading CTE list has been stepped over.
    """
    match = _FROM_KEYWORD_RE.search(sql)
    return sql[match.start():] if match else sql


_COST_SPLIT_RESTRICTION_RE = re.compile(
    r"\bWHERE\b.*data->>'(?:crewai_cost|browser_use_cost)'\s*\)?\s*::\s*numeric\s*>\s*0",
    re.IGNORECASE | re.DOTALL)


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_cost_split_panels_exclude_runs_that_recorded_no_split(path: Path):
    if path.name in PER_RUN_DASHBOARDS:
        return  # per-run panel: a zero split for that one run is the answer
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    for sql in _sql_targets(dashboard):
        if not _COST_SPLIT_FIELD_RE.search(sql) or not _AGGREGATE_RE.search(sql):
            continue
        assert _COST_SPLIT_RESTRICTION_RE.search(_after_from(sql)), (
            f"{path.name} aggregates crewai_cost/browser_use_cost without "
            f"excluding the runs that recorded no split, so the median falls "
            f"in the zero mass: {sql[:200]}"
        )


def test_cost_split_guard_fires_on_a_select_list_filter_clause(tmp_path):
    """The cost-split guard's own mutation test, in the style of
    test_llm_traces_guard_fires_on_a_select_list_model_filter above.

    A select-list `count(*) FILTER (WHERE (data->>'crewai_cost')::numeric >
    0)` counts the split runs correctly, but the median still runs over all
    447 rows because percentile_cont carries no restriction of its own — the
    exact defect this task fixes. It also contains a literal WHERE token
    inside the FILTER clause, so a guard anchored on the WHERE token alone,
    with no bound on where in the SQL it may appear, is satisfied by that
    inner WHERE and never notices the aggregate itself is unrestricted.
    Mutates a tmp copy of cost-latency-capacity.json with exactly that shape;
    never touches the committed file.
    """
    source = DASHBOARD_DIR / "cost-latency-capacity.json"
    dashboard = json.loads(source.read_text(encoding="utf-8"))
    dashboard["panels"][0]["targets"][0]["rawSql"] = (
        "SELECT count(*) FILTER (WHERE (data->>'crewai_cost')::numeric > 0) AS runs, "
        "round((percentile_cont(0.5) WITHIN GROUP (ORDER BY (data->>'crewai_cost')::numeric))::numeric, 4) AS median_crewai_usd "
        "FROM workflow_metrics"
    )

    mutant = tmp_path / source.name
    mutant.write_text(json.dumps(dashboard), encoding="utf-8")

    with pytest.raises(AssertionError, match="without excluding"):
        test_cost_split_panels_exclude_runs_that_recorded_no_split(mutant)


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


# The per-run dashboards' whole contract is in the name: every number on the
# page belongs to the one run in the textbox. Nothing asserted that. Proven by
# mutation on 2026-08-13 — deleting `WHERE workflow_id = ${run_id:sqlstring}`
# from the coverage strip's llm_traces arm, and separately from panel 6, each
# left this entire file green. The strip would then report
# llm_model_calls = 2,829, the whole table's model-call count, under a heading
# that says what THIS run left behind; panel 6 would list all 100,907
# llm_traces rows under a title reading "Every model call" for one run. Both
# render as data rather than as breakage, which is the worst way for a debug
# dashboard to be wrong. Three aggregate guards exempt these dashboards on the
# strength of that contract (see PER_RUN_DASHBOARDS), so it has to be real.
#
# The rule is a count, not a parse: a panel must interpolate the run id at
# least once per granted table it reads. Measured on the shipped file — the
# coverage strip reads five tables through five subqueries and carries five
# restrictions, seven panels read one table and carry one, and panel 8 unions
# two tables and carries two. Counting distinct granted tables reuses
# _production_table_refs, the same reader the detachment guard and the coverage
# strip guard already trust, instead of adding a third parser that would have
# to work out which restriction belongs to which arm.
#
# What a count therefore does NOT reach: two arms on the SAME table where only
# one is scoped. No shipped panel has that shape, and closing it means pulling
# UNION arms and correlated subqueries out of a string with regexes — which is
# how a static guard starts failing correct work, the failure mode the
# duration-alias helper below already had to be walked back from.
_RUN_ID_INTERPOLATION = "${run_id:sqlstring}"


@pytest.mark.parametrize("name", sorted(PER_RUN_DASHBOARDS))
def test_every_per_run_panel_scopes_to_the_run_id(name: str):
    dashboard = json.loads((DASHBOARD_DIR / name).read_text(encoding="utf-8"))
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            sql = target.get("rawSql")
            if not sql:
                continue
            ctes = {cte.lower() for cte in _CTE_RE.findall(sql)}
            tables = _production_table_refs(sql, ctes)
            # Bounded from the first FROM onward, the same reasoning _after_from
            # was written for: an interpolation sitting in the outer SELECT list
            # labels a column, it never narrows a row set. The bound can only
            # fail open — a FROM inside a string literal starts the window
            # early, which keeps more text, never less.
            scoped = _after_from(sql).count(_RUN_ID_INTERPOLATION)
            assert scoped >= len(tables), (
                f"{name} panel {panel.get('title')!r} reads {len(tables)} "
                f"granted table(s) {sorted(tables)} but carries only {scoped} "
                f"run-id restriction(s), so at least one of them returns the "
                f"whole table on a dashboard about one run: {sql[:200]}"
            )


def test_run_id_scope_guard_fires_when_one_arm_loses_its_restriction(tmp_path):
    """Proving the guard passes on the shipped file is not the same as proving
    it REJECTS an unscoped read — and the mutant is chosen so that a weaker
    guard could not pass either test by accident.

    The coverage strip's llm_traces arm loses its WHERE clause and the other
    four arms keep theirs, so the mutated SQL still contains four
    `${run_id:sqlstring}` interpolations. A guard that merely checked the
    restriction APPEARS would see those four and stay green while the strip
    reported the whole table's 2,829 model calls as this run's. Only counting
    against the tables read catches it. Mutates a tmp copy; never touches the
    committed file.
    """
    source = DASHBOARD_DIR / "trace-one-run.json"
    dashboard = json.loads(source.read_text(encoding="utf-8"))
    clause = f"FROM llm_traces WHERE workflow_id = {_RUN_ID_INTERPOLATION}"
    mutated = 0
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            sql = target.get("rawSql") or ""
            if "AS llm_model_calls" not in sql:
                continue
            assert clause in sql, "fixture llm_traces arm not found to mutate"
            target["rawSql"] = sql.replace(clause, "FROM llm_traces", 1)
            assert target["rawSql"].count(_RUN_ID_INTERPOLATION) == 4, (
                "the mutant must keep the other four restrictions, otherwise "
                "it also fires a guard that only checks the phrase appears")
            mutated += 1
    assert mutated == 1, "fixture coverage strip not found to mutate"

    mutant_dir = tmp_path / "dashboards"
    mutant_dir.mkdir()
    (mutant_dir / source.name).write_text(json.dumps(dashboard), encoding="utf-8")

    with patch(f"{__name__}.DASHBOARD_DIR", mutant_dir):
        with pytest.raises(AssertionError, match="run-id restriction"):
            test_every_per_run_panel_scopes_to_the_run_id(source.name)


def _bench_grant_columns(sql: str) -> set[str] | None:
    """Tables granted via an ACTIVE `GRANT SELECT ON bench.<table>, ... TO
    grafana_ro` in `sql`, or None if no such grant is present. SQL line
    comments are stripped first, so a commented-out GRANT — which the naive
    regex still matches, since `--` is not part of the pattern — reads as
    absent rather than present."""
    sql = re.sub(r"--[^\n]*", "", sql)
    match = re.search(
        r"\bGRANT\s+SELECT\s+ON\s+((?:bench\.[a-z_]+\s*,?\s*)+)TO\s+grafana_ro\b",
        sql, re.IGNORECASE)
    if not match:
        return None
    return {t.strip().lower() for t in match.group(1).split(",") if t.strip()}


def _bench_usage_grant_present(sql: str) -> bool:
    """True if `sql` has an active (non-commented) `GRANT USAGE ON SCHEMA
    bench TO grafana_ro`."""
    sql = re.sub(r"--[^\n]*", "", sql)
    return bool(re.search(r"GRANT\s+USAGE\s+ON\s+SCHEMA\s+bench\s+TO\s+grafana_ro",
                          sql, re.IGNORECASE))


def test_bench_grant_is_present_and_exact():
    """A second GRANT block, matched separately from the seven-table app
    grant, so neither can silently absorb the other."""
    sql = ROLE_SCRIPT.read_text(encoding="utf-8")
    granted = _bench_grant_columns(sql)
    assert granted is not None, "create_readonly_role.sql has no GRANT SELECT on bench tables"
    assert granted == BENCH_TABLES, f"grants {sorted(granted)}"
    assert _bench_usage_grant_present(sql), "no USAGE grant on schema bench"


def test_bench_grant_guard_fires_on_a_commented_out_grant():
    """The only other thing that would catch a commented-out bench GRANT is
    test_readonly_role.py::test_grafana_ro_can_read_bench_but_not_write_it,
    which is @pytest.mark.integration and skips wherever the stack is
    absent — so in CI a commented-out grant would ship silently and Grafana
    would lose bench access. Mutates the real file's text in memory only;
    never writes to create_readonly_role.sql and never runs _apply_script,
    which would revoke live grants."""
    sql = ROLE_SCRIPT.read_text(encoding="utf-8")
    mutated = sql.replace(
        "GRANT USAGE ON SCHEMA bench TO grafana_ro;",
        "-- GRANT USAGE ON SCHEMA bench TO grafana_ro;",
    ).replace(
        "GRANT SELECT ON bench.sweeps, bench.runs TO grafana_ro;",
        "-- GRANT SELECT ON bench.sweeps, bench.runs TO grafana_ro;",
    )
    assert mutated != sql, "fixture GRANT lines not found to comment out"
    assert _bench_grant_columns(mutated) is None
    assert not _bench_usage_grant_present(mutated)


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
        app_refs = _production_table_refs(sql, ctes)
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


def _panel_follows_time_picker(panel: dict) -> bool:
    """True when moving the time picker changes what this panel shows.

    Two ways it can: a Loki query is always bounded by the picker's range, and
    a SQL query that spells $__timeFrom/$__timeTo/$__timeFilter is filtered by
    it. Everything else — including every panel reading workflow_metrics, whose
    only time column is naive and cannot be filtered honestly — ignores it.
    """
    datasource = panel.get("datasource")
    if isinstance(datasource, dict) and datasource.get("type") == "loki":
        return True
    for target in panel.get("targets", []):
        if "$__time" in (target.get("rawSql") or ""):
            return True
    return False


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_a_dashboard_no_panel_follows_hides_its_time_picker(path: Path):
    """A control that changes nothing is worse than no control.

    On these dashboards every panel is all-time — locator-reliability reads
    only workflow_metrics, and the bench dashboards are scoped by sweep, not
    by wall-clock. Grafana still rendered a time picker, so narrowing to six
    hours silently returned the same all-time numbers and the README had to
    warn operators about it in prose. Hide it instead.

    Stated as a rule rather than a filename list so it stays correct in both
    directions: add a $__timeFrom panel to one of these and the test tells you
    to unhide the picker, rather than leaving a live control hidden.
    """
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    panels = dashboard.get("panels", [])
    hidden = dashboard.get("timepicker", {}).get("hidden", False)
    if any(_panel_follows_time_picker(p) for p in panels):
        assert not hidden, (
            f"{path.name} has a panel that follows the time picker, but the "
            f"picker is hidden")
    else:
        assert hidden, (
            f"{path.name}: no panel follows the time picker, so it must set "
            f'"timepicker": {{"hidden": true}} — otherwise the control lies')


def test_isolation_guard_fires_on_a_comma_joined_production_table(tmp_path):
    """The detachment guard's own mutation test, in the style of
    test_bench_grant_guard_fires_on_a_commented_out_grant above.

    Proving _production_table_refs detects a comma-join is not the same as
    proving the SHIPPED guard uses it — the guard read _TABLE_RE directly
    before this, and that is exactly how the hole survived. Mutates a real
    bench dashboard in a tmp copy, under its own filename so BENCH_DASHBOARDS
    membership still matches; never touches the committed file.
    """
    source = DASHBOARD_DIR / "bench-weakest-now.json"
    dashboard = json.loads(source.read_text(encoding="utf-8"))
    sql = dashboard["panels"][0]["targets"][0]["rawSql"]
    mutated_sql = sql.replace("FROM bench.sweeps", "FROM bench.sweeps, workflow_metrics", 1)
    assert mutated_sql != sql, "fixture FROM clause not found to mutate"
    dashboard["panels"][0]["targets"][0]["rawSql"] = mutated_sql

    mutant = tmp_path / source.name
    mutant.write_text(json.dumps(dashboard), encoding="utf-8")

    with pytest.raises(AssertionError, match="reads production tables"):
        test_bench_and_production_dashboards_never_mix(mutant)


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


def test_aggregate_error_log_panel_reads_both_log_formats():
    """The application writes errors in two formats and they do not overlap.

    Measured over 7 days on 2026-08-12: the shipped `|= "ERROR"` substring
    filter matched 58 lines, `| json | level=~"error|critical"` matched 348,
    and both filters at once matched 0 — the sets are disjoint. structlog
    writes a lowercase `"level": "error"` field that a substring match on
    ERROR cannot see, so the shipped panel — arm B alone — saw only 58 of
    the combined 406 lines and missed the 348 that only the JSON arm
    catches; the 58 it did see are ANSI-coloured plain text from third-party
    loggers that the JSON parser cannot read.

    Both arms are load-bearing as SHAPES, which is all this test asserts.
    Re-measured 2026-08-14 over the five days Loki actually holds: arm A 534
    lines, arm B 90, and every one of arm B's 90 was a single LiteLLM message,
    `Error creating standard logging object` — written once as ANSI plain text
    and once re-emitted as JSON, so it was also 90 of arm A's 534. Both arms
    now exclude it, leaving arm A at 444 and arm B matching nothing.

    Measure this over no window longer than the retained data. The same three
    queries asked over seven days return 348 / 58 / 290 — FEWER lines, not
    more, because the range sample points fall outside what Loki holds. The
    2026-08-12 figures above were taken that way and are kept as the record of
    that day, not as a count to reproduce.

    Do not read arm B's line count as evidence it catches real errors today;
    it does not, and after the exclusion it is expected to stay empty until a
    genuine plain-text error appears. That is the case it is kept for.

    Loki's own `detected_level` is not a third option: selecting on it
    returns 0 lines on this deployment.
    """
    path = DASHBOARD_DIR / "execution-outcomes.json"
    dashboard = json.loads(path.read_text(encoding="utf-8"))

    log_panels = [
        p for p in dashboard.get("panels", [])
        if (p.get("datasource") or {}).get("type") == "loki"
    ]
    assert len(log_panels) == 1, "expected exactly one aggregate log panel"
    exprs = [t.get("expr", "") for t in log_panels[0].get("targets", [])]

    assert any("| json | level=~" in e for e in exprs), (
        "no structured-log arm: the panel cannot see structlog's lowercase "
        "level field")
    assert any('|= "ERROR"' in e for e in exprs), (
        "no substring arm: the panel cannot see third-party plain-text loggers")
    assert any("$level" in e for e in exprs), (
        "the structured arm does not use the level variable")

    variables = {
        v["name"]: v for v in dashboard.get("templating", {}).get("list", [])
    }
    assert "level" in variables, "no level variable to widen the structured arm"
    default = variables["level"].get("current", {}).get("value", "")
    assert "error" in default and "critical" in default, (
        f"level variable defaults to {default!r}; it must default to the "
        f"error classes, not to everything")


# workflow_metrics carries two TOP-LEVEL durations and they are a part and a
# whole, not two measurements of one thing:
#   workflow_duration_s = wall clock of the whole generation run — both crew
#       kickoffs, the element stage and the dryrun gate (workflow_service.py,
#       "Wall clock for the whole run. Deliberately NOT execution_time").
#   execution_time      = browser_metrics['execution_time'], the browser-use
#       element stage alone, passed straight through.
# Measured 2026-08-12 over the 15 rows carrying both non-zero: execution_time
# <= workflow_duration_s on 15 of 15, ratio 0.089-0.623, mean 0.45. An alias
# of `duration_s` on either one is therefore ambiguous by construction, so
# each must carry a word that says which span it measures.
#
# There is a THIRD duration this rule deliberately does not reach:
# crew_stage_metrics.<stage>.duration_s, the per-stage time inside a single
# crew kickoff. It is aliased plain `duration_s`/`avg_duration_s` on the
# "Cost and duration per crew stage" panel on both dashboards, with no
# ambiguity to resolve — there is only one duration field at that level, not
# two candidates a reader could confuse. _DURATION_FIELD_RE matches only
# `workflow_duration_s`/`execution_time`, so those two panels are out of
# scope by design; a future author should not read their bare `duration_s`
# alias as a gap this rule missed.
_DURATION_FIELD_RE = re.compile(
    r"data->>'(workflow_duration_s|execution_time)'", re.IGNORECASE)
_ALIAS_AFTER_RE = re.compile(r"\bAS\s+([a-z_][a-z0-9_]*)", re.IGNORECASE)
_DURATION_ALIAS_RULE = {"workflow_duration_s": "wall", "execution_time": "browser"}


_LEADING_WITH_RE = re.compile(r"\s*WITH\b", re.IGNORECASE)


def _after_leading_ctes(sql: str) -> str:
    """`sql` with a leading `WITH <name> AS (...), <name> AS (...)` list removed.

    Paren-balanced rather than regex-matched, because a CTE body contains both
    parentheses and commas of its own and no regex tells those from the ones
    that end the list. Walks from the start, closes each CTE body when depth
    returns to 0, and continues only while the next non-space character is the
    comma that introduces another CTE.

    Fails open on anything it does not recognise — no leading WITH, or an
    unbalanced statement — by handing back the whole string. For the one caller
    below that means grading more text, never less, which is the safe direction
    for a helper whose false positives fail correct panels.
    """
    if not _LEADING_WITH_RE.match(sql):
        return sql
    depth = 0
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "(":
            depth += 1
        elif char == ")" and depth > 0:
            depth -= 1
            if depth == 0:
                rest = sql[index + 1:]
                stripped = rest.lstrip()
                if stripped.startswith(","):
                    index = len(sql) - len(stripped) + 1
                    continue
                return rest
        index += 1
    return sql


def _duration_aliases(sql: str) -> list[tuple[str, str]]:
    """(field, alias) for each duration field in `sql`'s outer SELECT list.

    Two bounds, and both were paid for.

    The select-list bound is the original one: only a field being SELECTED
    needs an alias saying which duration it is. Without it, every mention of
    the field anywhere in the statement is graded as if it were a select-list
    entry — `WHERE (data->>'workflow_duration_s')::numeric > 0` returned
    ('workflow_duration_s', '') and failed a panel that selects nothing of the
    kind, and a correctly-aliased panel that merely sorts by the column it
    selects returned its real pair PLUS an empty one from the ORDER BY and
    failed too. Nothing shipped tripped either, which is the point: a guard
    that fails correct work is discovered by whoever writes the correct work,
    and their fix is to weaken the guard.

    The CTE step-over is the newer one: bounding on the first FROM alone broke
    on `WITH ids AS (SELECT ... FROM workflow_metrics) SELECT ... FROM ids`,
    where that first FROM sits inside the CTE, so the outer select list holding
    the real alias was never scanned and the guard saw nothing at all on
    mark1-runs.json. Stepping over the leading CTE list first restores the
    select-list bound without giving that back.

    Within the bounded select list, each field's alias is the nearest AS in its
    own top-level comma segment, so a neighbouring column's alias is never
    borrowed by a field that has none.
    """
    body = _after_leading_ctes(sql)
    from_keyword = _FROM_KEYWORD_RE.search(body)
    select_list = body[:from_keyword.start()] if from_keyword else body
    out = []
    for segment in _split_top_level_commas(select_list):
        for match in _DURATION_FIELD_RE.finditer(segment):
            alias = _ALIAS_AFTER_RE.search(segment[match.end():])
            out.append((match.group(1).lower(), alias.group(1).lower() if alias else ""))
    return out


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_duration_fields_are_aliased_unambiguously(path: Path):
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    for sql in _sql_targets(dashboard):
        for field, alias in _duration_aliases(sql):
            required = _DURATION_ALIAS_RULE[field]
            assert required in alias, (
                f"{path.name} selects {field} as {alias!r}, which does not "
                f"say which duration it is — the alias must contain "
                f"{required!r}: {sql[:200]}"
            )


def test_duration_alias_guard_fires_on_an_ambiguous_execution_time_alias(tmp_path):
    """A shipped panel now reads execution_time — mark1-runs.json's runs list,
    added in Phase 2. This mutation test still proves the guard rejects an
    ambiguous alias, rather than merely proving today's panels happen to
    comply. Mutates a tmp copy; never touches the committed file."""
    source = DASHBOARD_DIR / "cost-latency-capacity.json"
    dashboard = json.loads(source.read_text(encoding="utf-8"))
    dashboard["panels"][0]["targets"][0]["rawSql"] = (
        "SELECT count(*) AS runs, "
        "round((data->>'execution_time')::numeric, 1) AS duration_s "
        "FROM workflow_metrics"
    )
    mutant = tmp_path / source.name
    mutant.write_text(json.dumps(dashboard), encoding="utf-8")

    with pytest.raises(AssertionError, match="which duration it is"):
        test_duration_fields_are_aliased_unambiguously(mutant)


def test_duration_alias_guard_fires_on_an_ambiguous_alias_behind_a_cte(tmp_path):
    """_duration_aliases used to bound its search to the text before the
    first FROM. On a CTE-fronted query — `WITH ids AS (SELECT ... FROM
    workflow_metrics) SELECT ... FROM ids` — that first FROM sits inside the
    CTE, so the outer SELECT list carrying the real alias was never scanned:
    the guard saw nothing at all, not even a wrong answer, on exactly the
    shape mark1-runs.json ships. Proven: with the pre-fix implementation this
    mutant's rawSql produced `_duration_aliases(sql) == []`, so
    test_duration_fields_are_aliased_unambiguously never looked at
    `duration_s` and did not raise. Mutates a tmp copy of
    cost-latency-capacity.json with a synthetic CTE-fronted rawSql; never
    touches the committed file."""
    source = DASHBOARD_DIR / "cost-latency-capacity.json"
    dashboard = json.loads(source.read_text(encoding="utf-8"))
    dashboard["panels"][0]["targets"][0]["rawSql"] = (
        "WITH ids AS (SELECT workflow_id AS id FROM workflow_metrics) "
        "SELECT round((data->>'execution_time')::numeric, 1) AS duration_s "
        "FROM ids"
    )
    mutant = tmp_path / source.name
    mutant.write_text(json.dumps(dashboard), encoding="utf-8")

    with pytest.raises(AssertionError, match="which duration it is"):
        test_duration_fields_are_aliased_unambiguously(mutant)


class TestDurationAliasScope:
    """_duration_aliases must grade the select list and nothing else.

    The two mutation tests above prove it still REJECTS an ambiguous alias.
    These prove the other half — that it does not invent one. The helper is
    shared by both phases' dashboards, so a false positive here fails panels
    nobody in this phase wrote, and the reported alias is `''`, which reads as
    a missing alias rather than as a guard looking in the wrong place.
    """

    def test_a_where_clause_mention_is_not_a_select_list_entry(self):
        """Selects no duration field at all; the mention is a filter."""
        sql = ("SELECT count(*) AS runs FROM workflow_metrics "
               "WHERE (data->>'workflow_duration_s')::numeric > 0")
        assert _duration_aliases(sql) == []

    def test_an_order_by_mention_is_not_a_second_select_list_entry(self):
        """A correctly-aliased panel that sorts by the column it selects. The
        ORDER BY must not produce a second, alias-less pair from the one
        field."""
        sql = ("SELECT workflow_id AS run_id, "
               "round((data->>'execution_time')::numeric,1) AS browser_stage_s "
               "FROM workflow_metrics ORDER BY (data->>'execution_time')::numeric DESC")
        assert _duration_aliases(sql) == [("execution_time", "browser_stage_s")]

    def test_the_outer_select_list_behind_a_cte_is_still_scanned(self):
        """The bug the select-list bound must not bring back: the first FROM
        sits inside the CTE, so a naive prefix bound scans the CTE body and
        returns nothing for the outer select list."""
        sql = ("WITH ids AS (SELECT workflow_id AS id FROM workflow_metrics) "
               "SELECT round((data->>'execution_time')::numeric, 1) AS duration_s "
               "FROM ids")
        assert _duration_aliases(sql) == [("execution_time", "duration_s")]

    def test_a_list_of_several_ctes_is_stepped_over_whole(self):
        """The comma between two CTEs is a top-level comma, so the step-over
        has to keep going past it rather than stopping at the first `)`."""
        sql = ("WITH a AS (SELECT run_id FROM test_runs), "
               "b AS (SELECT workflow_id FROM workflow_metrics) "
               "SELECT (data->>'execution_time')::numeric AS browser_s FROM a")
        assert _duration_aliases(sql) == [("execution_time", "browser_s")]


# The runs front door is the only inventory of what this install has ever run,
# and the three app tables it unions do not nest. Measured 2026-08-13 over the
# 528 UUID-shaped run ids: 415 have a workflow_metrics row, 45 have a test_runs
# row, 123 have an execution_records row, and 113 have no workflow_metrics row
# at all. Drop any one branch and a whole class of run stops being listed, with
# nothing on screen to say so — the one failure a front door cannot afford.
#
# A note for whoever hits this guard's neighbours: every branch of that union
# carries a WHERE clause, and it is load-bearing beyond the filtering it does.
# _FROM_CLAUSE_RE captures non-greedily up to the first WHERE/GROUP BY/ORDER
# BY/JOIN/LIMIT, so a branch without one lets the capture run past the CTE and
# swallow the outer SELECT list, whose commas sit at paren-depth 0. Measured:
# on the table panel, whose bare column aliases (`tr`, `m`) then read as
# tables, both test_aggregate_dashboards_use_no_inner_join and
# test_every_sql_target_queries_only_granted_tables fail — `er` never
# surfaces, because every er reference sits inside a coalesce(...) call that
# _FUNCTION_CALL_RE excludes. The stat panel's SELECT list is all count(...)
# calls, so neither guard fires there; its protection is the mutation test
# below, which drops a whole branch rather than a WHERE. Fail-closed on one
# panel, narrower on the other.
_RUNS_LIST_SOURCES = ("test_runs", "workflow_metrics", "execution_records")


def test_runs_list_unions_every_run_id_source():
    path = DASHBOARD_DIR / "mark1-runs.json"
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    sqls = _sql_targets(dashboard)
    assert sqls, "mark1-runs.json has no SQL target"
    for sql in sqls:
        for table in _RUNS_LIST_SOURCES:
            assert re.search(rf"\bFROM\s+{table}\b", sql, re.IGNORECASE), (
                f"mark1-runs.json reads no {table}, so every run that exists "
                f"only in that table is invisible on the one dashboard whose "
                f"job is to list every run: {sql[:200]}"
            )


def test_runs_list_guard_fires_when_a_union_branch_is_dropped(tmp_path):
    """Proving the guard passes on the shipped file is not the same as proving
    it REJECTS a query missing a branch. Mutates a tmp copy that drops the
    execution_records arm — the shape a future author produces by 'simplifying'
    the union — and asserts the guard notices. Never touches the committed file.
    """
    branch = (
        " UNION SELECT workflow_id FROM execution_records WHERE workflow_id ~ "
        "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"
    )
    source = DASHBOARD_DIR / "mark1-runs.json"
    dashboard = json.loads(source.read_text(encoding="utf-8"))
    removed = 0
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            if branch in (target.get("rawSql") or ""):
                target["rawSql"] = target["rawSql"].replace(branch, "", 1)
                removed += 1
    assert removed, "fixture union branch not found to remove"
    mutant_dir = tmp_path / "dashboards"
    mutant_dir.mkdir()
    (mutant_dir / source.name).write_text(json.dumps(dashboard), encoding="utf-8")

    with patch(f"{__name__}.DASHBOARD_DIR", mutant_dir):
        with pytest.raises(AssertionError, match="reads no execution_records"):
            test_runs_list_unions_every_run_id_source()


def test_trace_coverage_strip_counts_every_source_the_dashboard_reads():
    """An empty panel must read as 'no record', not as 'broken'.

    Measured 2026-08-13: 538 distinct run ids exist across the three app
    tables and only 158 also have rows in llm_traces — 380 have none. A run
    that resolves in every panel on this dashboard is the exception, so the
    strip at the top exists to say up front which sources hold anything for
    this id.

    The rule with teeth is not 'a strip exists' — it is that the strip must
    cover every source a panel below it reads. Add a panel on a new table and
    forget the strip, and its blank output is unexplained again, which is the
    exact defect the strip was added to fix. Asserted one-directional: the
    strip may count more sources than the panels read (learning_metrics has no
    panel here, and 'learning recorded nothing for this run' is worth
    knowing), never fewer.
    """
    path = DASHBOARD_DIR / "trace-one-run.json"
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    panels = dashboard["panels"]

    strip_sql = " ".join(
        t["rawSql"] for t in panels[0].get("targets", []) if t.get("rawSql"))
    assert strip_sql, "the first panel on the trace dashboard runs no SQL"

    read_below = set()
    for panel in panels[1:]:
        for target in panel.get("targets", []):
            read_below |= _production_table_refs(target.get("rawSql") or "", set())

    missing = {
        table for table in read_below
        if not re.search(rf"\bFROM\s+{table}\b", strip_sql, re.IGNORECASE)
    }
    assert not missing, (
        f"the coverage strip does not count {sorted(missing)}, which a panel "
        f"below it reads — an empty panel on that source has nothing above it "
        f"saying whether the run has a row there at all"
    )


def test_coverage_strip_guard_fires_when_a_source_is_dropped(tmp_path):
    """Proving the guard passes on the shipped file is not the same as proving
    it REJECTS a strip that has fallen behind the panels. Mutates a tmp copy
    with the llm_traces subquery removed; never touches the committed file."""
    source = DASHBOARD_DIR / "trace-one-run.json"
    dashboard = json.loads(source.read_text(encoding="utf-8"))
    target = dashboard["panels"][0]["targets"][0]
    # Split on the subquery separator and drop the one arm, rather than
    # regex-surgery on a string full of parentheses and ${...} interpolations.
    parts = target["rawSql"].split(", (SELECT ")
    kept = [p for p in parts if "llm_traces" not in p]
    assert len(kept) == len(parts) - 1, "fixture subquery not found to remove"
    target["rawSql"] = ", (SELECT ".join(kept)
    mutant_dir = tmp_path / "dashboards"
    mutant_dir.mkdir()
    (mutant_dir / source.name).write_text(json.dumps(dashboard), encoding="utf-8")

    with patch(f"{__name__}.DASHBOARD_DIR", mutant_dir):
        with pytest.raises(AssertionError, match=r"does not count \['llm_traces'\]"):
            test_trace_coverage_strip_counts_every_source_the_dashboard_reads()


def test_per_run_log_panel_keeps_lines_that_carry_no_level_field():
    """Two independent ways to silently lose most of a run's log lines.

    Measured over 7 days on 2026-08-13 across both services: `| json` returns
    all 36,289 lines, but 25,784 of them carry __error__="JSONParserErr" —
    third-party libraries write ANSI-coloured plain text, not structlog JSON,
    and `| json` keeps a line it cannot parse rather than dropping it.

    1. `line_format "{{.level}} {{.logger}} :: {{.event}}"` — the template the
       design specified — has no fields to substitute on those 25,784 lines,
       so it renders each as the literal string "  :: " and the content is
       gone. Verified live. The `{{ if .event }}...{{ else }}{{ __line__ }}
       {{ end }}` form passes them through byte for byte.
    2. A label filter cannot match a label that is absent, so `level=~".+"`
       returns 10,452 of the 36,289 while `level=~".*"` returns all of them —
       `.*` matches the empty string and `.+` does not.

    Both traps are invisible on the run this panel was designed against: all
    280 of its lines parse as JSON. Hence a static guard rather than a
    measurement someone repeats by hand.

    execution-outcomes.json uses `.+` for its own "every structured line"
    option and that is CORRECT there — that panel ships a second, substring
    arm which catches the plain-text class, and the two were measured
    disjoint. This panel is single-arm, so the default must be `.*`.
    """
    path = DASHBOARD_DIR / "trace-one-run.json"
    dashboard = json.loads(path.read_text(encoding="utf-8"))

    log_panels = [
        p for p in dashboard["panels"]
        if (p.get("datasource") or {}).get("type") == "loki"
    ]
    assert len(log_panels) == 1, "expected exactly one per-run log panel"
    exprs = [t.get("expr", "") for t in log_panels[0].get("targets", [])]

    assert any("__line__" in e for e in exprs), (
        "the log panel's line_format has no {{ __line__ }} fallback, so every "
        "line the JSON parser cannot read renders as empty separators")
    assert any("line_format" in e for e in exprs), "no line_format at all"

    variables = {v["name"]: v for v in dashboard["templating"]["list"]}
    assert "level" in variables, "no level variable on the per-run log panel"
    level = variables["level"]
    assert level.get("current", {}).get("value") == ".*", (
        f"the level variable defaults to "
        f"{level.get('current', {}).get('value')!r}; it must default to '.*' "
        f"— '.+' drops every line that carries no level field, which is 71% "
        f"of this stream")
    widest = [o["value"] for o in level["options"] if o["value"] in (".*", ".+")]
    assert widest == [".*"], (
        f"the widest level option is {widest}; '.+' is not a 'show everything' "
        f"option on a single-arm panel")

    assert any("| json" in e for e in exprs), (
        "no `| json` stage in the log panel's expr, so `.event`/`.level`/"
        "`.logger` never populate and every line falls to the `__line__` "
        "fallback — the panel silently reverts to raw JSON")
    assert any("$level" in e for e in exprs), (
        "the log panel's expr never consumes $level, so the Log level "
        "dropdown filters nothing and is inert UI")


def test_log_panel_guard_fires_on_the_unguarded_line_format(tmp_path):
    """Proving the guard passes on the shipped file is not the same as proving
    it REJECTS the template the design originally specified — which is the
    exact string a future author would paste back in from the spec. Mutates a
    tmp copy with the line_format half reverted to the unguarded template;
    the level variable is untouched here — see
    test_log_panel_guard_fires_on_a_level_default_that_drops_lines for the
    level-default mutant. Never touches the committed file."""
    source = DASHBOARD_DIR / "trace-one-run.json"
    dashboard = json.loads(source.read_text(encoding="utf-8"))
    for panel in dashboard["panels"]:
        if (panel.get("datasource") or {}).get("type") != "loki":
            continue
        for target in panel.get("targets", []):
            target["expr"] = (
                '{service=~"fastapi|browser-service"} |= "$run_id" | json '
                '| level=~"$level" '
                '| line_format "{{.level}} {{.logger}} :: {{.event}}"'
            )
    mutant_dir = tmp_path / "dashboards"
    mutant_dir.mkdir()
    (mutant_dir / source.name).write_text(json.dumps(dashboard), encoding="utf-8")

    with patch(f"{__name__}.DASHBOARD_DIR", mutant_dir):
        with pytest.raises(AssertionError, match="__line__"):
            test_per_run_log_panel_keeps_lines_that_carry_no_level_field()


def test_log_panel_guard_fires_on_a_level_default_that_drops_lines(tmp_path):
    """The line_format half has its own mutant above; this one anchors the
    level-default assertion instead. `expr` is left byte-for-byte untouched
    here, so the `__line__` assertion cannot be what fires — only the level
    variable's default and its matching 'every line' option move, from '.*'
    to '.+'. That is the realistic regression: an author copies
    execution-outcomes.json's '.+' "every structured line" pattern onto this
    single-arm panel, where '.*' is the only value that shows every line and
    '.+' silently drops every line with no level field. Mutates a tmp copy;
    never touches the committed file."""
    source = DASHBOARD_DIR / "trace-one-run.json"
    dashboard = json.loads(source.read_text(encoding="utf-8"))
    variables = {v["name"]: v for v in dashboard["templating"]["list"]}
    level = variables["level"]
    assert level["current"]["value"] == ".*", "fixture level default not found to mutate"
    level["current"]["value"] = ".+"
    matched = 0
    for option in level["options"]:
        if option["value"] == ".*":
            option["value"] = ".+"
            matched += 1
    assert matched == 1, "fixture 'every line' option not found to mutate"

    mutant_dir = tmp_path / "dashboards"
    mutant_dir.mkdir()
    (mutant_dir / source.name).write_text(json.dumps(dashboard), encoding="utf-8")

    with patch(f"{__name__}.DASHBOARD_DIR", mutant_dir):
        with pytest.raises(AssertionError, match=r"must default to"):
            test_per_run_log_panel_keeps_lines_that_carry_no_level_field()


# A data link is a promise that clicking it lands somewhere real, and nothing
# else in this repo checks it: Grafana renders a dead link exactly like a live
# one and only says so after the click. Both halves are guarded — the target
# must exist, and the id it carries must be whole, because the standing rule
# that run ids are never truncated has to survive the trip through a URL as
# well as through SQL.
_DASHBOARD_LINK_RE = re.compile(r"^/d/([a-z0-9-]+)")
_RUN_ID_PARAM_RE = re.compile(r"var-run_id=([^&]+)")


def _dashboard_uids() -> set[str]:
    return {json.loads(p.read_text(encoding="utf-8"))["uid"] for p in _dashboards()}


def _link_urls(dashboard: dict) -> list[str]:
    """Every link URL in a dashboard, from all three places Grafana keeps them.

    Dashboard-level `links` are the nav bar. Panel `fieldConfig.defaults.links`
    apply to every field. Panel `fieldConfig.overrides[].properties[]` with
    id == "links" apply to one named field, which is the form a per-row link on
    a single column takes — and the form a check that only read `defaults`
    would miss entirely.
    """
    urls = [link.get("url", "") for link in dashboard.get("links", [])]
    for panel in dashboard.get("panels", []):
        config = panel.get("fieldConfig", {})
        urls += [
            link.get("url", "")
            for link in config.get("defaults", {}).get("links", [])
        ]
        for override in config.get("overrides", []):
            for prop in override.get("properties", []):
                if prop.get("id") == "links":
                    urls += [link.get("url", "") for link in prop.get("value", [])]
    return [u for u in urls if u]


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_every_data_link_targets_a_dashboard_that_exists(path: Path):
    known = _dashboard_uids()
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    for url in _link_urls(dashboard):
        match = _DASHBOARD_LINK_RE.match(url)
        assert match, f"{path.name} has a link that is not a /d/<uid> path: {url}"
        assert match.group(1) in known, (
            f"{path.name} links to dashboard uid {match.group(1)!r}, which no "
            f"committed dashboard declares — the link renders normally and "
            f"dead-ends on click: {url}")


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_data_links_pass_the_whole_run_id(path: Path):
    """${__value.raw} is the only form that carries the id untouched.

    ${__value.text} hands over the DISPLAYED value, so a field with a display
    override — a unit, a decimal count, a value mapping — sends whatever the
    cell renders rather than the id, and Grafana gives no warning. That is the
    standing never-truncate rule reaching one step past the SQL it was written
    for.
    """
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    for url in _link_urls(dashboard):
        for value in _RUN_ID_PARAM_RE.findall(url):
            assert value == "${__value.raw}", (
                f"{path.name} passes var-run_id={value} — only "
                f"${{__value.raw}} carries the id untouched: {url}")


def test_link_guard_fires_on_a_dead_uid(tmp_path):
    """Mutates a tmp copy of the runs dashboard so its row link points at a uid
    no dashboard declares; never touches the committed file."""
    source = DASHBOARD_DIR / "mark1-runs.json"
    dashboard = json.loads(source.read_text(encoding="utf-8"))
    for panel in dashboard["panels"]:
        for override in panel.get("fieldConfig", {}).get("overrides", []):
            for prop in override.get("properties", []):
                if prop.get("id") == "links":
                    prop["value"][0]["url"] = "/d/mark1-trace-runs?var-run_id=${__value.raw}"
    mutant = tmp_path / source.name
    mutant.write_text(json.dumps(dashboard), encoding="utf-8")

    with pytest.raises(AssertionError, match="which no committed dashboard declares"):
        test_every_data_link_targets_a_dashboard_that_exists(mutant)


def test_link_guard_fires_on_a_display_formatted_run_id(tmp_path):
    """The plausible mistake is ${__value.text}, not a hand-truncated id —
    Grafana's own docs list it alongside .raw and it works on an unformatted
    field, so it survives review and breaks later when someone adds a display
    override. Mutates a tmp copy; never touches the committed file."""
    source = DASHBOARD_DIR / "mark1-runs.json"
    dashboard = json.loads(source.read_text(encoding="utf-8"))
    for panel in dashboard["panels"]:
        for override in panel.get("fieldConfig", {}).get("overrides", []):
            for prop in override.get("properties", []):
                if prop.get("id") == "links":
                    prop["value"][0]["url"] = (
                        prop["value"][0]["url"].replace("${__value.raw}", "${__value.text}"))
    mutant = tmp_path / source.name
    mutant.write_text(json.dumps(dashboard), encoding="utf-8")

    with pytest.raises(AssertionError, match=r"passes var-run_id="):
        test_data_links_pass_the_whole_run_id(mutant)


def _bench_dashboard_uids() -> set[str]:
    """The uids declared by the bench dashboards, resolved from the files.

    _dashboard_uids() deliberately globs every committed dashboard so a link
    target is checked against everything that exists. That makes it useless for
    the detachment question: a bench uid is a real uid, so a production
    dashboard linking to one passes the link-target guard exactly like a
    correct link does.
    """
    return {
        json.loads(path.read_text(encoding="utf-8"))["uid"]
        for path in _dashboards() if path.name in BENCH_DASHBOARDS
    }


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_no_link_crosses_the_bench_boundary(path: Path):
    """Detachment has to survive the nav bar, not only the SQL.

    test_bench_and_production_dashboards_never_mix keeps bench rows out of a
    production QUERY. A link mixes nothing but it still routes an operator
    reading production numbers straight into the bench corpus, one click from
    the dashboard they were trusting — and the two are not comparable
    populations. Proven open on 2026-08-13: pointing cost-latency-capacity's
    nav link at /d/bench-weakest-now left this entire file green.

    Both directions, because both are reachable. A production dashboard may
    not name a bench uid. A bench dashboard may declare no links at all —
    stricter than the mirror rule, and deliberately so: everything a bench
    dashboard could usefully link to is production, so there is nothing for an
    allowed list to hold, and "no links" is a rule with no edge cases.
    """
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    urls = _link_urls(dashboard)
    if path.name in BENCH_DASHBOARDS:
        assert not urls, (
            f"{path.name} is a bench dashboard and declares links: {urls} — a "
            f"bench board may link nowhere, since everything on the other side "
            f"of a link is production")
        return
    bench_uids = _bench_dashboard_uids()
    for url in urls:
        match = _DASHBOARD_LINK_RE.match(url)
        assert not match or match.group(1) not in bench_uids, (
            f"{path.name} is a production dashboard and links to bench "
            f"dashboard uid {match.group(1)!r} — the link resolves, which is "
            f"why nothing else catches it, and lands the reader in the bench "
            f"corpus: {url}")


def test_bench_boundary_link_guard_fires_on_a_link_into_the_bench_corpus(tmp_path):
    """The mutation the reviewer used to prove the hole, kept as a test.

    Mutates cost-latency-capacity.json's nav link to point at the bench board.
    The mutant is chosen precisely because it is a LIVE uid:
    test_every_data_link_targets_a_dashboard_that_exists passes on it, so this
    guard is the only thing between the mutation and a green suite. Mutates a
    tmp copy; never touches the committed file.
    """
    source = DASHBOARD_DIR / "cost-latency-capacity.json"
    dashboard = json.loads(source.read_text(encoding="utf-8"))
    bench_uid = sorted(_bench_dashboard_uids())[0]
    assert dashboard["links"], "fixture nav link not found to mutate"
    dashboard["links"][0]["url"] = f"/d/{bench_uid}"
    mutant = tmp_path / source.name
    mutant.write_text(json.dumps(dashboard), encoding="utf-8")

    # The link-target guard cannot see this: the uid it points at is real.
    test_every_data_link_targets_a_dashboard_that_exists(mutant)
    with pytest.raises(AssertionError, match="links to bench dashboard uid"):
        test_no_link_crosses_the_bench_boundary(mutant)


# The seven bench sub-stage columns look like seven disjoint slices of the
# identify stage. They are not. Measured 2026-08-13 over the 1,104 runs that
# carry them, stacking all seven sums to 1.947x the identify_s they decompose,
# because they are TWO overlapping views of the same wall clock — one timed
# from the caller, one from inside the agent — and each reconstructs identify_s
# on its own:
#
#   caller lane  submit_s + queue_s + poll_wait_s + postprocess_s
#                mean |error| 0.583 s, max 5.594 s
#   agent lane   session_setup_s + agent_setup_s + agent_run_s
#                mean |error| 0.647 s, max 2.999 s
#
# poll_wait_s (avg 28.07 s) is the caller blocking WHILE agent_run_s (avg
# 26.75 s) runs. Adding them counts the same seconds twice and still produces a
# plausible-looking number, which is exactly why this is a guard and not a
# comment in the SQL. Selecting both lanes is correct and is what the shipped
# panel does; ADDING across them never is.
# The two lane ALIASES sit in these sets beside the raw columns they sum.
# Panel 2 already emits caller_lane_s and agent_lane_s as named columns, so
# wrapping it in a CTE and adding the two is one obvious "simplification" — and
# with only raw column names listed, `SELECT caller_lane_s + agent_lane_s FROM
# lanes` reproduces the 1.947x stack while every guard in this file stays green.
_CALLER_LANE = frozenset({"submit_s", "queue_s", "poll_wait_s", "postprocess_s",
                          "caller_lane_s"})
_AGENT_LANE = frozenset({"session_setup_s", "agent_setup_s", "agent_run_s",
                         "agent_lane_s"})

# A chain is read one select-list ENTRY at a time: _select_list_entries cuts
# the query at every comma outside all parentheses, and the patterns below run
# inside each piece, so no match can span two entries. Both of this guard's
# comma defects were the SAME crossing — the wrapped term's argument tail
# running past a comma with no paren open — and they differ only in where it
# lands relative to the chain. Measured on c6028c4:
#
#   before it, and the guard goes SILENT. In `SELECT sweep_name, poll_wait_s +
#   coalesce(agent_run_s, 0)` the match starts at the NEIGHBOUR: `sweep_name`
#   takes `, poll_wait_s` as its tail, so the chain's head term is `sweep_name,
#   poll_wait_s`, _chain_term_column cuts it back to `sweep_name`, and the
#   caller lane is gone from the term set.
#
#   after it, and the guard FIRES on correct SQL. In `SELECT submit_s +
#   queue_s, session_setup_s + agent_setup_s` the tail of `queue_s` takes
#   `, session_setup_s` and the chain then keeps going into the next entry,
#   picking up `agent_setup_s`. A false accusation on the shape panel 2
#   actually computes, and the kind of finding that argues a future author
#   into weakening a guard.
#
# Measured 2026-08-14 on the sixteen shapes this round was specified against:
# 12 fire, 4 stay silent, and all 9 shipped dashboards stay silent.
#
# Depth is the whole trick, and it is why this is a scan and not a regex. The
# comma in `x, y` and the comma in `coalesce(x, 0)` are the same character;
# only the parenthesis balance to their left says which is an entry separator
# and which is an argument separator. Cutting at an argument comma would break
# `round(avg(a) + avg(b), 2)` in half and lose that chain outright. Cutting at
# a depth-0 comma can never break a real chain: a `+` chain is one expression,
# and one expression carries no comma at its own depth.
#
# TWO patterns then read each entry and the guard grades the union. They are
# NOT halves of one job — an earlier version of this comment said so, and the
# split is what made that false. Measured 2026-08-14, one pattern at a time
# over the same sixteen shapes: the WRAPPED pattern holds all sixteen alone;
# the BARE pattern holds nine — the five cross-lane shapes it fires on are five
# the wrapped pattern fires on too, plus the four that must stay silent. So
# neither is redundant, but not for the reason that sentence gave: each is
# load-bearing on classes the sixteen do not contain.
#
#   the WRAPPED one is the only one that reads a term behind a function call.
#   `round(avg(r.poll_wait_s) + avg(r.agent_run_s), 2)` and
#   `sum(r.postprocess_s) + sum(r.session_setup_s)` double-count exactly as a
#   bare stack does, and the bare pattern finds no chain at all in either.
#   Same lesson as _TRUNCATE_CALL_RE above: wrapped and aliased spellings are
#   ordinary SQL. So a term there is any number of wrapper calls, then the
#   column with an optional table alias, then whatever closing parens and extra
#   arguments those wrappers bring — and that trailing
#   `(?:\s*(?:,\s*ident|\)))*` is what lets one chain span
#   `sum(coalesce(a, 0)) + sum(coalesce(b, 0))`.
#
#   the BARE one is the only one that survives two spellings where that tail
#   still misreads the terms, both measured 2026-08-14 and neither reachable by
#   the split, since neither comma is at depth 0. A THREE-part qualified name:
#   in `bench.runs.poll_wait_s + bench.runs.agent_run_s` the wrapped term takes
#   at most one dot, so it matches `runs.poll_wait_s + bench.runs` and grades
#   {runs, poll_wait_s} — one lane, silent. And a chain passed as a function's
#   second argument: in `round(sweep_id, poll_wait_s + agent_run_s)` the tail
#   swallows `, poll_wait_s`, the cut reduces that term to `sweep_id`, and it
#   grades {sweep_id, agent_run_s} — one lane, silent. The bare pattern reads
#   the real pair in both. No shipped panel spells a column either way today,
#   and across the 9 dashboards the bare pattern finds no chain the wrapped one
#   has not already found, so it adds coverage without adding surface.
#
# What NEITHER pattern catches, re-verified 2026-08-14 against the code below:
# a lane column renamed on its way out of a CTE and added under the new name —
# `WITH a AS (SELECT caller_lane_s AS x ...), b AS (SELECT x + agent_lane_s
# ...)` is silent. The text says `x`, and only following that alias back
# through the CTE list would say otherwise. No text guard reaches it; closing
# it means parsing SQL. That list was written as though it were exhaustive and
# it was not: the grouping-paren shapes below were uncaught on a34d28c while it
# claimed to enumerate everything that got through. Read it as the cases known
# to be open, never as proof that the rest are closed.
#
# The leading run is spelled as an alternation — a wrapper CALL `avg(` or a
# bare GROUPING paren `(` — because both open a term the same way and only the
# first was admitted before. With the identifier mandatory,
# `caller_lane_s + (agent_lane_s)` produced no chain AT ALL and the guard went
# silent on it; measured 2026-08-14, three shapes in the list above.
# Written as an alternation rather than `(?:(?:ident\s*)?\(\s*)*`: both forms
# are correct and grade the same sixteen shapes, but an optional group inside a
# repeat backtracks harder — measured 139 ms against 122 ms on a pathological
# `avg(a` x400 input. Both patterns here were already quadratic in input length
# before this change and remain so; the shipped dashboards run in microseconds.
_CHAIN_TERM = (
    r"(?:[a-z_][a-z0-9_]*\s*\(\s*|\(\s*)*"
    r"[a-z_][a-z0-9_]*(?:\s*\.\s*[a-z_][a-z0-9_]*)?"
    r"(?:\s*(?:,\s*[a-z0-9_.']+|\)))*"
)
_ADDITIVE_CHAIN_RE = re.compile(
    rf"{_CHAIN_TERM}(?:\s*\+\s*{_CHAIN_TERM})+", re.IGNORECASE)
# Bare terms only: a dotted name, optionally wrapped in grouping parens, and
# nothing else. No wrapper CALL and no argument tail, so a match still reaches
# past no comma at any depth — which is what leaves it reading the true terms
# in the two spellings above. The parens are balanced-agnostic on purpose: this
# pattern grades identifiers, and a stray paren cannot invent one.
_BARE_ADDITIVE_CHAIN_RE = re.compile(
    r"\(*\s*[a-z_][a-z0-9_.]*\s*\)*(?:\s*\+\s*\(*\s*[a-z_][a-z0-9_.]*\s*\)*)+",
    re.IGNORECASE)
_IDENTIFIER_RE = re.compile(r"[a-z_][a-z0-9_]*", re.IGNORECASE)


def _select_list_entries(sql: str) -> list[str]:
    """`sql` cut at every comma that sits outside all parentheses.

    A comma at depth 0 ends one select-list entry and starts the next; a comma
    inside `round(..., 2)` or `coalesce(x, 0)` separates arguments of one
    expression and must not cut. Depth is tracked by scanning, which is why a
    regex could not do this: the two commas are the same character and only the
    parenthesis balance to their left tells them apart.
    """
    out, depth, start = [], 0, 0
    for i, ch in enumerate(sql):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(sql[start:i])
            start = i + 1
    out.append(sql[start:])
    return out


def _additive_chains(sql: str) -> list[str]:
    """Every additive chain in `sql`, read one select-list entry at a time.

    Reading per entry is what makes a chain unable to span two of them, and
    that is load-bearing in BOTH directions: it is why a chain sitting after a
    leading column is seen at all, and why two single-lane sums standing side
    by side are not read as one cross-lane chain.
    """
    out = []
    for entry in _select_list_entries(sql):
        out.extend(_ADDITIVE_CHAIN_RE.findall(entry))
        out.extend(_BARE_ADDITIVE_CHAIN_RE.findall(entry))
    return out


def _chain_term_column(term: str) -> str:
    """The column name a chain term reduces to, wrapper calls and alias stripped.

    `r.poll_wait_s`, `avg(r.poll_wait_s)` and `sum(coalesce(r.poll_wait_s, 0))`
    all reduce to `poll_wait_s`. The wrapper names and the table alias are
    identifiers too, but the column is always the last identifier before the
    term's first comma.

    The cut at that first comma is not purely protective, and reading it as
    though it were is what hid a regression for two rounds. On a term that
    swallowed a LEADING neighbour it discards the real column and returns the
    neighbour: `sweep_name, poll_wait_s` reduces to `sweep_name`. Splitting the
    query into select-list entries first is what stops that mattering for the
    entry-separator comma — no such neighbour is in the term any more. Inside
    one entry an argument comma can still produce it, which is one of the two
    classes the bare pattern is kept for.
    """
    identifiers = _IDENTIFIER_RE.findall(term.split(",")[0])
    return identifiers[-1].lower() if identifiers else ""


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_no_panel_adds_both_identify_substage_lanes(path: Path):
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    for sql in _sql_targets(dashboard):
        for chain in _additive_chains(sql):
            terms = {_chain_term_column(term) for term in chain.split("+")}
            assert not ((terms & _CALLER_LANE) and (terms & _AGENT_LANE)), (
                f"{path.name} adds a caller-lane sub-stage to an agent-lane one, "
                f"which double-counts the seconds they overlap on — measured at "
                f"1.947x the real identify_s across all seven: {chain.strip()}"
            )


def test_substage_lane_guard_fires_on_a_seven_column_stack(tmp_path):
    """Proving the guard passes on the shipped file is not the same as proving
    it REJECTS the stack. Mutates a tmp copy of bench-time.json so the two lane
    expressions become one seven-column sum — the exact shape a future author
    produces by 'simplifying' the panel. Never touches the committed file.
    """
    source = DASHBOARD_DIR / "bench-time.json"
    dashboard = json.loads(source.read_text(encoding="utf-8"))
    caller = "r.submit_s + r.queue_s + r.poll_wait_s + r.postprocess_s"
    agent = "r.session_setup_s + r.agent_setup_s + r.agent_run_s"
    mutated = 0
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            sql = target.get("rawSql") or ""
            if caller in sql and agent in sql:
                target["rawSql"] = sql.replace(caller, caller + " + " + agent, 1)
                mutated += 1
    assert mutated == 1, "fixture lane expressions not found to merge"

    mutant = tmp_path / source.name
    mutant.write_text(json.dumps(dashboard), encoding="utf-8")

    with pytest.raises(AssertionError, match="double-counts the seconds"):
        test_no_panel_adds_both_identify_substage_lanes(mutant)


# Nine shapes that compute the same double-counted stack, from three fix rounds.
# Each is asserted to fire on the two lane columns it actually names, not merely
# to raise: the mutants carry legitimate single-lane chains too, so matching the
# message alone would not prove which chain tripped it.
#
# Which part of the guard each one needs — measured 2026-08-14 against the
# SHIPPED code by removing one part at a time, because "the round that added a
# case is what catches it" keeps turning out to be false:
#
#   1, 2     the WRAPPED pattern. The bare pattern finds no chain at all in
#            either — verified, both return [].
#   3, 6, 9  the two lane ALIASES in the frozensets above. All three name only
#            caller_lane_s and agent_lane_s, so with the pre-round-1 sets they
#            are silent whichever patterns run — which corrects this comment's
#            previous reading, where case 6 was credited to the bare pattern
#            alone.
#   4, 5, 6  nothing this round added: measured firing on c6028c4 as well, and
#            on the shipped code EITHER pattern alone catches all three. They
#            are kept as the spellings that first exposed the leading-comma
#            bug, not because a part of the guard rests on them.
#   7, 8, 9  the depth-0 SPLIT, and only it. All three put a column ahead of a
#            sum whose second term is wrapped, and on c6028c4 both patterns
#            missed for DIFFERENT reasons — measured on case 7: the wrapped one
#            matched `sweep_name, poll_wait_s + coalesce(agent_run_s, 0)` and
#            graded {sweep_name, agent_run_s}, losing the caller lane to the
#            cut, while the bare one stopped at the wrapper name and matched
#            `poll_wait_s + coalesce`, never reaching the agent lane. Two
#            readings, one root cause, and the split is what removes it.
#
# Cases 6 and 9 are the likeliest of the nine. Panel 2 emits caller_lane_s and
# agent_lane_s as named columns AND groups by sweep, so lifting it into a CTE
# and adding the two reads as a tidy-up rather than as a measurement error — it
# never mentions a raw sub-stage column, and the GROUP BY guarantees sweep_name
# sits ahead of the sum. Case 9 is that same shape with one `coalesce` on the
# second term — one keystroke from case 6, and silent until this round. Case 3
# is their sum-first cousin, kept because it is the spelling round 1 measured.
_LANE_BYPASS_SHAPES = [
    pytest.param(
        "SELECT round(avg(r.poll_wait_s) + avg(r.agent_run_s), 2) AS identify_s "
        "FROM bench.runs r",
        ("poll_wait_s", "agent_run_s"),
        id="an-aggregate-around-each-term",
    ),
    pytest.param(
        "SELECT sum(r.postprocess_s) + sum(r.session_setup_s) AS identify_s "
        "FROM bench.runs r",
        ("postprocess_s", "session_setup_s"),
        id="a-sum-around-each-term",
    ),
    pytest.param(
        "WITH lanes AS (SELECT "
        "avg(r.submit_s + r.queue_s + r.poll_wait_s + r.postprocess_s) AS caller_lane_s, "
        "avg(r.session_setup_s + r.agent_setup_s + r.agent_run_s) AS agent_lane_s "
        "FROM bench.runs r) "
        "SELECT caller_lane_s + agent_lane_s AS identify_s FROM lanes",
        ("caller_lane_s", "agent_lane_s"),
        id="the-two-lane-aliases-behind-a-cte",
    ),
    pytest.param(
        "SELECT sweep_name, r.poll_wait_s + r.agent_run_s AS identify_s "
        "FROM bench.runs r",
        ("poll_wait_s", "agent_run_s"),
        id="a-bare-chain-that-is-not-first-in-the-select-list",
    ),
    pytest.param(
        "SELECT identify_s, poll_wait_s + agent_run_s FROM bench.runs",
        ("poll_wait_s", "agent_run_s"),
        id="an-unaliased-bare-chain-after-another-column",
    ),
    pytest.param(
        "WITH lanes AS (SELECT r.sweep_name, "
        "avg(r.submit_s + r.queue_s + r.poll_wait_s + r.postprocess_s) AS caller_lane_s, "
        "avg(r.session_setup_s + r.agent_setup_s + r.agent_run_s) AS agent_lane_s "
        "FROM bench.runs r GROUP BY 1) "
        "SELECT sweep_name, caller_lane_s + agent_lane_s AS identify_s FROM lanes",
        ("caller_lane_s", "agent_lane_s"),
        id="the-two-lane-aliases-behind-a-cte-grouped-by-sweep",
    ),
    pytest.param(
        "SELECT sweep_name, poll_wait_s + coalesce(agent_run_s, 0) AS x "
        "FROM bench.runs",
        ("poll_wait_s", "agent_run_s"),
        id="a-bare-term-added-to-a-coalesced-one-after-a-leading-column",
    ),
    pytest.param(
        "SELECT sweep_name, poll_wait_s + sum(agent_run_s) AS x FROM bench.runs",
        ("poll_wait_s", "agent_run_s"),
        id="a-bare-term-added-to-an-aggregated-one-after-a-leading-column",
    ),
    pytest.param(
        "SELECT sweep_name, caller_lane_s + coalesce(agent_lane_s, 0) AS x "
        "FROM lanes",
        ("caller_lane_s", "agent_lane_s"),
        id="the-two-lane-aliases-with-a-coalesce-on-the-second",
    ),
    # Measured 2026-08-14. A grouping paren around a term made BOTH patterns
    # find no chain at all — not a mis-graded chain, an empty result — so the
    # guard went silent on the plainest spelling of the defect it exists for.
    # The wrapped pattern required an IDENTIFIER before every `(`, so a bare
    # `(` matched nothing; the bare pattern admitted no parens whatsoever.
    # `a + (b)` is what an author writes the moment they group a term to force
    # precedence or paste one back from another query.
    pytest.param(
        "SELECT caller_lane_s + (agent_lane_s) AS identify_s FROM lanes",
        ("caller_lane_s", "agent_lane_s"),
        id="a-parenthesized-right-hand-term",
    ),
    pytest.param(
        "SELECT poll_wait_s + (agent_run_s) AS identify_s FROM bench.runs",
        ("poll_wait_s", "agent_run_s"),
        id="a-parenthesized-right-hand-raw-column",
    ),
    pytest.param(
        "SELECT (poll_wait_s) + (agent_run_s) AS identify_s FROM bench.runs",
        ("poll_wait_s", "agent_run_s"),
        id="both-terms-parenthesized",
    ),
]


@pytest.mark.parametrize("mutant_sql,lanes", _LANE_BYPASS_SHAPES)
def test_substage_lane_guard_fires_on_wrapped_and_aliased_stacks(
        tmp_path, mutant_sql: str, lanes: tuple[str, str]):
    """Each bypass measured, kept as a test — the first three on 2026-08-13,
    the other six on 2026-08-14.

    The function name records the first round's shapes; the list it is
    parametrized over now also carries six where the spelling is ordinary and
    it is the chain's POSITION in the select list that used to hide it. All of
    them mutate the same way, so they share one harness rather than three.

    Mutates a tmp copy of bench-time.json, replacing panel 2's query with a
    shape that adds across the lanes in a spelling the guard used to miss;
    never touches the committed file.
    """
    source = DASHBOARD_DIR / "bench-time.json"
    dashboard = json.loads(source.read_text(encoding="utf-8"))
    agent = "r.session_setup_s + r.agent_setup_s + r.agent_run_s"
    mutated = 0
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            if agent in (target.get("rawSql") or ""):
                target["rawSql"] = mutant_sql
                mutated += 1
    assert mutated == 1, "fixture lane panel not found to replace"

    mutant = tmp_path / source.name
    mutant.write_text(json.dumps(dashboard), encoding="utf-8")

    with pytest.raises(AssertionError, match="double-counts the seconds") as raised:
        test_no_panel_adds_both_identify_substage_lanes(mutant)
    reported = str(raised.value)
    for lane in lanes:
        assert lane in reported, (
            f"the guard fired, but not on {lane} — the chain it names is "
            f"{reported.rsplit(':', 1)[-1].strip()!r}, so this mutant does not "
            f"prove the {lanes} shape is caught")


# The mirror of the list above: two select lists that name both lanes and are
# nonetheless CORRECT, because each sum stays inside one lane. This is what
# panel 2 computes, and it escaped c6028c4 only on the wrapper it happens to
# use. Measured 2026-08-14 — with no split, the wrapped pattern finds exactly
# two chains in panel 2, `round(avg(r.submit_s + r.queue_s + r.poll_wait_s +
# r.postprocess_s)` and its agent-lane twin: each ends on the paren that closes
# `avg(`, before the comma, so each grades as one lane. Drop the wrapper and
# the tail runs on across the comma into the next entry and collects its lane,
# which is what these two shapes were measured doing on c6028c4. A false
# accusation is the more expensive failure of the two — it is what argues a
# future author into weakening this guard — so it is asserted here rather than
# left to the shipped dashboards to notice.
_LANE_LEGITIMATE_SHAPES = [
    pytest.param(
        "SELECT submit_s + queue_s, session_setup_s + agent_setup_s "
        "FROM bench.runs",
        id="one-sum-per-lane-side-by-side",
    ),
    pytest.param(
        "SELECT submit_s + queue_s AS caller_lane_s, "
        "session_setup_s + agent_setup_s AS agent_lane_s FROM bench.runs",
        id="one-aliased-sum-per-lane-side-by-side",
    ),
    # The mirror of the three parenthesized bypasses above, and the reason the
    # grouping-paren change is not simply "match more": grouping a lane's own
    # sum is ordinary SQL and must stay silent. A pattern loose enough to read
    # `a + (b)` across lanes is loose enough to misread this, so it is asserted
    # rather than assumed.
    pytest.param(
        "SELECT (submit_s + queue_s) AS caller_lane_s, "
        "(session_setup_s + agent_setup_s) AS agent_lane_s FROM bench.runs",
        id="a-parenthesized-sum-per-lane-side-by-side",
    ),
]


@pytest.mark.parametrize("legitimate_sql", _LANE_LEGITIMATE_SHAPES)
def test_substage_lane_guard_stays_silent_on_one_sum_per_lane(
        tmp_path, legitimate_sql: str):
    """Each false positive measured, kept as a test.

    Same mutation harness as the bypass shapes, run for the opposite verdict:
    the query goes onto panel 2 of a tmp copy of bench-time.json and the guard
    must return without raising. Never touches the committed file.
    """
    source = DASHBOARD_DIR / "bench-time.json"
    dashboard = json.loads(source.read_text(encoding="utf-8"))
    agent = "r.session_setup_s + r.agent_setup_s + r.agent_run_s"
    mutated = 0
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            if agent in (target.get("rawSql") or ""):
                target["rawSql"] = legitimate_sql
                mutated += 1
    assert mutated == 1, "fixture lane panel not found to replace"

    # Silence has to be a verdict rather than a subject-matter gap: the query
    # names columns from BOTH lanes, so anything that reads the select list as
    # one expression fires on it.
    named = {ident.lower() for ident in _IDENTIFIER_RE.findall(legitimate_sql)}
    assert named & _CALLER_LANE and named & _AGENT_LANE, (
        "fixture names only one lane, so staying silent would prove nothing")

    mutant = tmp_path / source.name
    mutant.write_text(json.dumps(dashboard), encoding="utf-8")

    test_no_panel_adds_both_identify_substage_lanes(mutant)


# Spend and token figures from llm_traces are a floor, not a total. Measured
# 2026-08-13 over the 2,829 rows that carry a model: cost_usd is populated on
# 1,603 of them (56.7%) and total_tokens on 1,874 (66.2%), while duration_ms is
# populated on all 2,829. A reader shown "$20.61 to date" with no caveat will
# read it as the bill. Latency panels need no such disclosure and are
# deliberately not covered — the guard keys off the aggregated COLUMN, not the
# table, so adding a percentile panel never trips it.
#
# The column may arrive table-qualified or wrapped, and both are ordinary SQL
# rather than exotic spellings: `sum(t.cost_usd)` is what a panel writes the
# moment it aliases llm_traces to join something, and `sum(coalesce(cost_usd,
# 0))` is the natural reflex for a column populated on 56.7% of rows — the very
# gap this disclosure exists to declare, so the author most likely to reach for
# coalesce is the one who most needs the guard. Both slipped past a pattern
# that required the column name immediately after the opening paren. Same
# lesson as _TRUNCATE_CALL_RE for the alias half: an alias is ordinary SQL.
#
# The wrapper half is narrower than "allow a wrapper" and the pattern below is
# the authority, not this sentence: exactly ONE wrapper is allowed and it is
# `coalesce` by name. Measured 2026-08-14 — `sum(nullif(cost_usd, 0))`,
# `sum(greatest(cost_usd, 0))`, `sum(round(cost_usd, 4))` and a doubled
# `sum(coalesce(coalesce(cost_usd, 0), 0))` all go unmatched, so a panel
# spelling its total any of those ways would carry no disclosure and nothing
# here would say so. That is a known and accepted limit (owner ruling), not an
# oversight — written down so the next reader finds a stated boundary rather
# than rediscovering it as a hole.
_SPEND_AGGREGATE_RE = re.compile(
    r"\b(?:sum|avg)\s*\(\s*(?:coalesce\s*\(\s*)?(?:[a-z_][a-z0-9_]*\s*\.\s*)?"
    r"(?:cost_usd|total_tokens|prompt_tokens|completion_tokens)\b",
    re.IGNORECASE)
_COVERAGE_DISCLOSURE = "a floor, not a total"


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_llm_spend_panels_disclose_their_coverage(path: Path):
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    for panel in dashboard.get("panels", []):
        sqls = [t["rawSql"] for t in panel.get("targets", []) if t.get("rawSql")]
        spends = [
            sql for sql in sqls
            if _LLM_TRACES_FROM_RE.search(sql) and _SPEND_AGGREGATE_RE.search(sql)
        ]
        if not spends:
            continue
        description = panel.get("description") or ""
        assert _COVERAGE_DISCLOSURE in description, (
            f"{path.name} panel {panel.get('title')!r} totals a cost or token "
            f"column from llm_traces but its description never says the figure "
            f"is {_COVERAGE_DISCLOSURE!r} — cost_usd is recorded on 1,603 of "
            f"2,829 model calls and total_tokens on 1,874"
        )


def test_coverage_disclosure_guard_fires_on_an_undisclosed_spend_panel(tmp_path):
    """Proving the guard passes on the shipped file is not the same as proving
    it REJECTS a panel that drops the caveat. Mutates a tmp copy of the cost
    dashboard, stripping the disclosure from whichever spend panel carries it;
    never touches the committed file.
    """
    source = DASHBOARD_DIR / "cost-latency-capacity.json"
    dashboard = json.loads(source.read_text(encoding="utf-8"))
    stripped = 0
    for panel in dashboard["panels"]:
        description = panel.get("description") or ""
        if _COVERAGE_DISCLOSURE in description:
            panel["description"] = description.replace(_COVERAGE_DISCLOSURE, "exact")
            stripped += 1
    assert stripped, "fixture disclosure not found to strip"

    mutant = tmp_path / source.name
    mutant.write_text(json.dumps(dashboard), encoding="utf-8")

    with pytest.raises(AssertionError, match="never says the figure is"):
        test_llm_spend_panels_disclose_their_coverage(mutant)


# The two spellings that totalled a spend column while the guard looked away,
# both measured on 2026-08-13. An alias appears the moment a panel joins
# anything to llm_traces; a coalesce appears the moment an author notices the
# column is null on 43% of rows — which is precisely the reader this disclosure
# is written for.
_SPEND_BYPASS_SHAPES = [
    pytest.param(
        "SELECT round(sum(t.cost_usd)::numeric, 4) AS spend_usd FROM llm_traces t "
        "WHERE nullif(t.model, '') IS NOT NULL",
        id="a-table-alias-on-the-column",
    ),
    pytest.param(
        "SELECT sum(coalesce(total_tokens, 0)) AS tokens FROM llm_traces "
        "WHERE nullif(model, '') IS NOT NULL",
        id="a-coalesce-around-the-column",
    ),
]


@pytest.mark.parametrize("mutant_sql", _SPEND_BYPASS_SHAPES)
def test_coverage_disclosure_guard_fires_on_wrapped_and_aliased_totals(
        tmp_path, mutant_sql: str):
    """Each bypass kept as a test.

    Puts the shape on cost-latency-capacity.json's first panel, which totals
    nothing from llm_traces today and carries no disclosure, so the guard has to
    reach the new query to fire at all — the disclosed spend panels beside it
    stay untouched and keep passing. Mutates a tmp copy; never touches the
    committed file.
    """
    source = DASHBOARD_DIR / "cost-latency-capacity.json"
    dashboard = json.loads(source.read_text(encoding="utf-8"))
    panel = dashboard["panels"][0]
    assert _COVERAGE_DISCLOSURE not in (panel.get("description") or ""), (
        "fixture panel already carries the disclosure, so this mutant would "
        "pass for the wrong reason")
    panel["targets"][0]["rawSql"] = mutant_sql

    mutant = tmp_path / source.name
    mutant.write_text(json.dumps(dashboard), encoding="utf-8")

    with pytest.raises(AssertionError, match="never says the figure is") as raised:
        test_llm_spend_panels_disclose_their_coverage(mutant)
    assert panel["title"] in str(raised.value), (
        "the guard fired on a different panel than the mutated one")
