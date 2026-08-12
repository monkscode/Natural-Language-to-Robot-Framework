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


def _after_from(sql: str) -> str:
    """The text from the first FROM onward — where a real WHERE clause lives.

    A select-list `count(*) FILTER (WHERE ...)` also contains the WHERE
    token, so anchoring on the token alone cannot tell a restriction that
    narrows the aggregate's row set from one that merely labels a column.
    Everything before the first FROM is the select list, so bounding the
    search after it excludes the FILTER form by construction. The mirror of
    _duration_aliases, which bounds itself to the text before that same FROM.
    """
    from_at = sql.upper().find(" FROM ")
    return sql[from_at:] if from_at != -1 else sql


_COST_SPLIT_RESTRICTION_RE = re.compile(
    r"\bWHERE\b.*data->>'(?:crewai_cost|browser_use_cost)'\s*\)?\s*::\s*numeric\s*>\s*0",
    re.IGNORECASE | re.DOTALL)


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_cost_split_panels_exclude_runs_that_recorded_no_split(path: Path):
    if path.name == "trace-one-run.json":
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
