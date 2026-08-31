"""T8 — /api/feedback says what actually happened to the correction.

Every path through this endpoint answered `{"status": "success"}`. The one that
matters is the circuit breaker: `process_user_feedback` returns its fallback
triage before writing anything (the `is_enabled()` check in feedback_loop.py),
the endpoint wrapped that as success, and the user was thanked while their text
was discarded. The same wrapper covered the outer `except` and the case where no
learning record was ever found.

T7 made `process_user_feedback` report which of the four things happened. This
pins the endpoint rendering each one honestly.

Two fields, two jobs, pinned here so the split is deliberate rather than
accidental:

  * `outcome` is the only authority on what happened to the CORRECTION.
  * `status` keeps its existing meaning across this router — could the endpoint
    give an account at all ("success" | "disabled" | "error"). It is "error"
    only for the outcome of the same name, which is the same class of event the
    handler's own `except` already answers that way.

Deliberately NOT tested here, because it is deliberately not built: detecting
paste-and-execute from `test_runs.user_query`. The SPA gates the feedback panel
on a non-empty generated query and on a pass/fail outcome, so neither
paste-and-execute nor an errored run can reach the form — that detection would
target the one case the product already prevents.

Referenced by: src/backend/api/endpoints.py (submit_feedback).
Depends on: src/backend/crew_ai/optimization/feedback_loop.py (the outcome
            vocabulary this renders).
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app_with(loop):
    """The feedback route with the registry and admin check stubbed out.

    Neither is under test here: an unknown run plus AUTH_ENFORCED off is
    `caller_can_access` rule 1 (caller None -> allow) and no re-run redirect,
    which is the plainest path to the body this file is about. Stubbing them
    also keeps the test off the live database.
    """
    registry = MagicMock()
    registry.get_run.return_value = None

    from src.backend.api.endpoints import router
    app = FastAPI()
    app.include_router(router)

    return (
        patch("src.backend.api.endpoints.get_feedback_loop", return_value=loop),
        patch("src.backend.api.endpoints.get_run_registry", return_value=registry),
        patch("src.backend.api.endpoints.is_validated_admin", return_value=False),
        app,
    )


@pytest.fixture
def client_and_loop():
    """The feedback route with a spy learning loop behind it."""
    loop = MagicMock()
    p1, p2, p3, app = _app_with(loop)
    with p1, p2, p3, TestClient(app) as client:
        yield client, loop


def _post(client, text="the search box locator was wrong"):
    return client.post("/api/feedback", json={
        "workflow_id": "wf-honesty",
        "feedback_type": "completely_wrong",
        "feedback_text": text,
    })


def _triage(**extra):
    """What process_user_feedback returns — triage fields plus its outcome."""
    return {
        "category": "locator", "specific_type": "wrong_selector",
        "confidence": 0.9, "taxonomy_code": "L1", "matched_patterns": [],
        **extra,
    }


class TestTheFourOutcomes:
    def test_processed_keeps_todays_fields_and_adds_the_outcome(self, client_and_loop):
        client, loop = client_and_loop
        loop.process_user_feedback.return_value = _triage(outcome="processed")

        body = _post(client).json()

        assert body["status"] == "success"
        assert body["applied_to"] == "wf-honesty"
        assert body["triage"]["category"] == "locator"
        assert body["outcome"] == "processed"
        assert "helps the system learn" in body["message"]

    def test_no_record_says_the_correction_did_not_land(self, client_and_loop):
        """Triage ran and the raw text was queued, but no engine was routed —
        so nothing was learned, and resubmitting is the recovery path (no hint
        was created, so T5's per-run gate cannot block the retry)."""
        client, loop = client_and_loop
        loop.process_user_feedback.return_value = _triage(outcome="no_record")

        body = _post(client).json()

        assert body["outcome"] == "no_record"
        assert body["status"] == "success", (
            "the endpoint gave a truthful account; `outcome` carries what "
            "happened to the correction"
        )
        assert "helps the system learn" not in body["message"]
        assert "again" in body["message"].lower()

    def test_learning_paused_says_nothing_was_recorded(self, client_and_loop):
        """The honesty gap that matters: today the user is thanked while the
        open circuit breaker throws their text away."""
        client, loop = client_and_loop
        loop.process_user_feedback.return_value = _triage(outcome="learning_paused")

        body = _post(client).json()

        assert body["outcome"] == "learning_paused"
        assert body["status"] == "success"
        assert "helps the system learn" not in body["message"]
        assert "paused" in body["message"].lower()
        assert "again" in body["message"].lower()

    def test_error_is_reported_as_an_error(self, client_and_loop):
        """`outcome="error"` is the same class of event the handler's own
        `except` answers with status="error" — the two agree."""
        client, loop = client_and_loop
        loop.process_user_feedback.return_value = _triage(outcome="error")

        body = _post(client).json()

        assert body["outcome"] == "error"
        assert body["status"] == "error"
        assert "helps the system learn" not in body["message"]

    def test_queued_says_it_is_still_being_saved(self, client_and_loop):
        """Task 1: the NL write was submitted (via submit_and_wait) but did
        not confirm within budget — it still runs, so this is not an error."""
        client, loop = client_and_loop
        loop.process_user_feedback.return_value = _triage(outcome="queued")

        body = _post(client).json()

        assert body["outcome"] == "queued"
        assert body["status"] == "success", (
            "nothing failed — confirmation merely did not arrive in budget"
        )
        assert "helps the system learn" not in body["message"]
        assert "do not need to send it again" in body["message"].lower()

    def test_no_text_says_it_was_recorded_but_nothing_was_learned(self, client_and_loop):
        """Task 8: Skip submits empty text, which every engine treats as a
        free no-op — no correction, no evidence, no audit row. The message
        must not repeat the "helps the system learn" claim that used to run
        unconditionally, and — fix round 1 — must not editorialise a verdict
        either: this outcome is also reachable from a PASSING run (Submit
        feedback has no empty-text guard on the close_enough path), so the
        message cannot say "unhelpful" or otherwise imply completely_wrong."""
        client, loop = client_and_loop
        loop.process_user_feedback.return_value = _triage(outcome="no_text")

        body = _post(client, text="")

        body = body.json()
        assert body["outcome"] == "no_text"
        assert body["status"] == "success", (
            "nothing failed — the run was recorded, just without a learned "
            "correction"
        )
        assert "helps the system learn" not in body["message"]
        assert "unhelpful" not in body["message"].lower()
        assert "no description" in body["message"].lower() or \
            "nothing was learned" in body["message"].lower()

    @pytest.mark.parametrize("feedback_type", ["close_enough", "completely_wrong"])
    def test_no_text_message_does_not_vary_by_feedback_type(self, client_and_loop, feedback_type):
        """Fix round 1: the reviewer found the passing (👎 -> close_enough)
        path can also reach `no_text` — "Submit feedback" has no empty-text
        guard on either outcome, only Skip is fail-only. The Step 4b gate in
        feedback_loop.py never reads feedback_type, so the outcome/status
        half of this combination was already correct before this round; only
        the WORDING was wrong ("recorded as unhelpful" on a run that may have
        passed). `_FEEDBACK_OUTCOME_MESSAGES` stays a flat dict keyed by
        outcome alone — the message must be identical for both types."""
        from src.backend.api.endpoints import _FEEDBACK_OUTCOME_MESSAGES

        client, loop = client_and_loop
        loop.process_user_feedback.return_value = _triage(outcome="no_text")

        resp = client.post("/api/feedback", json={
            "workflow_id": "wf-honesty",
            "feedback_type": feedback_type,
            "feedback_text": "",
        })
        body = resp.json()

        assert body["outcome"] == "no_text"
        assert body["status"] == "success"
        assert body["message"] == _FEEDBACK_OUTCOME_MESSAGES["no_text"]
        assert "unhelpful" not in body["message"].lower()

    def test_no_org_says_the_run_has_no_organisation(self, client_and_loop):
        """Task 1, mode (a): an org-less run's correction can never be filed
        where it could be read again, so the NL write is never submitted."""
        client, loop = client_and_loop
        loop.process_user_feedback.return_value = _triage(outcome="no_org")

        body = _post(client).json()

        assert body["outcome"] == "no_org"
        assert body["status"] == "success"
        assert "helps the system learn" not in body["message"]
        assert "organisation" in body["message"].lower()

    def test_no_org_does_not_deny_the_submission_it_just_stored(
        self, client_and_loop,
    ):
        """Step 2's `update_user_feedback` submit is unconditional and runs
        BEFORE the org check, so an org-less run's raw text really is on
        `execution_records.user_feedback`. "was not recorded" contradicts a
        row the owner can go and read; only the CORRECTION is missing."""
        client, loop = client_and_loop
        loop.process_user_feedback.return_value = _triage(outcome="no_org")

        message = _post(client).json()["message"]

        assert "was not recorded" not in message, message
        assert "was sent to be recorded against the run" in message, message
        assert "learning store" in message, message

    def test_error_does_not_claim_the_submission_was_not_recorded(
        self, client_and_loop,
    ):
        """Same overstatement on the error path — with one difference that
        keeps the sentence narrower than no_org's: process_user_feedback's own
        outer `except` also answers "error", and it can fire at Step 1 before
        the raw-feedback submit has happened. So this message says what is
        true on EVERY error path — the correction did not reach the learning
        store — and claims nothing about the run."""
        client, loop = client_and_loop
        loop.process_user_feedback.return_value = _triage(outcome="error")

        message = _post(client).json()["message"]

        assert "was not recorded" not in message, message
        assert "learning store" in message, message
        assert "recorded against the run" not in message, message

    def test_learning_paused_still_says_nothing_was_recorded(
        self, client_and_loop,
    ):
        """The contrast that proves the two rewordings above are not blanket
        edits: the breaker check returns BEFORE the raw-feedback submit, so
        for this outcome nothing at all was written and the flat sentence is
        the true one."""
        client, loop = client_and_loop
        loop.process_user_feedback.return_value = _triage(outcome="learning_paused")

        message = _post(client).json()["message"]

        assert "was not recorded" in message, message

    def test_every_outcome_carries_its_own_message(self, client_and_loop):
        """Derived from the shipped dict, never from a hand-written tuple.

        A tuple here is the drift this pin exists to stop, one level up: the
        branch added `no_text` and the enumeration was not extended, so a
        change that gave `no_text` `processed`'s sentence passed. The sibling
        scan at the end of this file catches a MISSING key; only this test
        catches a DUPLICATED message, and it can only do that if it walks
        every key the endpoint actually ships.
        """
        from src.backend.api.endpoints import _FEEDBACK_OUTCOME_MESSAGES

        client, loop = client_and_loop
        seen = {}
        for outcome in sorted(_FEEDBACK_OUTCOME_MESSAGES):
            loop.process_user_feedback.return_value = _triage(outcome=outcome)
            seen[outcome] = _post(client).json()["message"]

        assert len(seen) == len(_FEEDBACK_OUTCOME_MESSAGES)
        assert len(set(seen.values())) == len(_FEEDBACK_OUTCOME_MESSAGES), seen


class TestTheOutcomeHasExactlyOneHome:
    def test_the_outcome_is_not_left_inside_the_triage_dict(self, client_and_loop):
        """`triage` is the triage result the SPA displays — category,
        confidence, taxonomy. Leaving `outcome` in it too would publish the
        same fact at two addresses, and a caller reading the nested one would
        bind us to both forever."""
        client, loop = client_and_loop
        loop.process_user_feedback.return_value = _triage(outcome="processed")

        body = _post(client).json()

        assert "outcome" not in body["triage"]
        assert body["triage"]["taxonomy_code"] == "L1"

    def test_a_result_with_no_outcome_is_never_reported_as_processed(self, client_and_loop):
        """Absent the field there is no evidence the correction was applied,
        and claiming success on no evidence is the defect this task removes.
        Defaulting the other way would let a future refactor drop the field and
        silently restore the lie."""
        client, loop = client_and_loop
        loop.process_user_feedback.return_value = _triage()

        body = _post(client).json()

        assert body["outcome"] == "error"
        assert body["status"] == "error"
        assert "helps the system learn" not in body["message"]

    def test_an_unrecognised_outcome_is_normalised_rather_than_echoed(self, client_and_loop):
        """The body must not carry an outcome the message cannot describe —
        that is a self-contradicting answer, which is the thing being fixed."""
        client, loop = client_and_loop
        loop.process_user_feedback.return_value = _triage(outcome="processed_v2")

        body = _post(client).json()

        assert body["outcome"] == "error"
        assert body["status"] == "error"


class TestTheVocabularyIsNotTwoVocabularies:
    """T7 recorded the rule deliberately: "`_get_with_retry` returns
    `process_user_feedback`'s OWN outcome codes, not a separate reason
    vocabulary — one caller, one vocabulary, no mapping layer to get wrong."

    The endpoint then had to hold a second copy of those codes, because it is
    the layer that owns the words shown to a person. Drift between the two is
    silent by construction: an outcome the endpoint does not know is
    normalised to "error", so renaming "learning_paused" in the loop would
    make every paused submission say "Something went wrong" instead — a wrong
    statement produced by the honesty change. Nothing else would fail: the
    tests above supply their own literals.

    Read from the loop's SOURCE rather than from a declared tuple, because a
    declaration can drift from the literals it claims to describe while the
    literals are what actually ship. Limitation, stated rather than hidden: an
    outcome introduced through a variable named anything other than `outcome`
    is invisible here — Task 1's "no_org"/"queued"/mapped-"error" outcomes are
    all assigned to a variable literally named `outcome` (feedback_loop.py's
    own return uses `{**triage, "outcome": outcome}`), so the third pattern
    below catches exactly that shape without also catching unrelated
    string-literal assignments elsewhere in the module.
    """

    def test_the_endpoint_can_describe_every_outcome_the_loop_can_return(self):
        import inspect
        import re

        from src.backend.crew_ai.optimization import feedback_loop
        from src.backend.api.endpoints import _FEEDBACK_OUTCOME_MESSAGES

        src = inspect.getsource(feedback_loop)
        produced = set(re.findall(r'"outcome":\s*"(\w+)"', src))
        produced |= set(re.findall(r'return\s+\w+,\s*"(\w+)"', src))
        produced |= set(re.findall(r'\boutcome\s*=\s*"(\w+)"', src))

        assert produced, "the scan found no outcome literals — it has stopped working"
        assert produced == set(_FEEDBACK_OUTCOME_MESSAGES), (
            "the loop and the endpoint disagree about the outcome vocabulary; "
            "loop=%s endpoint=%s" % (sorted(produced), sorted(_FEEDBACK_OUTCOME_MESSAGES))
        )


class TestTheBranchesThatMustNotChange:
    def test_learning_switched_off_answers_exactly_what_it_always_did(self):
        """The disabled early-return is a different statement from the four
        outcomes — the deployment turned learning off, nothing was lost in
        flight — and it fires before the loop is ever called."""
        p1, p2, p3, app = _app_with(None)
        with p1, p2, p3, TestClient(app) as client:
            body = _post(client).json()

        assert body == {
            "status": "disabled",
            "message": "Learning system is not enabled",
        }

    def test_an_over_long_correction_is_still_refused(self, client_and_loop):
        client, loop = client_and_loop
        resp = _post(client, text="x" * 501)
        assert resp.status_code == 400
        assert not loop.process_user_feedback.called

    def test_an_invalid_feedback_type_is_still_refused(self, client_and_loop):
        client, loop = client_and_loop
        resp = client.post("/api/feedback", json={
            "workflow_id": "wf-honesty",
            "feedback_type": "sort_of_ok",
            "feedback_text": "hi",
        })
        assert resp.status_code == 400
        assert not loop.process_user_feedback.called
