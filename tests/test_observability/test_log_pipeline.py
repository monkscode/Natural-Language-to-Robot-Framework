"""Static guards over the log pipeline's level classification.

The level a log line carries in Loki is set in TWO files that have to agree,
and neither one reads as broken on its own. Alloy stamps `detected_level`
from the level the application recorded; Loki is told to stop guessing one
from the raw text. Delete either half and the other silently stops working —
which is the whole reason this file exists.

The guards below check wiring, not just presence. Four mutations that leave
every Alloy component healthy and every line flowing were found to pass an
earlier presence-only version of this file, the worst being an empty
`forward_to` inside the process block: logs stop reaching Loki entirely and
nothing complains.

Referenced by: nothing — pytest entry point.
Depends on: observability/alloy/config.alloy, observability/loki/loki-config.yml
"""
import re
from pathlib import Path

import yaml

ALLOY_CONFIG = Path("observability/alloy/config.alloy")
LOKI_CONFIG = Path("observability/loki/loki-config.yml")

# The stages, in the only order that works. json first so a real record wins;
# regex second so uvicorn's line-start prefix is read only when json found
# nothing; template third to normalise and default; structured_metadata last,
# because it can only attach a value the earlier stages have already put in
# the extracted map.
EXPECTED_STAGE_ORDER = [
    "stage.json",
    "stage.regex",
    "stage.template",
    "stage.structured_metadata",
]


def _alloy() -> str:
    return ALLOY_CONFIG.read_text(encoding="utf-8")


def _loki() -> dict:
    return yaml.safe_load(LOKI_CONFIG.read_text(encoding="utf-8"))


def _block(header: str, text: str, closing: str) -> str:
    """The body of a `header { ... }` block, up to its own closing brace.

    `closing` is what distinguishes the block's own brace from the braces of
    the blocks nested inside it — indentation is the only thing that does,
    and getting it wrong fails in both directions. A top-level block closes
    at column 0; a stage nested in one closes indented. Matching "any
    whitespace" for a top-level block stops at the first nested stage;
    matching greedily instead runs past the block entirely and swallows
    whatever follows, which would let a stage moved OUT of the pipeline
    still satisfy the assertions below.
    """
    match = re.search(header + r"\s*\{(.*?)\n" + closing + r"\}", text, re.S)
    assert match, f"block {header!r} not found"
    return match.group(1)


def _top_level(header: str) -> str:
    """A block at column 0 — `alloy fmt` never indents these."""
    return _block(header, _alloy(), closing="")


def _process_block() -> str:
    return _top_level(r'loki\.process\s+"level"')


def _stage(name: str) -> str:
    """A stage nested inside the process block, so its brace is indented.

    Space or tab: `alloy fmt` re-indents with tabs, the committed file uses
    spaces, and this has to hold either way.
    """
    return _block(re.escape(name), _process_block(), closing=r"[ \t]+")


def test_docker_source_forwards_only_through_the_level_stage():
    """The scrape must reach Loki via the stage, and not also bypass it.

    Forwarding straight to `loki.write` stamps nothing and returns level
    classification to Loki's guessing. Forwarding to BOTH ingests every line
    twice, which reads as a traffic doubling rather than as a config error.
    """
    source = _top_level(r'loki\.source\.docker\s+"containers"')

    assert "loki.process.level.receiver" in source, (
        "the docker source does not forward through loki.process.level; "
        "nothing stamps detected_level and Loki falls back to guessing")
    assert "loki.write" not in source, (
        "the docker source also forwards straight to loki.write, so every "
        "line is ingested twice")


def test_the_level_stage_still_reaches_loki():
    """An empty `forward_to` drops every log line and stays healthy.

    This is the failure this file exists to catch and the one a presence-only
    guard misses: Alloy reports all components healthy, Loki simply stops
    receiving anything.
    """
    assert "loki.write.default.receiver" in _process_block(), (
        "loki.process.level does not forward to loki.write; every log line "
        "is silently dropped and every component still reports healthy")


def test_stages_run_in_the_only_order_that_works():
    body = _process_block()
    positions = []
    for stage in EXPECTED_STAGE_ORDER:
        assert stage in body, f"{stage} is missing from loki.process.level"
        positions.append(body.index(stage))

    assert positions == sorted(positions), (
        f"stages are out of order: {EXPECTED_STAGE_ORDER} must appear in "
        f"that sequence, found offsets {positions}")


def test_alloy_stamps_detected_level_from_the_recorded_level():
    """The stamp must come from the application's own `level` field.

    Measured 2026-08-14 over the five days Loki held, counting only lines
    that are NOT structlog records: 7,769 carried a level Loki had guessed.
    1,053 of those were CrewAI box-drawing prompt echo and every one was
    wrong — 270 stamped critical, 232 warn, 551 error. Discovery was correct
    on all 16,436 real structlog records, and correct on the 6,408 uvicorn
    access lines the regex stage below preserves.

    The trigger is not only the `KEYWORD:` shape. `--- CRITICAL: ONLY
    EXPLICIT ELEMENTS ---` is stamped critical, but so is a prompt line that
    merely contains the bare word `error` in prose — that is where 551 of
    the 1,053 came from.
    """
    extraction = _stage("stage.json")
    assert re.search(r'\blevel\s*=\s*"level"', extraction), (
        "stage.json does not extract the application's `level` field into "
        "`level`; a rename here leaves every record stamped unknown")

    mapping = _stage("stage.structured_metadata")
    assert re.search(r'detected_level\s*=\s*"level"', mapping), (
        "structured metadata does not map detected_level from the extracted "
        "level; Loki will keep guessing from raw text")


def test_uvicorn_keeps_its_level_and_the_rule_cannot_reach_prompt_text():
    """uvicorn writes plain text with the level as a line-start prefix.

    Loki classified those 6,408 lines correctly, so dropping them to unknown
    would be a regression. The anchor is the safety property: every CrewAI
    echo line starts with a box-drawing character, never with `LEVEL:`, so an
    anchored rule cannot reach prompt text. Unanchored, it would re-introduce
    exactly the bug this pipeline removes.

    Verified live 2026-08-14: `INFO:     127.0.0.1 - "GET /health" 200 OK`
    stores info, `ERROR:    Exception in ASGI application` stores error, and
    both prompt-echo shapes still store unknown.
    """
    expression = _stage("stage.regex")
    assert expression.lstrip().count("^") and re.search(
        r'expression\s*=\s*"\^', expression), (
        "the uvicorn level rule is not anchored to the start of the line; "
        "unanchored it matches prompt text and re-creates the bug")
    assert "uvicorn_level" in expression, (
        "stage.regex does not capture a uvicorn_level group")


def test_non_records_default_to_unknown():
    """Lines that are not log records must not borrow a level.

    95.4% of the fastapi stream is not a log record at all — 73.7% of it is
    CrewAI's verbose box-drawing echo of the prompt. Those lines have no
    level to report and must say so rather than inherit one.

    Verified live 2026-08-14 against the real Alloy pipeline: a JSON record
    with no `level` key stores `unknown`, and `ToLower` is load-bearing —
    `"ERROR"` stores `error`, `"Warning"` stores `warning`. Only `info`,
    `error` and `warning` occur in this deployment's logs.

    Known and accepted: a numeric level (`"level": 40`, what stdlib logging
    would write) passes through as `detected_level=40`. No logger here emits
    one, so no guard was added for it.
    """
    template = _stage("stage.template")

    assert re.search(r'source\s*=\s*"level"', template), (
        "stage.template does not read the extracted `level`, so nothing is "
        "normalised or defaulted")
    assert re.search(r'\{\{\s*else\s*\}\}unknown\{\{\s*end\s*\}\}', template), (
        "stage.template does not default a missing level to unknown")
    assert template.count("ToLower") >= 2, (
        "stage.template does not lower-case both the recorded level and the "
        "uvicorn prefix; an upstream logger writing ERROR would stamp a "
        "level Loki does not recognise")


def test_loki_does_not_guess_levels_from_raw_text():
    """Loki's discovery must be off, or it overrides our `unknown` stamps.

    This is the coupling that is invisible from either file alone. Loki
    honours an incoming `detected_level` only when it names a real level; it
    treats the literal string `unknown` as absent and re-runs discovery.
    Measured 2026-08-14 by pushing the same line four ways:

        stamped info    + text "--- CRITICAL: ..."  -> stored info
        stamped unknown + text "--- CRITICAL: ..."  -> stored CRITICAL
        no stamp        + text "--- CRITICAL: ..."  -> stored critical
        stamped unknown + text "--- ERROR: ..."     -> stored ERROR

    So the Alloy stage alone does not work.
    """
    limits = _loki().get("limits_config", {})

    assert "discover_log_levels" in limits, (
        "discover_log_levels is unset, so it defaults to true and Loki "
        "overrides every unknown stamp with a keyword guess")
    assert limits["discover_log_levels"] is False, (
        f"discover_log_levels is {limits['discover_log_levels']!r}; it must "
        f"be false or Alloy's stamps are discarded for non-records")
