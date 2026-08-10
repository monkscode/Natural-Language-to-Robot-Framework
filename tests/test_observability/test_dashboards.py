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
        unknown = referenced - GRANTED_TABLES - ctes
        assert not unknown, f"{path.name} queries ungranted tables: {sorted(unknown)}"


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_no_panel_time_filters_on_naive_ts(path: Path):
    """workflow_metrics.ts runs 5.5h ahead of the tz-aware columns on this
    deployment. Bucketing or windowing on it silently shifts every result."""
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    for sql in _sql_targets(dashboard):
        collapsed = " ".join(sql.split()).lower()
        assert "date_trunc('day', m.ts" not in collapsed
        assert "date_trunc('hour', m.ts" not in collapsed
        assert "$__timefilter(m.ts)" not in collapsed
        assert "$__timefilter(ts)" not in collapsed


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.name)
def test_aggregate_dashboards_use_no_inner_join(path: Path):
    """Cross-table history is sparse: 30/429 metrics rows have a test_runs
    row, 9/118 execution records join to metrics. A bare or INNER JOIN in an
    aggregate panel silently drops most of the data.

    Every occurrence of the JOIN keyword must be qualified LEFT JOIN
    (or LEFT OUTER JOIN / CROSS JOIN) — a bare JOIN or an explicit INNER
    JOIN fails. trace-one-run.json is exempt: on a per-id dashboard a
    missing partner row is informative, not a silently dropped population.
    """
    if path.name == "trace-one-run.json":
        return  # per-id dashboard: a missing partner row is informative
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    for sql in _sql_targets(dashboard):
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
    """Standing owner rule: full uuid, always."""
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    for sql in _sql_targets(dashboard):
        collapsed = " ".join(sql.split()).lower()
        for column in ("workflow_id", "run_id"):
            assert f"left({column}" not in collapsed
            assert f"substring({column}" not in collapsed


def test_trace_dashboard_has_a_run_id_variable():
    path = DASHBOARD_DIR / "trace-one-run.json"
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    names = {v["name"] for v in dashboard.get("templating", {}).get("list", [])}
    assert "run_id" in names
