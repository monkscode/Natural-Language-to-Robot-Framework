"""
Shared LLM conflict-detection pipeline — Trigger 1 and Trigger 2.

Both triggers execute the same sequence: resolve injected hint IDs →
build prompt → call LLM → flag conflicting hints via write queue →
write trigger_events telemetry. This module centralises that sequence.
Also provides shared prompt-building helpers used by both trigger prompt builders.

Referenced by: workflow_service._fire_llm_conflict_detection (Trigger 1)
               workflow_service._build_conflict_prompt (Trigger 1 prompt builder)
               feedback_loop.FeedbackLoop.process_user_feedback (Trigger 2)
               feedback_loop._build_conflict_prompt_with_feedback (Trigger 2 prompt builder)
Depends on: learning_config (_get_conflict_detection_model,
            _get_conflict_detection_completion_kwargs, _call_conflict_detection_llm,
            _parse_conflict_json, _classify_llm_error)
"""

import json
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _hint_line(h: dict) -> str:
    """Format one hint dict into a prompt line, with optional usage metadata.

    Simple form (no metadata): "  [7] use XPath locators"
    With metadata:             "  [7] use XPath locators
                                       applied=5, success=4, failure=1, age=12d"
    """
    line = f"  [{h['id']}] {h['feedback_text']}"
    if h.get("applied_count") is not None:
        applied = h["applied_count"] or 0
        success = h.get("success_count") or 0
        failure = h.get("failure_count") or 0
        age_str = ""
        if h.get("created_at"):
            try:
                created = datetime.fromisoformat(h["created_at"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - created).days
                age_str = f", age={age_days}d"
            except Exception as e:
                logger.debug(
                    "_hint_line: unparseable created_at %r for hint %r: %s",
                    h.get("created_at"), h.get("id"), e,
                )
        line += f"\n        applied={applied}, success={success}, failure={failure}{age_str}"
    return line


def _build_context_prefix(
    domain: str | None,
    url: str | None,
    user_query: str | None,
) -> str:
    """Return the CONTEXT FOR THIS JUDGMENT block shared by both prompt builders.

    Returns a string ending with a single \\n. Callers add one more \\n in the
    prompt string to produce the blank line before the next section.
    """
    context_domain = domain or "unknown"
    context_url = url or "unknown"
    context_query = f'"{user_query[:200]}"' if user_query else '"unknown"'
    return (
        "CONTEXT FOR THIS JUDGMENT:\n"
        f"  Domain: {context_domain}\n"
        f"  URL: {context_url}\n"
        f"  User's request: {context_query}\n"
    )


def fire_conflict_detection(
    *,
    feedback_loop,
    trigger_type: str,
    workflow_id: str,
    domain: str | None,
    url: str | None,
    feedback_text: str | None,
    injected_hint_ids: str | None,
    prompt_builder,
) -> None:
    """Shared conflict-detection pipeline: resolve hints → call LLM → flag
    via write queue → write trigger_events telemetry. Non-blocking; all
    error paths log at WARNING and return without propagating.

    injected_hint_ids tri-branch:
        None   → legacy row (schema < v10): domain-wide fallback query.
        '[]'   → known-empty: nothing was injected; write telemetry, return.
        <list> → fetch exactly those hint IDs from the DB.

    prompt_builder: callable(active_hints: list[dict]) -> str
        Builds the trigger-specific LLM prompt from the resolved hint list.
        Called only when active_hints is non-empty.
    """
    from src.backend.crew_ai.optimization.learning_config import (
        _get_conflict_detection_model,
        _get_conflict_detection_completion_kwargs,
        _call_conflict_detection_llm,
        _parse_conflict_json,
        _classify_llm_error,
    )

    tag = f"[LEARNING:{trigger_type.replace('_', '').upper()}]"

    # Determine which hints to judge and record injected_ids for telemetry.
    # Three branches:
    #   None  → legacy row (schema < v10); fall back to domain-wide query.
    #   '[]'  → known-empty; nothing was injected, skip the LLM call.
    #   else  → fetch only the hints that were actually injected.
    if injected_hint_ids is None:
        active_hints = feedback_loop.nl_engine.get_active_hints_raw(domain, url)
        injected_ids = [h["id"] for h in active_hints]
    else:
        try:
            injected_ids = json.loads(injected_hint_ids)
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "%s malformed injected_hint_ids %r; falling back to domain query",
                tag, injected_hint_ids,
            )
            active_hints = feedback_loop.nl_engine.get_active_hints_raw(domain, url)
            injected_ids = [h["id"] for h in active_hints]
        else:
            if not isinstance(injected_ids, list):
                logger.warning(
                    "%s injected_hint_ids decoded to non-list (%s) %r; "
                    "falling back to domain query",
                    tag, type(injected_ids).__name__, injected_hint_ids,
                )
                active_hints = feedback_loop.nl_engine.get_active_hints_raw(domain, url)
                injected_ids = [h["id"] for h in active_hints]
            elif not injected_ids:
                # Known-empty: '[]' stored → no hints injected → nothing to judge.
                try:
                    feedback_loop.write_queue.submit(
                        feedback_loop.write_trigger_event,
                        trigger_type=trigger_type,
                        workflow_id=workflow_id,
                        domain=domain, url=url,
                        feedback_text=feedback_text,
                        active_hint_ids=[], flagged_hint_ids=[],
                        actually_flagged_hint_ids=[],
                        reason=None, llm_model=None,
                        input_tokens=0, output_tokens=0, llm_latency_ms=0,
                        status="no_active_hints", error_message=None,
                    )
                except Exception as tel_err:
                    logger.warning(
                        "%s telemetry submit failed (non-blocking): %s", tag, tel_err,
                    )
                return
            else:
                active_hints = feedback_loop.nl_engine.get_hints_by_id(injected_ids)

    if not active_hints:
        # No hints available: domain query returned [] or all injected hints
        # were deactivated/flagged since injection time.
        try:
            feedback_loop.write_queue.submit(
                feedback_loop.write_trigger_event,
                trigger_type=trigger_type,
                workflow_id=workflow_id,
                domain=domain, url=url,
                feedback_text=feedback_text,
                active_hint_ids=injected_ids, flagged_hint_ids=[],
                actually_flagged_hint_ids=[],
                reason=None, llm_model=None,
                input_tokens=0, output_tokens=0, llm_latency_ms=0,
                status="no_active_hints", error_message=None,
            )
        except Exception as tel_err:
            logger.warning(
                "%s telemetry submit failed (non-blocking): %s", tag, tel_err,
            )
        return

    prompt = prompt_builder(active_hints)
    model_string = _get_conflict_detection_model()
    extra_kwargs = _get_conflict_detection_completion_kwargs()

    t_start = time.monotonic()
    status = "succeeded"
    error_message: str | None = None
    per_hint_reasons: dict[int, str] = {}
    input_tokens = 0
    output_tokens = 0
    try:
        response = _call_conflict_detection_llm(
            model_string,
            [{"role": "user", "content": prompt}],
            extra_kwargs,
        )
        usage = getattr(response, "usage", None)
        if usage:
            input_tokens = getattr(usage, "prompt_tokens", 0) or 0
            output_tokens = getattr(usage, "completion_tokens", 0) or 0
        try:
            result = _parse_conflict_json(response.choices[0].message.content)
            flag_entries = result.get("flag", [])
            if not isinstance(flag_entries, list):
                flag_entries = []
            shared_reason_fallback = result.get("reason", "")
            for entry in flag_entries:
                if isinstance(entry, dict):
                    hint_id = entry.get("id")
                    reason = entry.get("reason", "") or shared_reason_fallback
                    if isinstance(hint_id, (int, str)):
                        try:
                            per_hint_reasons[int(hint_id)] = str(reason)
                        except (TypeError, ValueError):
                            logger.warning(
                                "%s bad hint id in entry: %r", tag, entry,
                            )
                    else:
                        logger.warning(
                            "%s missing/bad id in entry: %r", tag, entry,
                        )
                elif isinstance(entry, (int, str)):
                    # Backward-compat: bare int from old LLM response format.
                    try:
                        per_hint_reasons[int(entry)] = shared_reason_fallback
                    except (TypeError, ValueError):
                        logger.warning("%s invalid bare ID: %r", tag, entry)
                else:
                    logger.warning("%s non-dict-non-int entry: %r", tag, entry)
            # Membership guard: the LLM must only flag hints it was actually
            # shown. An ID that is valid in the DB but outside this judgment's
            # hint set would otherwise flag an unrelated, innocent hint —
            # conflict_flag_hints does a bare WHERE id = ?. Mirrors the
            # known-hint-ids check _parse_review_response applies on the
            # hint-review path. active_hints is already filtered to
            # is_active=1 AND conflict_flagged=0, so this also guarantees no
            # disabled or already-flagged hint can be (re-)flagged.
            valid_hint_ids = {h["id"] for h in active_hints}
            for stray_id in [hid for hid in per_hint_reasons if hid not in valid_hint_ids]:
                logger.warning(
                    "%s LLM flagged out-of-scope hint id %s — not in this "
                    "judgment's hint set; dropping", tag, stray_id,
                )
                del per_hint_reasons[stray_id]
            # An empty reason does not void the flag — the model still made a
            # judgment; only the justification text failed to serialize. Store
            # an explicit marker so the dashboard shows it was the model, not a
            # framework fault, that withheld the reason, and so the LLM hint
            # review can revisit and refine it later.
            for hid in per_hint_reasons:
                reason = per_hint_reasons[hid]
                if not isinstance(reason, str) or not reason.strip():
                    logger.warning(
                        "%s model returned no reason for flagged hint %s — "
                        "storing explicit marker", tag, hid,
                    )
                    per_hint_reasons[hid] = "no specific reason returned by the model"
            if per_hint_reasons:
                logger.info(
                    "%s Queued conflict-flag for hints "
                    "(will be suspended from injection unless strong-history "
                    "guard protects): %s",
                    tag, list(per_hint_reasons.keys()),
                )
        except (json.JSONDecodeError, ValueError) as parse_err:
            status = "json_parse_failed"
            error_message = f"{type(parse_err).__name__}: {parse_err}"
            logger.warning(
                "%s LLM JSON parse failed (non-blocking): %s", tag, parse_err,
            )
    except Exception as e:
        status = _classify_llm_error(e)
        error_message = f"{type(e).__name__}: {e}"
        logger.warning(
            "%s LLM conflict detection failed (non-blocking): %s", tag, e,
        )
    finally:
        latency_ms = int((time.monotonic() - t_start) * 1000)
        # Combined task: flag hints first (knows which were suppressed by the
        # strong-history guard), then write telemetry using the actual flag
        # set. Keeps flagged_hint_ids (LLM recommendation, read by the weekly
        # review's _compute_exonerations) distinct from
        # actually_flagged_hint_ids (enforcement, read by engagement / reversal
        # KPIs). conflict_flag_hints catches its own exceptions and returns []
        # on rollback, so this closure cannot raise from the flagging step;
        # only a write_trigger_event failure can propagate to the queue
        # worker's per-item handler (acceptable — flag state already in DB).
        def _flag_and_record_telemetry() -> None:
            actually_flagged: list[int] = []
            if per_hint_reasons:
                actually_flagged = feedback_loop.nl_engine.conflict_flag_hints(
                    per_hint_reasons, trigger_type=trigger_type,
                )
            feedback_loop.write_trigger_event(
                trigger_type=trigger_type,
                workflow_id=workflow_id,
                domain=domain, url=url,
                feedback_text=feedback_text,
                active_hint_ids=injected_ids,
                flagged_hint_ids=list(per_hint_reasons.keys()),
                actually_flagged_hint_ids=actually_flagged,
                reason=None,
                llm_model=model_string,
                input_tokens=input_tokens, output_tokens=output_tokens,
                llm_latency_ms=latency_ms,
                status=status, error_message=error_message,
            )

        try:
            feedback_loop.write_queue.submit(_flag_and_record_telemetry)
        except Exception as tel_err:
            logger.warning(
                "%s combined flag+telemetry submit failed (non-blocking): %s",
                tag, tel_err,
            )
