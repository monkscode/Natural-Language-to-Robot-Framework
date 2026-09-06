import asyncio
import base64
import logging
import re
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.backend.core.config import settings
from src.backend.services.workflow_service import stream_generate_and_run, stream_generate_only, stream_execute_only
from src.backend.runner_exec import client as runner_exec_client
from src.backend.runner_exec.client import RunnerExecUnavailable
from src.backend.api.history_endpoints import resolve_robot_code
from src.backend.api.history_scope import history_scope
from src.backend.crew_ai.optimization.learning_registry import get_feedback_loop
from src.backend.crew_ai.optimization.learning_config import MAX_FEEDBACK_TEXT_CHARS
from src.backend.crew_ai.llm_provider_routing import PROVIDER_PREFIXES
# require_user/require_admin enforce JWT (and the admin role) per route.
from src.backend.auth.jwt_utils import require_user, require_admin, is_validated_admin
from src.backend.auth.ownership import caller_can_access, hint_mutation_verdict
from src.backend.core.run_registry import get_run_registry

router = APIRouter()

SSE_MEDIA_TYPE = "text/event-stream"

class Query(BaseModel):
    query: str

class ExecuteRequest(BaseModel):
    robot_code: Optional[str] = None  # The code to execute (omit when rerun_of is set)
    user_query: Optional[str] = None  # Optional: original user query for pattern learning
    workflow_id: Optional[str] = None  # Optional: workflow ID from generation for unified tracking
    rerun_of: Optional[str] = None  # Optional: run id whose STORED code to re-execute (learning skipped)

@router.post('/generate-test')
async def generate_test_only(query: Query, user: dict | None = Depends(require_user)):
    """
    Generate Robot Framework test code without executing it.
    Allows user to review and edit before execution.

    require_user enforces the JWT exactly as before; it is a parameter (not a
    route dependency) so the requester can be recorded on the run's history row.
    """
    user_query = query.query
    if not user_query:
        raise HTTPException(status_code=400, detail="Query not provided")

    model_provider = settings.MODEL_PROVIDER
    model_name = settings.LOCAL_MODEL if model_provider == "local" else settings.ONLINE_MODEL

    if model_provider == "gemini" and not settings.GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY environment variable is not set.")

    logging.info(f"[GENERATE ONLY] Using {model_provider} model provider: {model_name}")

    return StreamingResponse(stream_generate_only(user_query, model_provider, model_name, user=user), media_type=SSE_MEDIA_TYPE)

def _rerun_from_history(source_run_id: str, user: dict | None) -> StreamingResponse:
    """Re-execute a history run's STORED code as a fresh run (History's
    "Run again"): no LLM, no regeneration cost, new run id owned by the
    requester.

    Learning is deliberately skipped (user_query=None): the original execution
    already recorded the (query -> code) evidence, so a passing rerun would
    double-count it and a failing rerun usually means site drift, not bad
    generation. The source's query still lands on the new history row via
    history_query so the run is recognizable in the list.

    The new run stays in the folder the source run sits in until the user
    moves it, inherited from the IMMEDIATE source row — never from rerun_of,
    which is root-flattened below and would send a re-run of a since-moved
    re-run back to the ORIGINAL run's folder.

    Access mirrors the detail endpoint: owner or validated admin; unknown ids
    and other users' runs both 404.
    """
    try:
        source_run_id = str(uuid.UUID(source_run_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid rerun_of: must be a UUID")

    # Read through the caller's OWN scope: the new run inherits the source's
    # folder, and an unscoped read would hand back a folder this caller cannot
    # see — an org_admin re-running a member's run would file it into that
    # member's private folder. history_scope is the single scope computation
    # (never re-derive it here) and carries the re-validated platform-admin
    # flag, so the ownership gate below costs no extra DB round-trip.
    scope = history_scope(user)
    source = get_run_registry().get_run(
        source_run_id, org_id=scope.folder_org_id,
        identified=scope.caller_user_id is not None)
    # A re-run executes a container against the org's environment using the
    # credentials embedded in the stored script, so it ends when the
    # membership does — an ex-member cannot keep firing tests at a customer's
    # systems. That is the ordinary org rule; there is no author-keeps-it
    # exception any more.
    #
    # is_grouped opens it to the rest of the org (decision D5): a test the
    # team published into a folder is there to be re-run by the team. The
    # group_id read here comes from the org-scoped join above, so it is
    # non-NULL only when the folder is one THIS caller's org owns — the same
    # fact the ownership rule needs, already established by the read. A
    # caller with no org owns none, and identified above is what makes the
    # join say so rather than resolving any org's folder.
    allowed = source is not None and caller_can_access(
        user, source.get("user_id"), source.get("org_id"),
        is_platform_admin=scope.is_admin,
        is_grouped=source.get("group_id") is not None,
    )
    if source is None or not allowed:
        # 404, not 403 — don't leak run existence across orgs. The read above
        # is org-scoped, so "no such run" and "not an org you may act in"
        # genuinely collapse here; both answer the same, which is what makes
        # the 404 leak-free rather than merely vague.
        raise HTTPException(status_code=404, detail="Run not found")

    robot_code = resolve_robot_code(source)
    if not robot_code:
        raise HTTPException(
            status_code=409,
            detail="No stored code for this run — it predates code persistence. Use Regenerate instead.",
        )

    # Lineage anchor, root-flattened: re-running a re-run still points at the
    # ORIGINAL run, so /api/feedback always resolves to the row that owns the
    # learning record in one lookup (no chain walking).
    learning_anchor = source.get("rerun_of") or source_run_id

    logging.info(f"[RERUN] Re-executing stored code of run {source_run_id} as a new run")
    return StreamingResponse(
        stream_execute_only(robot_code, user=user,
                            history_query=source.get("user_query"),
                            rerun_of=learning_anchor,
                            group_id=source.get("group_id")),
        media_type=SSE_MEDIA_TYPE,
    )


@router.post('/execute-test')
async def execute_test_only(request: ExecuteRequest, user: dict | None = Depends(require_user)):
    """
    Execute provided Robot Framework test code in Docker container.
    Accepts user-edited or manually-written code.

    Optional: Pass user_query for pattern learning from successful executions.
    Optional: Pass workflow_id for unified ID tracking (same ID for metrics and files).
    Optional: Pass rerun_of (instead of robot_code) to re-execute a history
    run's stored code as a new run — learning is skipped by design.
    """
    if request.rerun_of:
        # Threaded: source-run lookup, admin re-validation and the stored-code
        # disk fallback all block; the StreamingResponse it returns only wraps
        # the (not yet started) async generator, so building it off-loop is safe.
        return await asyncio.to_thread(_rerun_from_history, request.rerun_of, user)

    robot_code = request.robot_code
    user_query = request.user_query  # Optional: for pattern learning
    workflow_id = request.workflow_id  # Optional: for unified ID tracking

    if not robot_code or not robot_code.strip():
        raise HTTPException(status_code=400, detail="Robot code not provided")

    logging.info(f"[EXECUTE ONLY] Executing user-provided test code ({len(robot_code)} characters)")
    if workflow_id:
        safe_workflow_id = (
            workflow_id if re.match(r'^[a-zA-Z0-9_-]+$', workflow_id)
            else "[b64]" + base64.b64encode(workflow_id.encode('UTF-8')).decode()
        )
        logging.info("[EXECUTE ONLY] 🆔 Using unified workflow_id: %s", safe_workflow_id)
    if user_query:
        safe_user_query = user_query.replace("\n", " ").replace("\r", "")
        logging.info(f"[EXECUTE ONLY] ✅ User query provided for pattern learning: {safe_user_query[:50]}...")
    else:
        logging.warning("[EXECUTE ONLY] ⚠️ No user query provided - pattern learning will be skipped")

    return StreamingResponse(stream_execute_only(robot_code, user_query, workflow_id, user=user), media_type=SSE_MEDIA_TYPE)

@router.post('/generate-and-run')
async def generate_and_run_streaming(query: Query, user: dict | None = Depends(require_user)):
    """
    Legacy endpoint: Generate and execute test in one flow.
    Kept for backward compatibility.
    """
    user_query = query.query
    if not user_query:
        raise HTTPException(status_code=400, detail="Query not provided")

    model_provider = settings.MODEL_PROVIDER
    model_name = settings.LOCAL_MODEL if model_provider == "local" else settings.ONLINE_MODEL

    if model_provider == "gemini" and not settings.GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY environment variable is not set.")

    logging.info(f"[GENERATE AND RUN] Using {model_provider} model provider: {model_name}")

    return StreamingResponse(stream_generate_and_run(user_query, model_provider, model_name, user=user), media_type=SSE_MEDIA_TYPE)

@router.post('/rebuild-docker-image', dependencies=[Depends(require_admin)])
async def rebuild_docker_image_endpoint():
    try:
        return await asyncio.to_thread(runner_exec_client.rebuild_image)
    except RunnerExecUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception:
        logging.error("Unexpected error during Docker image rebuild", exc_info=True)
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")

@router.get('/docker-status', dependencies=[Depends(require_user)])
async def docker_status_endpoint():
    try:
        return await asyncio.to_thread(runner_exec_client.docker_status)
    except Exception:
        logging.error("Docker status unavailable", exc_info=True)
        return {"status": "error", "docker_available": False, "error": "Docker is unavailable."}

@router.delete('/test/containers/cleanup', dependencies=[Depends(require_admin)])
async def cleanup_test_containers_endpoint():
    try:
        return await asyncio.to_thread(runner_exec_client.cleanup)
    except Exception as e:
        logging.error(f"Failed to cleanup test containers: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to cleanup test containers: {str(e)}")


# ---------------------------------------------------------------------------
# Learning System Endpoints (DAY_08)
# ---------------------------------------------------------------------------

class FeedbackRequest(BaseModel):
    workflow_id: str
    feedback_text: str = ""
    feedback_type: str  # "close_enough" | "completely_wrong"


# What the response is allowed to claim, keyed by the outcome the learning loop
# reported (process_user_feedback's own vocabulary — one set of strings, no
# translation layer to get wrong). Every one of these paths used to answer
# "Thanks — your feedback helps the system learn", including the four where the
# correction is discarded outright: no_record, learning_paused, no_org, error.
#
# The wording is the backend's, not the SPA's, for the same reason the
# "disabled" and error branches below already carry a `message`: one place says
# what the system did, and an API caller reads the same sentence the panel
# shows.
_FEEDBACK_OUTCOME_MESSAGES = {
    "processed": "Thanks — your feedback helps the system learn.",
    # No trailing "a description can still be sent": the SPA retires the
    # feedback form on this outcome (GeneratePage's neutral branch renders a
    # bare sentence, and the panel never remounts for the run), so that clause
    # offered an action the UI had just removed — on both routes here, Skip
    # included.
    "no_text": (
        "Nothing was learned because no description was given, though this "
        "submission was sent to be recorded against the run."
    ),
    "no_record": (
        "We could not find the learning record this belongs to, so nothing "
        "was learned from it. Please send it again in a moment."
    ),
    "learning_paused": (
        "Learning is paused right now, so this correction was not recorded. "
        "Please send it again later."
    ),
    "queued": (
        "Your correction was received and is still being saved. You do not "
        "need to send it again."
    ),
    # Four clauses, each checked against what the engine can actually see.
    #
    # "already on file": the dedup SELECT matched an existing row in this run's
    # org — the user's words are stored.
    # "currently switched off": is_active = 0 on that row, read in the same
    # transaction, and every retrieval query filters is_active = 1, so the hint
    # reaches no prompt.
    # "did not change it": the run-level gate returned before every effect —
    # evidence_count, last_seen, is_active, conflict_flagged and its metadata,
    # unused_count, and both hint_audit rows. Scoped to the CORRECTION on
    # purpose: the submission's raw text still went to the run's execution
    # record (Step 2's unconditional submit), so an unqualified "changed
    # nothing" would be the same overstatement no_org and error were fixed for.
    # "an organisation admin can switch it back on": reactivate is gated by
    # hint_mutation_verdict with author_tier_applies=False — org_admin or
    # above, within the hint's own org — and ensure_personal_org seats a solo
    # user as org_admin of their personal org, so the sentence is true for a
    # team member and a solo user alike.
    #
    # What it must NOT say, and does not: that the CALLER retracted it. The
    # engine reads one bit, is_active, and a user retract, an admin retract,
    # _auto_disable_hint's unused_count retirement and an LLM review disable
    # are indistinguishable in it. It also does not say "send it again" — that
    # is the one action which provably does nothing on this run, because the
    # claim row gating it is permanent.
    "hint_inactive": (
        "This correction is already on file but is currently switched off, so "
        "this submission did not change it. An organisation admin can switch "
        "it back on."
    ),
    "no_org": (
        "This run is not associated with an organisation, so the correction "
        "could not be filed in the learning store, though this submission was "
        "sent to be recorded against the run."
    ),
    # Deliberately says less than "no_org" above: process_user_feedback's own
    # outer except also answers "error", and it can fire at Step 1 before the
    # raw-feedback submit has run — so this must not promise the text is on
    # the run. "Did not reach the learning store" is true on every path here.
    "error": (
        "Something went wrong, so this correction did not reach the learning "
        "store — please send it again."
    ),
}


async def _gated_feedback_target(
    run_row: dict | None,
    submitted_id: str,
    user: dict | None,
    *,
    is_platform_admin: bool,
    action: str = "submit",
) -> str:
    """The run whose learning record this feedback mutates — gated.

    Re-run rows never own a learning record (their execution deliberately
    skipped learning), so feedback applies to the ORIGINAL run the code was
    cloned from. rerun_of is root-flattened at creation (_rerun_from_history:
    `source.get("rerun_of") or source_run_id`), so there is at most one hop —
    a chain walk here would be dead code.

    submit_feedback's gate cleared the SUBMITTED row, and that is NOT
    authority over the row about to be mutated. It used to be: under
    owner-only re-run, whoever held the copy had already been authorized
    against the original. D5 ended that by opening the re-run of a PUBLISHED
    run to the whole org. A plain member now re-runs a colleague's shared
    test, owns the copy, and would reach the colleague's learning record
    through it; and a platform admin's legitimate cross-org re-run seats a
    copy inside their own org, where that org's org_admin would inherit the
    same reach into the ORIGINAL org's hints. So the same gate is re-applied
    to the original.

    Same two deliberate choices as the caller's gate, for the same reasons:
    the read is UNSCOPED (a platform admin re-running across orgs is
    legitimate, and a caller-org scope would refuse the one caller who is
    entitled), and is_grouped is NOT passed — publication opens reading and
    re-running a test, never rewriting the hints the org's future generations
    receive.

    Fails closed: nothing in src/backend/ ever deletes a run (zero
    `DELETE FROM test_runs`), so a rerun_of that resolves to nothing is an
    anomaly rather than ordinary state, and an unreadable original arrives at
    the gate as owner_id=None/org_id=None — refused by the same line that
    refuses an unknown run.

    Referenced by: submit_feedback, get_run_corrections (this module).
    Depends on: core/run_registry.py, auth/ownership.py.
    """
    rerun_of = run_row.get("rerun_of") if run_row else None
    if not rerun_of:
        return submitted_id

    # Threaded like the caller's lookup: the registry query blocks.
    try:
        original = await asyncio.to_thread(
            lambda: get_run_registry().get_run(rerun_of))
    except Exception:
        original = None
    if original is None:
        logging.warning(
            "[FEEDBACK] run %s points at original %s, which does not resolve",
            submitted_id, rerun_of,
        )
        original = {}

    if not caller_can_access(
        user, original.get("user_id"), original.get("org_id"),
        is_platform_admin=is_platform_admin,
    ):
        raise HTTPException(
            status_code=403,
            detail=f"You cannot {action} feedback for this run",
        )

    logging.info(
        f"[FEEDBACK] {submitted_id} is a re-run — applying feedback "
        f"to its original run {rerun_of}"
    )
    return rerun_of


@router.post('/api/feedback')
async def submit_feedback(request: FeedbackRequest, user: dict | None = Depends(require_user)):
    """
    Submit user feedback on test execution results.

    Triages feedback via NL seed patterns and routes to
    learning engines. Returns triage result for frontend display.

    Two fields, two jobs:

      * `outcome` is the only authority on what happened to the CORRECTION —
        "processed" | "no_text" | "no_record" | "learning_paused" | "no_org" |
        "queued" | "hint_inactive" | "error", straight from
        process_user_feedback. Read this
        one. `_FEEDBACK_OUTCOME_MESSAGES` above is the full list; the two are
        pinned together by test_feedback_response_honesty.py.
      * `status` keeps the meaning it has across this router: could the
        endpoint give an account at all. It is "error" only for the outcome of
        the same name, which is the same class of event the handler's own
        `except` already answers that way.

    `applied_to` names the run this feedback TARGETED (the original, when the
    submitted run is a re-run) — not a claim that anything was applied to it.
    Only `outcome == "processed"` says that.

    Returns 200 with status="disabled" when learning system is off.
    """
    # One PK lookup serves both the ownership gate and the re-run redirect.
    # Threaded: the registry query (and its first-use pool init) blocks.
    try:
        run_row = await asyncio.to_thread(
            lambda: get_run_registry().get_run(request.workflow_id))
    except Exception:
        run_row = None  # registry unavailable — fail closed for non-admins

    # Authorization: feedback mutates the learning store (hints, Case B
    # credits), so only the run's owner — or a validated admin — may submit
    # it. `user` is None only when AUTH_ENFORCED is off (local debugging).
    # Unattributed/unknown runs are admin-only (fail closed).
    #
    # caller_can_ACT, not _read: conflict detection fires with
    # org_id=record.org_id — the RUN's org, never the caller's — so under the
    # read predicate an ex-member kept shaping the hints injected into that
    # org's future generations indefinitely. 403 rather than the re-run path's
    # 404 because THIS lookup is unscoped: an unknown run arrives here as
    # owner_id=None/org_id=None and is refused by the same line with the same
    # status, so the two cases already answer identically and there is no
    # existence to leak.
    #
    # is_grouped is deliberately NOT passed (decision D7): D5 opened the
    # RE-RUN of a published test to the whole org, not its learning record.
    # Feedback rewrites the hints every future generation in the org sees,
    # which is a different power from reading or repeating a colleague's
    # test, and it stays with the run's owner and the org_admin.
    admin = await asyncio.to_thread(is_validated_admin, user)
    owner_id = run_row.get("user_id") if run_row else None
    org_id = run_row.get("org_id") if run_row else None
    if not caller_can_access(user, owner_id, org_id, is_platform_admin=admin):
        raise HTTPException(
            status_code=403,
            detail="You cannot submit feedback for this run",
        )

    # Re-run rows redirect the mutation to their ORIGINAL run, which the
    # gate above has NOT cleared — see _gated_feedback_target, which
    # re-applies it (and refuses with the same 403).
    feedback_target_id = await _gated_feedback_target(
        run_row, request.workflow_id, user, is_platform_admin=admin)

    feedback_loop = get_feedback_loop()
    if not feedback_loop:
        return {
            "status": "disabled",
            "message": "Learning system is not enabled",
        }

    # Validate feedback_type
    if request.feedback_type not in ("close_enough", "completely_wrong"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid feedback_type: {request.feedback_type}. "
                   f"Must be 'close_enough' or 'completely_wrong'.",
        )

    if request.feedback_text and len(request.feedback_text) > MAX_FEEDBACK_TEXT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Feedback must be {MAX_FEEDBACK_TEXT_CHARS} characters or fewer",
        )
    text = request.feedback_text or ""

    # Audit identity for an implicit hint unflag comes from the verified token
    # only (same trust model as _audit_actor), never client input. "unknown" is
    # reachable only with AUTH_ENFORCED off (local dev) — an unidentified human,
    # deliberately not a machine actor like 'system'.
    actor = (user or {}).get("email") or "unknown"
    # The author's STABLE key, threaded beside the email. It is deliberately
    # left None rather than defaulted: "unknown" is a legitimate audit actor
    # string, but the Author permission tier compares ids for equality, so a
    # placeholder id would make every unidentified submitter each other's
    # author.
    actor_user_id = (user or {}).get("user_id") or None

    try:
        # process_user_feedback runs a blocking conflict-detection LLM call
        # (up to 30s). Offload it so this async handler does not freeze the
        # event loop for every other request while it waits.
        result = await asyncio.to_thread(
            feedback_loop.process_user_feedback,
            feedback_target_id, text, request.feedback_type,
            actor=actor,
            actor_user_id=actor_user_id,
        )

        # An outcome this endpoint cannot describe is not evidence that the
        # correction landed, so it is reported as an error rather than echoed
        # back beside a message that contradicts it. Defaulting the other way
        # would let the field go missing one refactor from now and silently
        # restore the "thanks" this endpoint used to give unconditionally.
        outcome = result.get("outcome")
        if outcome not in _FEEDBACK_OUTCOME_MESSAGES:
            outcome = "error"

        # `outcome` is dropped from the triage view. `triage` is the category /
        # confidence the SPA displays; publishing the same fact at two
        # addresses would bind us to both contracts forever.
        return {
            "status": "error" if outcome == "error" else "success",
            "outcome": outcome,
            "message": _FEEDBACK_OUTCOME_MESSAGES[outcome],
            # `actor` and `actor_user_id` are stamped into the triage dict by
            # FeedbackLoop.process_user_feedback as CARRIERS for the engines
            # (ExecutionRecord has no user_id to hang them on) — they are not
            # triage. Dropped here with `outcome` for the same reason: triage
            # is the category / confidence the SPA displays, and echoing the
            # submitter's identity back widens that contract by accident.
            "triage": {
                k: v for k, v in result.items()
                if k not in ("outcome", "actor", "actor_user_id")
            },
            "applied_to": feedback_target_id,
        }
    except Exception as e:
        logging.error(f"[FEEDBACK] Error processing feedback: {e}")
        return {
            "status": "error",
            "message": "Feedback received but triage failed",
        }


@router.get('/api/feedback/{run_id}')
async def get_run_corrections(run_id: str, response: Response,
                              user: dict | None = Depends(require_user)):
    """The corrections this run has already contributed to the learning store.

    T5 made a second submission of the same correction from the same run a
    no-op. The answer to that is not a warning about the ignored duplicate —
    nothing is lost, and this endpoint could not know the outcome anyway (the
    gate runs on the writer thread inside a queued job, long after the
    response is sent). It is visible memory: the user sees their own words on
    file, which is the set of hints this run CREATED plus the ones it
    REINFORCED.

    Gated exactly as POST /api/feedback is, and for a stronger reason than the
    POST has: correction text is user-authored content about a customer's
    site, so a run-id-only read would leak it across orgs. Same unscoped
    lookup, same caller_can_access refusal, same rerun_of redirect. See
    submit_feedback for why the gate is caller_can_ACT and why is_grouped is
    deliberately not passed — publishing a run into a folder publishes the
    test, never the corrections filed against it. The two routes keep separate
    copies of those few lines rather than a shared helper; the agreement is
    pinned by a test that sends the same caller through both.

    `applied_to` names the run these corrections are filed against — the
    ORIGINAL when the requested run is a re-run, matching what the POST
    mutates.

    Degrades to an empty list rather than an error on every internal failure:
    the SPA fetches this the moment a run finishes, and a run that succeeded
    must not render as a broken page. An empty list adds nothing to the panel,
    so silence claims nothing.
    """
    # Same lookup, same threading and the same fail-closed reason as the POST:
    # a registry that cannot answer leaves owner_id/org_id None, which the gate
    # refuses for everyone but a platform admin.
    # Correction text is user-authored content about a customer's site and the
    # gate below is per-caller, so this response must never sit in a private
    # browser cache: after an org move the same run_id would be served from
    # disk without caller_can_access ever running again. Set on the injected
    # Response, which covers every RETURN path (the 'learning disabled' one
    # included). It does NOT reach the 403 raise below — FastAPI's exception
    # handler builds its own response and drops these headers (verified). That
    # is fine and deliberately not worked around: 403 is not in the set of
    # heuristically cacheable statuses (RFC 7231 6.1), and the refusal body
    # carries nothing worth protecting. The header is enforced server-side
    # rather than left to the SPA's fetch options, because the server is the
    # half that binds every client, including one that forgets.
    response.headers["Cache-Control"] = "no-store"

    try:
        run_row = await asyncio.to_thread(
            lambda: get_run_registry().get_run(run_id))
    except Exception:
        run_row = None

    admin = await asyncio.to_thread(is_validated_admin, user)
    owner_id = run_row.get("user_id") if run_row else None
    org_id = run_row.get("org_id") if run_row else None
    if not caller_can_access(user, owner_id, org_id, is_platform_admin=admin):
        raise HTTPException(
            status_code=403,
            detail="You cannot read feedback for this run",
        )

    target_id = await _gated_feedback_target(
        run_row, run_id, user, is_platform_admin=admin, action="read")

    feedback_loop = get_feedback_loop()
    if not feedback_loop:
        return {
            "status": "disabled",
            "message": "Learning system is not enabled",
            "applied_to": target_id,
            "corrections": [],
        }

    engine = getattr(feedback_loop, "nl_engine", None)
    raw_corrections = []
    if engine is not None:
        # Threaded: the read borrows a pooled connection, which blocks.
        raw_corrections = await asyncio.to_thread(
            engine.get_corrections_for_run, target_id)

    # Explicit projection, field by field — never the row dict with keys
    # deleted. org_id and created_by_user_id ride along on each row only to
    # compute can_retract; building the response any other way would let a
    # future column added to that SELECT leak to the client silently.
    # is_active rides along for the same can_retract computation, but it is
    # also safe to forward as active: the caller already reads the hint's
    # own text, and the backend already discloses the same state in the
    # hint_inactive message. can_retract surfaces the Author tier: the
    # feedback panel is the only place a plain org member ever sees their
    # own hint text (list_hints and get_hint stay gated on
    # is_dashboard_viewer), so it is also the only place they can be offered
    # a control to act on it — the client never decides this, it only draws
    # what the server permits.
    #
    # can_retract is an OFFER of an action, not just a permission check, so
    # it is not hint_mutation_verdict alone. get_corrections_for_run stays
    # deliberately unfiltered on is_active (a retracted hint is still the
    # user's own recorded words — see its docstring), and
    # hint_mutation_verdict answers WHO may act on a hint, not whether the
    # hint is still active. Without also requiring is_active, a caller who
    # passes the verdict would be offered a control on an already-retracted
    # hint that can only ever answer "already retracted" — a small
    # dishonesty this codebase's contract does not tolerate: an offered
    # action must not be one the server already knows will no-op.
    corrections = [
        {
            "hint_id": c.get("hint_id"),
            "feedback_text": c.get("feedback_text"),
            "recorded_at": c.get("recorded_at"),
            "active": bool(c.get("is_active")),
            "can_retract": (
                # user is not None FIRST, mirroring _require_caller: this
                # route family does not honour the AUTH_ENFORCED-off escape
                # hatch, but hint_mutation_verdict does (it answers "allow"
                # for a None caller), so without this term the panel drew a
                # Retract control that POST /hints/{id}/retract then 401s.
                # An offered action must not be one the server already knows
                # it will refuse, for the same reason it must not be one the
                # server already knows will no-op.
                user is not None
                and hint_mutation_verdict(
                    user, c.get("org_id"), c.get("created_by_user_id"),
                    is_platform_admin=admin,
                    # The Author tier is opt-in per call site and this is the
                    # one that wants it: the panel's Retract control is exactly
                    # the surface that tier exists for, and POST /hints/{id}/
                    # retract asks for it on the same terms. patch, unflag and
                    # reactivate do not, so the flag must be named here rather
                    # than assumed.
                    author_tier_applies=True,
                ) == "allow"
                and bool(c.get("is_active"))
            ),
        }
        for c in raw_corrections
    ]

    return {
        "status": "success",
        "applied_to": target_id,
        "corrections": corrections,
    }


@router.get('/api/learning-stats', dependencies=[Depends(require_admin)])
async def get_learning_stats():
    """
    Return comprehensive learning system statistics.

    Aggregates stats from all engines, metrics tracker,
    and circuit breaker into a single snapshot.

    Returns 200 with status="disabled" when learning system is off.
    """
    feedback_loop = get_feedback_loop()
    if not feedback_loop:
        return {
            "status": "disabled",
            "message": "Learning system is not enabled",
        }

    try:
        stats = feedback_loop.get_learning_stats()
        return {"status": "success", "stats": stats}
    except Exception as e:
        logging.error(f"[LEARNING-STATS] Error fetching stats: {e}")
        return {
            "status": "error",
            "message": "Failed to retrieve learning stats",
        }
