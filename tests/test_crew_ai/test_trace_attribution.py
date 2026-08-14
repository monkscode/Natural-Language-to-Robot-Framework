"""
Unit tests for LiteLLM trace attribution via call metadata.

Purpose: every llm_traces row must carry the workflow_id of the run that made
         the call. Without it bench/run_bench.py's detach_run
         (DELETE ... WHERE workflow_id = %s) cannot match the row, so bench LLM
         traces leak into the live store permanently.

Why metadata and not OTel baggage: litellm dispatches success callbacks with
executor.submit (litellm/utils.py:1262), i.e. on a thread-pool thread.
baggage.get_baggage() reads contextvars, which do not cross that hop, so the
callback could never see the workflow span reliably. metadata travels inside
the call's own kwargs and is immune to it. Measured on litellm 1.75.3:
metadata arrives as kwargs["litellm_params"]["metadata"].

Baggage is kept as a fallback rather than deleted — some rows do carry a
workflow_id today and the async-callback path that produces them is not fully
characterised.
"""

from unittest.mock import MagicMock, patch

import pytest


WF_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"


def _kwargs_with_metadata(metadata):
    return {"model": "gemini/gemini-2.5-flash", "litellm_params": {"metadata": metadata}}


def _completion_response():
    resp = MagicMock()
    resp.choices = []
    resp.usage = MagicMock(prompt_tokens=10, completion_tokens=2, total_tokens=12)
    resp._hidden_params = {"response_cost": 0.0001}
    return resp


def _run_callback(kwargs):
    """Drive the trace callback with a captured store; return the insert kwargs."""
    from datetime import datetime
    from src.backend.crew_ai import cleaned_llm_wrapper as clw

    store = MagicMock()
    # get_trace_store is imported inside the callback, so patch it at source.
    with patch("src.backend.core.trace_store.get_trace_store", return_value=store):
        clw._litellm_trace_callback(kwargs, _completion_response(),
                                    datetime.now(), datetime.now())
    if not store.insert_litellm_call.called:
        return None
    return store.insert_litellm_call.call_args.kwargs


class TestAttributionFromMetadata:
    def test_workflow_id_read_from_call_metadata(self):
        got = _run_callback(_kwargs_with_metadata({"workflow_id": WF_ID}))
        assert got is not None
        assert got["workflow_id"] == WF_ID

    def test_metadata_wins_over_baggage(self):
        """Metadata is the call's own truth; baggage may belong to whatever
        context the pool thread happens to be carrying."""
        from src.backend.crew_ai import cleaned_llm_wrapper as clw

        with patch.object(clw, "_workflow_id_from_baggage",
                          return_value="stale-baggage-id"):
            got = _run_callback(_kwargs_with_metadata({"workflow_id": WF_ID}))
        assert got["workflow_id"] == WF_ID

    def test_falls_back_to_baggage_when_metadata_absent(self):
        from src.backend.crew_ai import cleaned_llm_wrapper as clw

        with patch.object(clw, "_workflow_id_from_baggage",
                          return_value="baggage-id"):
            got = _run_callback({"model": "m"})
        assert got["workflow_id"] == "baggage-id"

    def test_no_attribution_available_is_not_fatal(self):
        from src.backend.crew_ai import cleaned_llm_wrapper as clw

        with patch.object(clw, "_workflow_id_from_baggage",
                          return_value=None):
            got = _run_callback({"model": "m"})
        assert got["workflow_id"] is None

    def test_malformed_metadata_does_not_raise(self):
        """A non-dict metadata must not take down the callback."""
        got = _run_callback({"model": "m", "litellm_params": {"metadata": "not-a-dict"}})
        assert got is not None  # row still written, just unattributed


class _Stub:
    """Minimal stand-in: set_workflow_id touches only additional_params.

    CleanedLLMWrapper overrides __new__ (it bypasses crewai's LLM.__new__
    routing), so the method is called unbound against this instead of
    constructing a real wrapper — which would need a live provider config.
    """


def _set_workflow_id(stub, workflow_id):
    from src.backend.crew_ai.cleaned_llm_wrapper import CleanedLLMWrapper
    CleanedLLMWrapper.set_workflow_id(stub, workflow_id)


class TestWrapperCarriesTheMetadata:
    """The wrapper is what puts workflow_id on the wire."""

    def test_set_workflow_id_populates_additional_params(self):
        """additional_params is the only attribute crewai forwards untouched to
        litellm (crewai/llm.py:702), so it is the injection point."""
        stub = _Stub()
        stub.additional_params = {}

        _set_workflow_id(stub, WF_ID)

        assert stub.additional_params["metadata"]["workflow_id"] == WF_ID

    def test_set_workflow_id_preserves_existing_additional_params(self):
        """The Vertex thinking guard also lives in additional_params — it must
        survive (llm_provider_routing.resolve_thinking_kwargs)."""
        stub = _Stub()
        stub.additional_params = {"thinkingConfig": {"thinkingBudget": 0}}

        _set_workflow_id(stub, WF_ID)

        assert stub.additional_params["thinkingConfig"] == {"thinkingBudget": 0}
        assert stub.additional_params["metadata"]["workflow_id"] == WF_ID

    def test_empty_workflow_id_writes_nothing(self):
        """run_crew's workflow_id defaults to "" — do not ship an empty label."""
        stub = _Stub()
        stub.additional_params = {}

        _set_workflow_id(stub, "")

        assert "metadata" not in stub.additional_params

    def test_never_raises(self):
        stub = _Stub()  # no additional_params attribute at all
        _set_workflow_id(stub, WF_ID)  # must not raise
