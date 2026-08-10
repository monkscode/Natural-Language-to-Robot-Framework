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


def _from_clause_real_tables(sql: str) -> list[str]:
    """Bare table identifiers in a single FROM clause, comma-separated or
    not. A `jsonb_each(...)`-shaped segment (a set-returning function, no
    space before its opening paren) is excluded — that is the lateral
    expansion idiom, not a second table."""
    match = _FROM_CLAUSE_RE.search(sql)
    if not match:
        return []
    tables = []
    for segment in match.group(1).split(","):
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
        # LATERAL expansions alias jsonb functions, not tables.
        referenced = {
            t.lower() for t in _TABLE_RE.findall(sql)
            if t.lower() not in {"lateral", "jsonb_each", "jsonb_array_elements",
                                 "jsonb_each_text"}
        }
        # A comma-join (`FROM a, b`) names its second table without a FROM
        # or JOIN keyword in front of it, which _TABLE_RE never sees.
        referenced |= {t.lower() for t in _from_clause_real_tables(sql)}
        unknown = referenced - GRANTED_TABLES - ctes
        assert not unknown, f"{path.name} queries ungranted tables: {sorted(unknown)}"


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_no_panel_time_filters_on_naive_ts(path: Path):
    """workflow_metrics.ts runs 5.5h ahead of the tz-aware columns on this
    deployment. Bucketing or windowing on it silently shifts every result.

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
