"""
Shared LLM judgment pipeline — Trigger 2 conflict detection and Part-2 usage
attribution.

Each path resolves injected hint IDs → builds a prompt (via an injected
prompt_builder) → calls the LLM → applies the result via the write queue →
writes trigger_events telemetry. This module centralises that sequence and the
shared prompt-building helpers (_hint_line, _build_context_prefix).

Referenced by: feedback_loop.FeedbackLoop.process_user_feedback (Trigger 2, via
                   fire_conflict_detection)
               feedback_loop._build_conflict_prompt_with_feedback (Trigger 2 prompt builder)
               workflow_service._process_learning (usage attribution, via
                   fire_usage_attribution)
               workflow_service._build_standalone_attribution_prompt /
                   _build_merged_attribution_prompt (attribution prompt builders)
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


def _coerce_attribution_bucket(raw, tag: str) -> tuple[list[int], dict[int, str]]:
    """Coerce one LLM attribution bucket into (ordered unique int ids, reasons).

    Accepts a list of bare ids (int/str) or {"id", "reason"} dicts; anything
    else yields ([], {}). Bad ids are logged and skipped. The reasons map is
    populated only for entries that carried a non-empty reason.
    """
    ids: list[int] = []
    reasons: dict[int, str] = {}
    if not isinstance(raw, list):
        return ids, reasons
    for entry in raw:
        hid = None
        reason = ""
        if isinstance(entry, dict):
            hid = entry.get("id")
            reason = entry.get("reason", "") or ""
        elif isinstance(entry, (int, str)):
            hid = entry
        if hid is None:
            continue
        try:
            hid_int = int(hid)
        except (TypeError, ValueError):
            logger.warning("%s bad hint id in attribution bucket: %r", tag, entry)
            continue
        if hid_int not in ids:
            ids.append(hid_int)
        if reason and str(reason).strip():
            reasons[hid_int] = str(reason)
    return ids, reasons


def _submit_attribution_telemetry(
    feedback_loop, *, trigger_type, workflow_id, domain, url, feedback_text,
    injected_ids, status, error_message, llm_model, input_tokens, output_tokens,
    latency_ms, tag,
) -> None:
    """Best-effort skip/error-path telemetry: a usage-attribution trigger_events
    row with no flags and empty used/unused. The main (apply) path writes its own
    row inside the combined task. Non-blocking."""
    try:
        feedback_loop.write_queue.submit(
            feedback_loop.write_trigger_event,
            trigger_type=trigger_type, workflow_id=workflow_id,
            domain=domain, url=url, feedback_text=feedback_text,
            active_hint_ids=injected_ids, flagged_hint_ids=[],
            actually_flagged_hint_ids=[], reason=None, llm_model=llm_model,
            input_tokens=input_tokens, output_tokens=output_tokens,
            llm_latency_ms=latency_ms, status=status, error_message=error_message,
            used_hint_ids=[], unused_hint_ids=[],
        )
    except Exception as tel_err:
        logger.warning("%s telemetry submit failed (non-blocking): %s", tag, tel_err)


def fire_usage_attribution(
    *,
    feedback_loop,
    workflow_id: str,
    domain: str | None,
    url: str | None,
    feedback_text: str | None,
    injected_hint_ids: str | None,
    case_b: bool,
    prompt_builder,
) -> tuple[list[int], list[int], list[int], dict[int, str]]:
    """Usage-aware hint attribution (Part 2): resolve the injected hints →
    classify each (used / harmful [Case B only] / unused / unsure) with ONE LLM
    judgment → submit a combined apply_hint_attribution + telemetry task to the
    write queue. Non-blocking; every error path logs at WARNING and returns the
    empty result. NEVER calls circuit_breaker (a broken attribution feature must
    not trip the global learning fuse).

    Modeled on fire_conflict_detection, with three deliberate differences:
      - P1: NO scope/domain-wide fallback. None / malformed / non-list / '[]'
        injected_hint_ids → skip (crediting "all in-scope" would pollute counters
        with hints never injected here).
      - P4: each bucket is membership-guarded against the resolved active set S,
        then the buckets are made disjoint (an id in >1 bucket → no-signal).
      - B3: inline bounded retry (up to 2 attempts) on retryable LLM-call errors;
        on exhaustion no apply runs, so the once-guard flag stays unset and a
        later legitimate pass can retry.

    case_b selects response interpretation: True = merged Case-B (the v1→v2 diff
    is in the prompt, so a 'harmful' bucket is allowed and the telemetry row is a
    'trigger_1' event); False = standalone first-pass (no 'harmful' bucket; the
    telemetry row is a 'usage_attribution' event).

    prompt_builder: callable(active_hints: list[dict]) -> str, built on the caller
    side (services layer) and passed in, so optimization never imports services
    (F4 / dependency inversion). It is called INSIDE the try/except so a build
    failure records a status row instead of being silently swallowed.

    Returns (used_ids, failure_ids, unused_ids, reasons) after membership-guard +
    disjointness (the same lists handed to apply_hint_attribution). reasons maps
    each harmful hint id to the LLM's justification (used for the conflict flag).
    """
    from src.backend.crew_ai.optimization.learning_config import (
        _get_conflict_detection_model,
        _get_conflict_detection_completion_kwargs,
        _call_conflict_detection_llm,
        _parse_conflict_json,
        _classify_llm_error,
    )

    tag = "[LEARNING:ATTR]"
    empty: tuple[list[int], list[int], list[int], dict[int, str]] = ([], [], [], {})
    trigger_type = "trigger_1" if case_b else "usage_attribution"

    # P1 — NO scope/domain-wide fallback. The F3 gate already filters these, but
    # this is the independent second layer: skip (no LLM, no write) on any
    # non-list-or-empty injected_hint_ids.
    if injected_hint_ids is None:
        logger.warning(
            "%s injected_hint_ids is None — skipping (no scope-wide fallback)", tag,
        )
        return empty
    try:
        injected_ids = json.loads(injected_hint_ids)
    except (json.JSONDecodeError, ValueError):
        logger.warning(
            "%s malformed injected_hint_ids %r — skipping", tag, injected_hint_ids,
        )
        return empty
    if not isinstance(injected_ids, list) or not injected_ids:
        return empty

    active_hints = feedback_loop.nl_engine.get_hints_by_id(injected_ids)
    if not active_hints:
        # All injected hints disabled/flagged since injection → nothing to judge.
        _submit_attribution_telemetry(
            feedback_loop, trigger_type=trigger_type, workflow_id=workflow_id,
            domain=domain, url=url, feedback_text=feedback_text,
            injected_ids=injected_ids, status="no_active_hints",
            error_message=None, llm_model=None, input_tokens=0, output_tokens=0,
            latency_ms=0, tag=tag,
        )
        return empty

    valid_ids = {h["id"] for h in active_hints}
    model_string = _get_conflict_detection_model()
    extra_kwargs = _get_conflict_detection_completion_kwargs()

    # B3 — inline bounded retry on retryable LLM-call errors. Never credit-all,
    # never touch circuit_breaker.
    max_attempts = 2
    response = None
    status = "succeeded"
    error_message: str | None = None
    input_tokens = output_tokens = 0
    t_start = time.monotonic()
    for attempt in range(max_attempts):
        try:
            # F4 — build the prompt INSIDE the try so a builder failure records a
            # status row (here, classified like an LLM error) rather than being
            # coarsely swallowed.
            prompt = prompt_builder(active_hints)
            response = _call_conflict_detection_llm(
                model_string, [{"role": "user", "content": prompt}], extra_kwargs,
            )
            status = "succeeded"
            error_message = None
            break
        except Exception as e:
            status = _classify_llm_error(e)
            error_message = f"{type(e).__name__}: {e}"
            if attempt + 1 < max_attempts:
                logger.warning(
                    "%s attribution attempt %d failed (%s) — retrying",
                    tag, attempt + 1, e,
                )
                time.sleep(0.5 * (attempt + 1))
                continue
            logger.warning(
                "%s attribution failed after %d attempts (non-blocking): %s",
                tag, max_attempts, e,
            )
    latency_ms = int((time.monotonic() - t_start) * 1000)

    if response is None:
        # Exhausted retries — record telemetry but DO NOT apply, so the once-guard
        # flag stays unset and a later legitimate pass can retry.
        _submit_attribution_telemetry(
            feedback_loop, trigger_type=trigger_type, workflow_id=workflow_id,
            domain=domain, url=url, feedback_text=feedback_text,
            injected_ids=injected_ids, status=status, error_message=error_message,
            llm_model=model_string, input_tokens=0, output_tokens=0,
            latency_ms=latency_ms, tag=tag,
        )
        return empty

    usage = getattr(response, "usage", None)
    if usage:
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0

    try:
        result = _parse_conflict_json(response.choices[0].message.content)
    except (json.JSONDecodeError, ValueError) as parse_err:
        # Unparseable — skip + leave the flag unset (B3: a later pass retries).
        _submit_attribution_telemetry(
            feedback_loop, trigger_type=trigger_type, workflow_id=workflow_id,
            domain=domain, url=url, feedback_text=feedback_text,
            injected_ids=injected_ids, status="json_parse_failed",
            error_message=f"{type(parse_err).__name__}: {parse_err}",
            llm_model=model_string, input_tokens=input_tokens,
            output_tokens=output_tokens, latency_ms=latency_ms, tag=tag,
        )
        logger.warning(
            "%s attribution JSON parse failed (non-blocking): %s", tag, parse_err,
        )
        return empty

    used_ids, _ = _coerce_attribution_bucket(result.get("used"), tag)
    unused_ids, _ = _coerce_attribution_bucket(result.get("unused"), tag)
    if case_b:
        failure_ids, reasons = _coerce_attribution_bucket(result.get("harmful"), tag)
    else:
        failure_ids, reasons = [], {}

    # P4 — membership-guard each bucket against S (the resolved active set). An id
    # valid in the DB but outside this judgment's injected set is dropped.
    used_ids = [i for i in used_ids if i in valid_ids]
    failure_ids = [i for i in failure_ids if i in valid_ids]
    unused_ids = [i for i in unused_ids if i in valid_ids]

    # Disjointness — an id in >1 bucket is a contradiction → no-signal (drop from
    # all). apply_hint_attribution has the same guard (G4) as a backstop.
    used_set = set(used_ids)
    failure_set = set(failure_ids)
    unused_set = set(unused_ids)
    overlap = (
        (used_set & failure_set)
        | (used_set & unused_set)
        | (failure_set & unused_set)
    )
    if overlap:
        logger.warning(
            "%s id(s) classified into >1 bucket — no-signal: %s",
            tag, sorted(overlap),
        )
        used_ids = [i for i in used_ids if i not in overlap]
        failure_ids = [i for i in failure_ids if i not in overlap]
        unused_ids = [i for i in unused_ids if i not in overlap]
    reasons = {i: r for i, r in reasons.items() if i in failure_ids}

    # Submit the combined apply + telemetry task: apply on the writer thread, then
    # telemetry with the ENFORCED flag set (matching _flag_and_record_telemetry).
    # Even an all-empty-but-valid result submits apply so the once-guard flag is
    # set (no-op; don't retry). apply returns the enforced flags, or None on a
    # lost claim (dedup / already-attributed).
    def _attribute_and_record_telemetry() -> None:
        actually_flagged = feedback_loop.nl_engine.apply_hint_attribution(
            workflow_id, used_ids, failure_ids, unused_ids, reasons=reasons,
        )
        feedback_loop.write_trigger_event(
            trigger_type=trigger_type, workflow_id=workflow_id,
            domain=domain, url=url, feedback_text=feedback_text,
            active_hint_ids=injected_ids,
            flagged_hint_ids=failure_ids,
            actually_flagged_hint_ids=actually_flagged or [],
            reason=None, llm_model=model_string,
            input_tokens=input_tokens, output_tokens=output_tokens,
            llm_latency_ms=latency_ms, status=status, error_message=error_message,
            used_hint_ids=used_ids, unused_hint_ids=unused_ids,
        )

    try:
        feedback_loop.write_queue.submit(_attribute_and_record_telemetry)
    except Exception as tel_err:
        logger.warning(
            "%s combined apply+telemetry submit failed (non-blocking): %s",
            tag, tel_err,
        )

    return (used_ids, failure_ids, unused_ids, reasons)
