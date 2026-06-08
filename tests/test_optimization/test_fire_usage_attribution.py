"""
Phase 3 — usage-attribution fire function + telemetry write path.

Covers:
  - write_trigger_event's used_hint_ids / unused_hint_ids columns and the
    json.dumps(None)=="null" NULL trap.
  - fire_usage_attribution: P1 no-fallback skips, empty-S skip, standalone +
    Case-B happy paths, membership guard, disjointness, inline retry, retry
    exhaustion, JSON-parse failure, F4 prompt-builder fault, and that it never
    touches circuit_breaker.
  - F4 layering: the optimization package never imports services.

Real SQLite (in_memory_em / in_memory_db fixtures); the LLM call is mocked.
"""

import json
import pathlib
import re
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from src.backend.crew_ai.optimization.feedback_loop import FeedbackLoop
from src.backend.crew_ai.optimization.nl_feedback_engine import NLFeedbackEngine
from src.backend.crew_ai.optimization import conflict_detection

_LLM = "src.backend.crew_ai.optimization.learning_config._call_conflict_detection_llm"


def _fake_response(content, pt=10, ct=5):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=pt, completion_tokens=ct),
    )


class _ImmediateQueue:
    """Runs submitted tasks synchronously on the (conftest-renamed) writer thread."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class _RecordingFL:
    """Minimal FeedbackLoop stand-in: real NL engine + immediate queue, with
    write_trigger_event recorded so telemetry kwargs can be asserted."""

    def __init__(self, em):
        self.nl_engine = NLFeedbackEngine(em)
        self.write_queue = _ImmediateQueue()
        self.trigger_events = []
        self.circuit_breaker = MagicMock()  # must never be touched by attribution

    def write_trigger_event(self, **kwargs):
        self.trigger_events.append(kwargs)


def _insert_exec(conn, workflow_id, *, done=0):
    conn.execute(
        "INSERT INTO execution_records "
        "(workflow_id, timestamp, user_query, test_status, hint_attribution_done) "
        "VALUES (?, '2026-01-01T00:00:00+00:00', 'q', 'passed', ?)",
        (workflow_id, done),
    )


def _insert_hint(conn, text, *, success=0, failure=0):
    cur = conn.execute(
        "INSERT INTO nl_feedback_corrections "
        "(feedback_text, category, scope, success_count, failure_count, "
        " created_at, last_seen) "
        "VALUES (?, 'C1', 'global', ?, ?, '2026-01-01', '2026-01-01') RETURNING id",
        (text, success, failure),
    )
    return cur.fetchone()["id"]


def _hint(conn, hid):
    return conn.execute(
        "SELECT success_count, failure_count, unused_count, conflict_flagged "
        "FROM nl_feedback_corrections WHERE id = ?",
        (hid,),
    ).fetchone()


# ===================================================================
# write_trigger_event — used/unused columns + NULL trap
# ===================================================================

class TestWriteTriggerEventUsageColumns:

    def test_used_unused_written_and_none_is_sql_null(self, in_memory_em):
        fake_self = SimpleNamespace(execution_memory=in_memory_em)
        FeedbackLoop.write_trigger_event(
            fake_self,
            trigger_type="usage_attribution", workflow_id="wf-te",
            domain="d", url="u", feedback_text=None,
            active_hint_ids=[1, 2], flagged_hint_ids=[], actually_flagged_hint_ids=[],
            reason=None, llm_model="m", input_tokens=1, output_tokens=2,
            llm_latency_ms=3, status="succeeded", error_message=None,
            used_hint_ids=[1], unused_hint_ids=None,
        )
        row = in_memory_em._writer_conn.execute(
            "SELECT used_hint_ids, unused_hint_ids FROM trigger_events "
            "WHERE workflow_id = 'wf-te'"
        ).fetchone()
        assert row["used_hint_ids"] == "[1]"
        # The NULL trap: None must be SQL NULL, NOT the string "null".
        assert row["unused_hint_ids"] is None

    def test_legacy_trigger_call_leaves_columns_null(self, in_memory_em):
        """A trigger_1/2 caller that omits the new kwargs → both columns NULL."""
        fake_self = SimpleNamespace(execution_memory=in_memory_em)
        FeedbackLoop.write_trigger_event(
            fake_self,
            trigger_type="trigger_2", workflow_id="wf-legacy",
            domain="d", url="u", feedback_text=None,
            active_hint_ids=[1], flagged_hint_ids=[1], actually_flagged_hint_ids=[1],
            reason=None, llm_model="m", input_tokens=0, output_tokens=0,
            llm_latency_ms=0, status="succeeded", error_message=None,
        )
        row = in_memory_em._writer_conn.execute(
            "SELECT used_hint_ids, unused_hint_ids FROM trigger_events "
            "WHERE workflow_id = 'wf-legacy'"
        ).fetchone()
        assert row["used_hint_ids"] is None
        assert row["unused_hint_ids"] is None


# ===================================================================
# fire_usage_attribution — P1 skips (no scope-wide fallback)
# ===================================================================

class TestP1NoFallback:

    @pytest.mark.parametrize("injected", [None, "{bad json", "[]", "null", '"x"'])
    def test_skips_without_llm_or_write(self, in_memory_db, injected):
        fl = _RecordingFL(in_memory_db)
        with patch(_LLM) as llm:
            result = conflict_detection.fire_usage_attribution(
                feedback_loop=fl, workflow_id="wf", domain="d", url="u",
                feedback_text=None, injected_hint_ids=injected, case_b=False,
                prompt_builder=lambda hints: "p",
            )
        assert result == ([], [], [], {})
        llm.assert_not_called()
        assert fl.trigger_events == []  # P1 skip writes nothing

    def test_empty_active_set_skips_with_telemetry(self, in_memory_db):
        """Injected ids that no longer resolve (disabled/flagged) → skip + a
        no_active_hints telemetry row, no LLM call."""
        fl = _RecordingFL(in_memory_db)
        in_memory_db.commit()
        with patch(_LLM) as llm:
            result = conflict_detection.fire_usage_attribution(
                feedback_loop=fl, workflow_id="wf", domain="d", url="u",
                feedback_text=None, injected_hint_ids="[999]", case_b=False,
                prompt_builder=lambda hints: "p",
            )
        assert result == ([], [], [], {})
        llm.assert_not_called()
        assert len(fl.trigger_events) == 1
        assert fl.trigger_events[0]["status"] == "no_active_hints"


# ===================================================================
# fire_usage_attribution — happy paths
# ===================================================================

class TestHappyPaths:

    def test_standalone_used_and_unused(self, in_memory_db):
        a = _insert_hint(in_memory_db, "A")
        b = _insert_hint(in_memory_db, "B")
        _insert_exec(in_memory_db, "wf-s")
        in_memory_db.commit()
        fl = _RecordingFL(in_memory_db)
        content = json.dumps({"used": [a], "unused": [b]})

        with patch(_LLM, return_value=_fake_response(content)):
            result = conflict_detection.fire_usage_attribution(
                feedback_loop=fl, workflow_id="wf-s", domain="d", url="u",
                feedback_text=None, injected_hint_ids=json.dumps([a, b]),
                case_b=False, prompt_builder=lambda hints: "p",
            )

        assert result == ([a], [], [b], {})
        assert _hint(in_memory_db, a)["success_count"] == 1
        assert _hint(in_memory_db, b)["unused_count"] == 1
        ev = fl.trigger_events[-1]
        assert ev["trigger_type"] == "usage_attribution"
        assert ev["used_hint_ids"] == [a] and ev["unused_hint_ids"] == [b]
        assert ev["flagged_hint_ids"] == [] and ev["status"] == "succeeded"
        fl.circuit_breaker.record_error.assert_not_called()

    def test_case_b_harmful_bucket_flags_and_tags_trigger_1(self, in_memory_db):
        a = _insert_hint(in_memory_db, "A")
        b = _insert_hint(in_memory_db, "B")
        _insert_exec(in_memory_db, "wf-b")
        in_memory_db.commit()
        fl = _RecordingFL(in_memory_db)
        content = json.dumps({"used": [a], "harmful": [{"id": b, "reason": "removed it"}]})

        with patch(_LLM, return_value=_fake_response(content)):
            result = conflict_detection.fire_usage_attribution(
                feedback_loop=fl, workflow_id="wf-b", domain="d", url="u",
                feedback_text=None, injected_hint_ids=json.dumps([a, b]),
                case_b=True, prompt_builder=lambda hints: "p",
            )

        assert result == ([a], [b], [], {b: "removed it"})
        assert _hint(in_memory_db, a)["success_count"] == 1
        assert _hint(in_memory_db, b)["failure_count"] == 1
        assert _hint(in_memory_db, b)["conflict_flagged"] == 1
        ev = fl.trigger_events[-1]
        assert ev["trigger_type"] == "trigger_1"
        assert ev["flagged_hint_ids"] == [b]
        assert ev["actually_flagged_hint_ids"] == [b]
        assert ev["used_hint_ids"] == [a]

    def test_standalone_ignores_harmful_bucket(self, in_memory_db):
        a = _insert_hint(in_memory_db, "A")
        _insert_exec(in_memory_db, "wf-ih")
        in_memory_db.commit()
        fl = _RecordingFL(in_memory_db)
        content = json.dumps({"used": [], "harmful": [a], "unused": []})

        with patch(_LLM, return_value=_fake_response(content)):
            result = conflict_detection.fire_usage_attribution(
                feedback_loop=fl, workflow_id="wf-ih", domain="d", url="u",
                feedback_text=None, injected_hint_ids=json.dumps([a]),
                case_b=False, prompt_builder=lambda hints: "p",
            )

        assert result == ([], [], [], {})  # no harmful bucket when case_b is False
        assert _hint(in_memory_db, a)["failure_count"] == 0


# ===================================================================
# fire_usage_attribution — guards: membership + disjointness
# ===================================================================

class TestGuards:

    def test_membership_guard_drops_stray_id(self, in_memory_db):
        a = _insert_hint(in_memory_db, "A")
        _insert_exec(in_memory_db, "wf-mg")
        in_memory_db.commit()
        fl = _RecordingFL(in_memory_db)
        content = json.dumps({"used": [a, 99999], "unused": []})

        with patch(_LLM, return_value=_fake_response(content)):
            result = conflict_detection.fire_usage_attribution(
                feedback_loop=fl, workflow_id="wf-mg", domain="d", url="u",
                feedback_text=None, injected_hint_ids=json.dumps([a]),
                case_b=False, prompt_builder=lambda hints: "p",
            )

        assert result == ([a], [], [], {})  # stray 99999 dropped

    def test_disjointness_drops_contradicted_id(self, in_memory_db):
        a = _insert_hint(in_memory_db, "A")
        _insert_exec(in_memory_db, "wf-dj")
        in_memory_db.commit()
        fl = _RecordingFL(in_memory_db)
        content = json.dumps({"used": [a], "unused": [a]})  # contradiction

        with patch(_LLM, return_value=_fake_response(content)):
            result = conflict_detection.fire_usage_attribution(
                feedback_loop=fl, workflow_id="wf-dj", domain="d", url="u",
                feedback_text=None, injected_hint_ids=json.dumps([a]),
                case_b=False, prompt_builder=lambda hints: "p",
            )

        assert result == ([], [], [], {})  # a in 2 buckets → no-signal
        assert _hint(in_memory_db, a)["success_count"] == 0


# ===================================================================
# fire_usage_attribution — retry / failure paths
# ===================================================================

class TestRetryAndFailure:

    def test_retry_then_success(self, in_memory_db):
        a = _insert_hint(in_memory_db, "A")
        _insert_exec(in_memory_db, "wf-rt")
        in_memory_db.commit()
        fl = _RecordingFL(in_memory_db)
        content = json.dumps({"used": [a], "unused": []})

        with patch("src.backend.crew_ai.optimization.conflict_detection.time.sleep"), \
                patch(_LLM, side_effect=[TimeoutError("t"), _fake_response(content)]) as llm:
            result = conflict_detection.fire_usage_attribution(
                feedback_loop=fl, workflow_id="wf-rt", domain="d", url="u",
                feedback_text=None, injected_hint_ids=json.dumps([a]),
                case_b=False, prompt_builder=lambda hints: "p",
            )

        assert llm.call_count == 2
        assert result == ([a], [], [], {})
        assert _hint(in_memory_db, a)["success_count"] == 1

    def test_retry_exhausted_does_not_apply(self, in_memory_db):
        a = _insert_hint(in_memory_db, "A")
        _insert_exec(in_memory_db, "wf-ex")
        in_memory_db.commit()
        fl = _RecordingFL(in_memory_db)

        with patch("src.backend.crew_ai.optimization.conflict_detection.time.sleep"), \
                patch(_LLM, side_effect=TimeoutError("down")) as llm:
            result = conflict_detection.fire_usage_attribution(
                feedback_loop=fl, workflow_id="wf-ex", domain="d", url="u",
                feedback_text=None, injected_hint_ids=json.dumps([a]),
                case_b=False, prompt_builder=lambda hints: "p",
            )

        assert llm.call_count == 2
        assert result == ([], [], [], {})
        assert _hint(in_memory_db, a)["success_count"] == 0  # not applied
        # The once-guard flag stays unset so a later pass can retry.
        done = in_memory_db.execute(
            "SELECT hint_attribution_done FROM execution_records WHERE workflow_id='wf-ex'"
        ).fetchone()["hint_attribution_done"]
        assert done == 0
        assert fl.trigger_events[-1]["status"] == "llm_timeout"
        fl.circuit_breaker.record_error.assert_not_called()

    def test_json_parse_failure_does_not_apply(self, in_memory_db):
        a = _insert_hint(in_memory_db, "A")
        _insert_exec(in_memory_db, "wf-jp")
        in_memory_db.commit()
        fl = _RecordingFL(in_memory_db)

        with patch(_LLM, return_value=_fake_response("not json at all")):
            result = conflict_detection.fire_usage_attribution(
                feedback_loop=fl, workflow_id="wf-jp", domain="d", url="u",
                feedback_text=None, injected_hint_ids=json.dumps([a]),
                case_b=False, prompt_builder=lambda hints: "p",
            )

        assert result == ([], [], [], {})
        assert _hint(in_memory_db, a)["success_count"] == 0
        assert fl.trigger_events[-1]["status"] == "json_parse_failed"

    def test_prompt_builder_fault_is_isolated(self, in_memory_db):
        """F4: a prompt_builder that raises is caught (built inside the try) and
        degrades to skip — no apply, no crash."""
        a = _insert_hint(in_memory_db, "A")
        _insert_exec(in_memory_db, "wf-pb")
        in_memory_db.commit()
        fl = _RecordingFL(in_memory_db)

        def boom(hints):
            raise RuntimeError("builder blew up")

        with patch("src.backend.crew_ai.optimization.conflict_detection.time.sleep"), \
                patch(_LLM) as llm:
            result = conflict_detection.fire_usage_attribution(
                feedback_loop=fl, workflow_id="wf-pb", domain="d", url="u",
                feedback_text=None, injected_hint_ids=json.dumps([a]),
                case_b=False, prompt_builder=boom,
            )

        assert result == ([], [], [], {})
        llm.assert_not_called()  # builder raised before the LLM call
        assert _hint(in_memory_db, a)["success_count"] == 0


# ===================================================================
# F4 layering — optimization must never import services
# ===================================================================

def test_optimization_never_imports_services():
    opt_dir = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src" / "backend" / "crew_ai" / "optimization"
    )
    pattern = re.compile(r"^\s*(?:from|import)\s+src\.backend\.services", re.M)
    offenders = [
        py.name for py in opt_dir.glob("*.py")
        if pattern.search(py.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"optimization must not import services: {offenders}"


# ===================================================================
# Phase 7 — reproduce the motivating bug
# ===================================================================


class TestMotivatingBugReproduction:
    """The exact bug Part 2 fixes: on a passing run, success_count used to
    increment for EVERY injected hint (the old all-injected crediting), not just
    the hints the model actually used. Here 5 hints are injected; the model uses
    2 → only those 2 get success++ (the other 3 get unused++), and a re-run
    credits nothing more (the atomic once-guard)."""

    @staticmethod
    def _counts(conn, hid):
        return conn.execute(
            "SELECT applied_count, success_count, failure_count, unused_count, "
            "       conflict_flagged "
            "FROM nl_feedback_corrections WHERE id = ?", (hid,),
        ).fetchone()

    def test_five_injected_two_used_only_those_two_credited(self, in_memory_db):
        ids = [_insert_hint(in_memory_db, f"hint {i}") for i in range(5)]
        used, unused = ids[:2], ids[2:]
        _insert_exec(in_memory_db, "wf-bug")          # hint_attribution_done = 0
        in_memory_db.commit()
        fl = _RecordingFL(in_memory_db)
        content = json.dumps({"used": used, "unused": unused})

        with patch(_LLM, return_value=_fake_response(content)):
            result = conflict_detection.fire_usage_attribution(
                feedback_loop=fl, workflow_id="wf-bug", domain="d", url="u",
                feedback_text=None, injected_hint_ids=json.dumps(ids),
                case_b=False, prompt_builder=lambda hints: "p",
            )

        assert set(result[0]) == set(used) and set(result[2]) == set(unused)
        assert result[1] == [] and result[3] == {}
        # The 2 USED hints: success++ (under the OLD bug, all 5 would be here).
        for hid in used:
            r = self._counts(in_memory_db, hid)
            assert (r["applied_count"], r["success_count"], r["unused_count"]) == (1, 1, 0)
        # The 3 UNUSED hints: applied++ but success STAYS 0 — the fix.
        for hid in unused:
            r = self._counts(in_memory_db, hid)
            assert (r["applied_count"], r["success_count"], r["unused_count"]) == (1, 0, 1)
        # Telemetry records the split.
        ev = fl.trigger_events[-1]
        assert set(ev["used_hint_ids"]) == set(used)
        assert set(ev["unused_hint_ids"]) == set(unused)

    def test_rerun_credits_exactly_once(self, in_memory_db):
        ids = [_insert_hint(in_memory_db, f"hint {i}") for i in range(5)]
        used, unused = ids[:2], ids[2:]
        _insert_exec(in_memory_db, "wf-bug2")
        in_memory_db.commit()
        fl = _RecordingFL(in_memory_db)
        content = json.dumps({"used": used, "unused": unused})

        with patch(_LLM, return_value=_fake_response(content)):
            for _ in range(2):                        # identical re-attribution
                conflict_detection.fire_usage_attribution(
                    feedback_loop=fl, workflow_id="wf-bug2", domain="d", url="u",
                    feedback_text=None, injected_hint_ids=json.dumps(ids),
                    case_b=False, prompt_builder=lambda hints: "p",
                )

        # The atomic claim (hint_attribution_done) credits the workflow once.
        for hid in used:
            assert self._counts(in_memory_db, hid)["success_count"] == 1   # not 2
        for hid in unused:
            assert self._counts(in_memory_db, hid)["unused_count"] == 1     # not 2

    def test_case_b_five_injected_used_harmful_unused(self, in_memory_db):
        # Case B (fail→edit→pass): the model splits the 5 into used / harmful
        # (removed-and-fixed-it) / unused → success++ / failure+flag / unused++.
        ids = [_insert_hint(in_memory_db, f"hint {i}") for i in range(5)]
        used, harmful, unused = [ids[0]], ids[1], ids[2:]
        _insert_exec(in_memory_db, "wf-bug3")
        in_memory_db.commit()
        fl = _RecordingFL(in_memory_db)
        content = json.dumps({
            "used": used,
            "harmful": [{"id": harmful, "reason": "removed it and the test passed"}],
            "unused": unused,
        })

        with patch(_LLM, return_value=_fake_response(content)):
            conflict_detection.fire_usage_attribution(
                feedback_loop=fl, workflow_id="wf-bug3", domain="d", url="u",
                feedback_text=None, injected_hint_ids=json.dumps(ids),
                case_b=True, prompt_builder=lambda hints: "p",
            )

        assert self._counts(in_memory_db, used[0])["success_count"] == 1
        h = self._counts(in_memory_db, harmful)
        assert h["failure_count"] == 1 and h["conflict_flagged"] == 1
        for hid in unused:
            assert self._counts(in_memory_db, hid)["unused_count"] == 1
