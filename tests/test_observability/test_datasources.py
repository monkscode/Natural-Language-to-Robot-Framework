"""Static guards over the committed Grafana datasource provisioning.

The dashboards have had a guard suite since Phase 1; the datasource file has
not, and it now carries behaviour — a derived field that turns a workflow id in
a log line into a link back to that run's trace view. A wrong regex there fails
the way a dead link fails: silently, and only for whoever clicks it.

Referenced by: nothing — pytest entry point.
Depends on: observability/grafana/provisioning/datasources/datasources.yml
"""
import re
from pathlib import Path

import pytest
import yaml

from tests.test_observability.test_dashboards import _dashboard_uids, _dashboards

DATASOURCE_FILE = Path(
    "observability/grafana/provisioning/datasources/datasources.yml")

# The /d/<uid> segment of a derived field's url — the same shape
# test_dashboards.py's own link-target guard parses
# (test_every_data_link_targets_a_dashboard_that_exists). A filename can
# survive a rename of the uid inside it, so the check that matters is
# against the uid the url actually names, not the file it happens to live in.
_DERIVED_FIELD_TARGET_RE = re.compile(r"^/d/([a-z0-9-]+)")

# A real log line's shape, with the hazard in it: user_id is written BEFORE
# workflow_id, and both are UUIDs. Synthetic values, so this file names no run.
SAMPLE_LINE = (
    '{"event": "step complete", '
    '"user_id": "aaaaaaaa-1111-2222-3333-444444444444", '
    '"workflow_id": "bbbbbbbb-5555-6666-7777-888888888888", '
    '"level": "info", "logger": "root"}'
)
EXPECTED_WORKFLOW_ID = "bbbbbbbb-5555-6666-7777-888888888888"


def _loki_derived_fields() -> list[dict]:
    parsed = yaml.safe_load(DATASOURCE_FILE.read_text(encoding="utf-8"))
    loki = [d for d in parsed["datasources"] if d.get("type") == "loki"]
    assert len(loki) == 1, f"expected one Loki datasource, found {len(loki)}"
    return loki[0].get("jsonData", {}).get("derivedFields", [])


def _assert_derived_field_is_sound(field: dict) -> None:
    """The whole rule in one place, so the mutation test below can drive the
    real assertions with a field the shipped file does not contain. Inlining
    these into the test instead would leave the mutation test re-implementing
    the rule and proving nothing about the guard."""
    pattern = re.compile(field["matcherRegex"])
    match = pattern.search(SAMPLE_LINE)
    assert match, f"matcherRegex matches nothing in a real log line: {field['matcherRegex']}"
    captured = match.group(1)
    assert captured == EXPECTED_WORKFLOW_ID, (
        f"matcherRegex captured {captured!r} rather than the workflow id — a "
        f"log line carries more than one UUID and Grafana takes the first "
        f"match, so the regex must be anchored to the workflow_id key")
    assert len(captured) == 36, f"captured id is {len(captured)} chars, not a whole UUID"
    assert "/d/mark1-trace-run" in field["url"], (
        f"derived field links to {field['url']!r}, not the trace dashboard")
    # $$ (not $) is deliberate: provisioning YAML sends every ${...} through
    # env var expansion, and a single-$ __value.raw resolves against a
    # nonexistent env var and silently becomes empty — verified empirically
    # against this datasource file, where the sibling Postgres block's
    # ${POSTGRES_DB} really does resolve to the live env value on read-back.
    # The file must therefore hold the escaped $$ form so Grafana's own
    # frontend, not the provisioner, is what interpolates __value.raw.
    assert "var-run_id=$${__value.raw}" in field["url"], (
        f"derived field must pass the raw captured value: {field['url']!r}")


def test_loki_derived_field_captures_a_whole_workflow_id():
    """The link a log line offers must open the run that line belongs to.

    Two ways it does not. First, a log line carries more than one UUID —
    user_id is written before workflow_id — and Grafana uses the FIRST match,
    so a regex that is not anchored to the key opens the trace view for a user
    id and renders an empty dashboard with nothing saying why. Second, a
    capture shorter than 36 characters truncates the id, which the standing
    owner rule forbids everywhere including in a URL.
    """
    fields = _loki_derived_fields()
    assert len(fields) == 1, (
        f"expected exactly one derived field, found {len(fields)} — more than "
        f"one produces competing links on the same log line")
    known = {p.name for p in _dashboards()}
    assert "trace-one-run.json" in known, "the link target dashboard file is gone"

    # The filename check above proves a file exists; it says nothing about
    # what uid that file declares. Grafana resolves /d/<uid> by uid, not by
    # filename, so the check that actually matters is that the uid the url
    # names is one a committed dashboard declares — change the uid without
    # renaming the file and the link above would still pass while the click
    # dead-ends. Kept alongside it rather than in place of it: the two catch
    # different accidents (file deleted/renamed vs. uid changed in place),
    # and this one still derives the uid from the url itself rather than a
    # second hardcoded string, so it can't drift independently of what the
    # field actually links to.
    target = _DERIVED_FIELD_TARGET_RE.match(fields[0]["url"])
    assert target, f"derived field url is not a /d/<uid> path: {fields[0]['url']!r}"
    assert target.group(1) in _dashboard_uids(), (
        f"derived field links to dashboard uid {target.group(1)!r}, which no "
        f"committed dashboard declares — the target file can keep its name "
        f"while its uid drifts, and the link would render normally and "
        f"dead-end on click: {fields[0]['url']!r}")

    _assert_derived_field_is_sound(fields[0])


def test_derived_field_guard_fires_on_a_regex_that_is_not_anchored_to_the_key():
    """Proving the shipped regex is right is not the same as proving the guard
    REJECTS the loose one — and the loose one is what an author reaches for,
    because it is shorter and it does match a workflow id. It just matches the
    user id first. Drives the real assertions with a mutated copy of the
    shipped field; never writes to the datasource file."""
    loose_pattern = r"([0-9a-f-]{36})"
    first = re.search(loose_pattern, SAMPLE_LINE).group(1)
    assert first != EXPECTED_WORKFLOW_ID, "fixture line no longer carries the hazard"

    mutated = dict(_loki_derived_fields()[0], matcherRegex=loose_pattern)
    with pytest.raises(AssertionError, match="anchored to the workflow_id key"):
        _assert_derived_field_is_sound(mutated)


def test_derived_field_guard_fires_on_a_single_dollar_url():
    """Proving the shipped file has $$ is not the same as proving the guard
    REJECTS a single $ — and a single $ is what a future author reaches for,
    because $$ reads like a typo. It is not: provisioning YAML sends every
    ${...} through env var expansion, so a single-$ __value.raw resolves
    against a nonexistent env var and is silently replaced with an empty
    string. The link still renders — Grafana has no way to know the value it
    was supposed to carry is missing — and dead-ends on click with nothing on
    screen saying why. Drives the real assertion with a mutated copy of the
    shipped field; never writes to the datasource file."""
    field = _loki_derived_fields()[0]
    unescaped_url = field["url"].replace("$${__value.raw}", "${__value.raw}")
    assert unescaped_url != field["url"], "fixture url has no $${__value.raw} to unescape"

    mutated = dict(field, url=unescaped_url)
    with pytest.raises(AssertionError, match="must pass the raw captured value"):
        _assert_derived_field_is_sound(mutated)
