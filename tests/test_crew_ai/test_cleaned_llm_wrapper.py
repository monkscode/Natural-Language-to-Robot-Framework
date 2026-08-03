"""
Unit tests for get_llm factory in src.backend.crew_ai.cleaned_llm_wrapper.

Purpose: get_llm is the factory that creates the correct LLM wrapper based
         on MODEL_PROVIDER.
"""

import os
import pytest
from unittest.mock import patch


class TestGetLlm:
    """Tests for get_llm factory function."""

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_gemini_returns_cleaned_wrapper(self, MockCleanedLLMWrapper):
        """Gemini provider creates CleanedLLMWrapper."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict('os.environ', {"GEMINI_API_KEY": "test-key"}):
            llm = get_llm(model_provider="gemini", model_name="gemini-2.5-flash")
            assert llm == MockCleanedLLMWrapper.return_value
            MockCleanedLLMWrapper.assert_called_once_with(
                api_key="test-key",
                model="gemini/gemini-2.5-flash",
                num_retries=3,
                is_litellm=True
            )

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_local_returns_ollama_wrapper(self, MockCleanedLLMWrapper):
        """Local provider creates CleanedLLMWrapper with ollama/ prefix and default base URL."""
        import os
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        # Remove OLLAMA_API_BASE so the default "http://localhost:11434" is used
        with patch.dict('os.environ', {}, clear=False):
            os.environ.pop("OLLAMA_API_BASE", None)
            llm = get_llm(model_provider="local", model_name="llama3")
            assert llm == MockCleanedLLMWrapper.return_value
            MockCleanedLLMWrapper.assert_called_once_with(
                model="ollama/llama3",
                base_url="http://localhost:11434",
                is_litellm=True,
                num_retries=3,
            )

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_uses_env_api_key(self, MockCleanedLLMWrapper):
        """Falls back to GEMINI_API_KEY from settings when no explicit key."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict('os.environ', {"GEMINI_API_KEY": "env-key-123"}):
            llm = get_llm(model_provider="gemini", model_name="gemini-2.5-flash")
            assert llm == MockCleanedLLMWrapper.return_value
            MockCleanedLLMWrapper.assert_called_once_with(
                api_key="env-key-123",
                model="gemini/gemini-2.5-flash",
                num_retries=3,
                is_litellm=True
            )

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_get_llm_vertex_strips_gemini_prefix(self, MockCleanedLLMWrapper):
        """Verify get_llm(model_provider='vertex') strips gemini/ and prepends vertex_ai/."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": "creds.json",
                                      "VERTEXAI_PROJECT": "test-project",
                                      "VERTEXAI_LOCATION": "us-central1"}):
            llm = get_llm(model_provider="vertex", model_name="gemini/gemini-2.5-flash")
            assert llm == MockCleanedLLMWrapper.return_value
            MockCleanedLLMWrapper.assert_called_once_with(
                model="vertex_ai/gemini-2.5-flash",
                num_retries=3,
                is_litellm=True,
            )

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_get_llm_vertex_handles_bare_model_name(self, MockCleanedLLMWrapper):
        """Verify get_llm(model_provider='vertex') works with no prefix in model name."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": "creds.json",
                                      "VERTEXAI_PROJECT": "test-project",
                                      "VERTEXAI_LOCATION": "us-central1"}):
            llm = get_llm(model_provider="vertex", model_name="gemini-2.5-flash")
            assert llm == MockCleanedLLMWrapper.return_value
            MockCleanedLLMWrapper.assert_called_once_with(
                model="vertex_ai/gemini-2.5-flash",
                num_retries=3,
                is_litellm=True,
            )

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_get_llm_vertex_does_not_pass_safety_settings_while_parked(self, MockCleanedLLMWrapper):
        """safety_settings is parked (commented out in cleaned_llm_wrapper.py).

        If this test fails it means safety_settings has been re-enabled — update the
        test along with the change and document why in the PR.
        """
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": "creds.json",
                                      "VERTEXAI_PROJECT": "test-project",
                                      "VERTEXAI_LOCATION": "us-central1"}):
            get_llm(model_provider="vertex", model_name="gemini-2.5-flash")

        assert "safety_settings" not in MockCleanedLLMWrapper.call_args.kwargs

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_get_llm_vertex_does_not_pass_api_key(self, MockCleanedLLMWrapper):
        """Verify vertex provider does not inject an api_key (auth is via service account)."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": "creds.json",
                                      "VERTEXAI_PROJECT": "test-project",
                                      "VERTEXAI_LOCATION": "us-central1"}):
            llm = get_llm(model_provider="vertex", model_name="gemini/gemini-2.5-flash")
            assert llm == MockCleanedLLMWrapper.return_value
            MockCleanedLLMWrapper.assert_called_once()
            call_kwargs = MockCleanedLLMWrapper.call_args.kwargs
            assert call_kwargs["model"] == "vertex_ai/gemini-2.5-flash"
            assert "api_key" not in call_kwargs

    def test_get_llm_vertex_disables_thinking_budget(self):
        """Vertex flipped gemini-3.5-flash to server-side thinking-ON (2026-07-18),
        inflating completion tokens 4-6x and burning TPM/RPD quota. crewai's own
        LLM.__init__ has a same-named `thinking` constructor param that is never
        stored or forwarded (verified against the pinned crewai==1.8.1 source), so
        the override must land in additional_params post-construction — that's
        the only path LLM._prepare_completion_params forwards untouched to LiteLLM.

        The guard carries Vertex's own thinkingConfig, not LiteLLM's `thinking`
        shorthand; see test_vertex_thinking_guard_reaches_the_request_body for
        why, and prefer that test — this one only checks that we set the key,
        which says nothing about whether it survives into the request.
        """
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": "creds.json",
                                      "VERTEXAI_PROJECT": "test-project",
                                      "VERTEXAI_LOCATION": "us-central1"}):
            llm = get_llm(model_provider="vertex", model_name="gemini-3.5-flash")

        assert llm.additional_params["thinkingConfig"] == {"thinkingBudget": 0}

    @pytest.mark.parametrize("model_name", ["gemini-3.5-flash", "gemini-2.5-flash"])
    def test_vertex_thinking_guard_reaches_the_request_body(self, model_name):
        """The guard is only worth what LiteLLM actually puts on the wire.

        Asserting on additional_params (the tests above) checks our half of the
        contract and cannot see the other half. litellm 1.94.1 silently stopped
        honouring thinking={'budget_tokens': 0} for gemini-3.5-flash: its
        _map_thinking_param treats any "Gemini 3 or newer" model as thinkingLevel-
        based and emits only includeThoughts=False, so Vertex fell back to
        server-side thinking-ON. Measured cost of that gap: reasoning tokens ate
        the 65,535-token output allowance, truncating the planner's JSON to its
        first step (0 elements identified, vacuous passing tests) or past
        parsing entirely (max_tokens ConverterError, dead run).

        Assert on the BUILT REQUEST BODY, not on get_optional_params. Since the
        guard switched from `thinking` to `thinkingConfig` it no longer passes
        through any mapping: `thinkingConfig` is absent from
        get_supported_openai_params(), so LiteLLM echoes it back verbatim the way
        it echoes any unrecognised vertex kwarg (probe: thinkingConfigTYPO and
        totalNonsenseKey come back untouched). A test on that stage is satisfied
        by its own input and cannot fail.

        The stage that CAN drop it is _transform_request_body, which filters
        optional_params against GenerationConfig.__annotations__ before building
        generationConfig. Drop `thinkingConfig` from that TypedDict — exactly the
        shape of the 1.94.1 change — and the body comes back {} while the
        get_optional_params assertion stays green.

        Importing a private LiteLLM helper is deliberate: if a future bump moves
        or renames it the import fails loudly on the version bump, which is when
        the wire needs re-verifying anyway.
        """
        from litellm.llms.vertex_ai.gemini.transformation import (
            _transform_request_body,
        )

        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": "creds.json",
                                      "VERTEXAI_PROJECT": "test-project",
                                      "VERTEXAI_LOCATION": "us-central1"}):
            llm = get_llm(model_provider="vertex", model_name=model_name)

        body = _transform_request_body(
            messages=[{"role": "user", "content": "ping"}],
            model=model_name,
            optional_params=dict(llm.additional_params),
            custom_llm_provider="vertex_ai",
            litellm_params={},
            cached_content=None,
        )
        generation_config = body.get("generationConfig", {})

        assert generation_config.get("thinkingConfig", {}).get("thinkingBudget") == 0, (
            f"{model_name}: the guard did not survive into the request body as a "
            f"zero thinking budget — generationConfig was {generation_config!r}"
        )

    @pytest.mark.parametrize("model_name", [
        "claude-sonnet-4@20250514",
        "llama-3.1-405b-instruct-maas",
        "mistral-large@2411",
    ])
    def test_non_gemini_vertex_models_get_no_thinking_config(self, model_name):
        """thinkingConfig is a Gemini generationConfig field. Vertex also serves
        Anthropic, Llama and Mistral, and resolve_model_string does not check
        the family — ONLINE_MODEL is a free string, so one config edit reaches
        this branch with a non-Gemini model.

        Switching from `thinking` to `thinkingConfig` removed the loud failure
        that used to cover that. Measured on the pinned litellm 1.75.3:

            thinking       llama/mistral -> UnsupportedParamsError (loud)
                           claude        -> a valid Anthropic thinking param
            thinkingConfig every one     -> silently accepted, Gemini-only
                                            field shipped to a non-Gemini model

        So the guard has to carry its own family check."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": "creds.json",
                                      "VERTEXAI_PROJECT": "test-project",
                                      "VERTEXAI_LOCATION": "us-central1"}):
            llm = get_llm(model_provider="vertex", model_name=model_name)

        assert "thinkingConfig" not in llm.additional_params, (
            f"{model_name} is not a Gemini model — a Gemini-only "
            f"generationConfig field must not be attached to it")

    def test_get_llm_gemini_does_not_disable_thinking_budget(self):
        """The thinking-ON flip is Vertex-specific (probe-verified 2026-07-18) —
        Google AI Studio isn't touched, so gemini provider must stay untouched."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            llm = get_llm(model_provider="gemini", model_name="gemini-3.5-flash")

        assert "thinking" not in llm.additional_params

    def test_get_llm_local_does_not_disable_thinking_budget(self):
        """Ollama models have no Vertex thinking-budget concept — must stay untouched."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        llm = get_llm(model_provider="local", model_name="llama3")

        assert "thinking" not in llm.additional_params

    def test_get_llm_and_the_learning_path_agree_on_the_guard(self):
        """The two LiteLLM call sites must not drift apart on the thinking guard.

        The guard shipped on the agent path only. learning_config calls
        litellm.completion() directly for conflict detection — a live path
        (MODEL_PROVIDER=vertex, ONLINE_MODEL=gemini-3.5-flash,
        OPTIMIZATION_ENABLED=true; 17 [CONFLICT_DETECT] calls in the retained
        log window) that paid for server-side thinking on every call because
        the rule was encoded at one call site instead of in the shared router.

        This test is the anti-drift pin: whatever get_llm attaches, the
        conflict-detection kwargs must carry too.
        """
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm
        from src.backend.crew_ai.optimization.learning_config import (
            _get_conflict_detection_completion_kwargs,
        )
        from src.backend.core import config as config_module

        with patch.dict(os.environ, {"VERTEXAI_CREDENTIALS": "creds.json",
                                      "VERTEXAI_PROJECT": "test-project",
                                      "VERTEXAI_LOCATION": "us-central1"}):
            llm = get_llm(model_provider="vertex", model_name="gemini-3.5-flash")

        with patch.object(config_module.settings, "MODEL_PROVIDER", "vertex"), \
                patch.object(config_module.settings, "ONLINE_MODEL", "gemini-3.5-flash"):
            learning_kwargs = _get_conflict_detection_completion_kwargs()

        assert learning_kwargs.get("thinkingConfig") == llm.additional_params.get("thinkingConfig"), (
            "the conflict-detection path and the agent path disagree on the "
            "Vertex thinking guard — one of them is paying for thinking"
        )

    @pytest.mark.parametrize("bad_provider", ["openai", "anthropic", "gemni", "", "GEMINI", "gpt-4"])
    def test_unsupported_provider_raises_value_error(self, bad_provider):
        """Unknown model_provider values must raise ValueError immediately, not silently route."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with pytest.raises(ValueError, match="Unsupported model_provider"):
            get_llm(model_provider=bad_provider, model_name="some-model")


class TestResolveThinkingKwargs:
    """Contract for the shared Vertex thinking guard in llm_provider_routing.

    The rule is one fact about Vertex billing, consumed by every LiteLLM call
    site. It lives in the router so a second call site cannot be added without
    it — which is exactly how the conflict-detection path ended up unguarded.
    """

    @pytest.mark.parametrize("model_name", ["gemini-3.5-flash", "gemini-2.5-flash"])
    def test_vertex_gemini_gets_a_zero_budget(self, model_name):
        from src.backend.crew_ai.llm_provider_routing import (
            resolve_model_string, resolve_thinking_kwargs,
        )
        routed = resolve_model_string("vertex", model_name)
        assert resolve_thinking_kwargs("vertex", routed) == {
            "thinkingConfig": {"thinkingBudget": 0}
        }

    @pytest.mark.parametrize("model_name", [
        "claude-sonnet-4@20250514",
        "llama-3.1-405b-instruct-maas",
        "mistral-large@2411",
    ])
    def test_non_gemini_vertex_models_get_nothing(self, model_name):
        """thinkingConfig is a Gemini-only generationConfig field, and Vertex
        also serves Anthropic, Llama and Mistral. litellm 1.75.3 accepts the
        key silently for all three, so the guard must carry its own family
        check — there is no loud failure to fall back on."""
        from src.backend.crew_ai.llm_provider_routing import (
            resolve_model_string, resolve_thinking_kwargs,
        )
        routed = resolve_model_string("vertex", model_name)
        assert resolve_thinking_kwargs("vertex", routed) == {}

    def test_family_check_reads_the_routed_string_not_the_bare_name(self):
        """Regression pin for a real divergence, measured 2026-08-03.

        resolve_model_string strips a stale cross-provider prefix, so the bare
        argument 'gemini/claude-sonnet-4@20250514' routes to
        'vertex_ai/claude-sonnet-4@20250514'. A family check on the BARE name
        sees 'gemini' (from the stripped prefix) and returns True; the routed
        string correctly says False. Keying on the bare name would ship a
        Gemini-only field to a Claude endpoint.
        """
        from src.backend.crew_ai.llm_provider_routing import (
            resolve_model_string, resolve_thinking_kwargs,
        )
        bare = "gemini/claude-sonnet-4@20250514"
        routed = resolve_model_string("vertex", bare)

        assert "gemini" in bare.lower()          # the trap
        assert "gemini" not in routed.lower()    # the truth
        assert resolve_thinking_kwargs("vertex", routed) == {}

    @pytest.mark.parametrize("provider,model_name", [
        ("gemini", "gemini-3.5-flash"),
        ("local", "llama3"),
    ])
    def test_non_vertex_providers_get_nothing(self, provider, model_name):
        """The thinking-ON flip is Vertex-specific (probe-verified 2026-07-18).

        Note the gemini case also guards the router's own shape: its routed
        string is 'gemini/gemini-3.5-flash', so the family substring is always
        present. Only the provider gate keeps it out.
        """
        from src.backend.crew_ai.llm_provider_routing import (
            resolve_model_string, resolve_thinking_kwargs,
        )
        routed = resolve_model_string(provider, model_name)
        assert resolve_thinking_kwargs(provider, routed) == {}

    def test_conflict_detection_guard_reaches_the_vertex_request_body(self):
        """The learning path's kwargs are only worth what LiteLLM puts on the wire.

        Same reasoning as test_vertex_thinking_guard_reaches_the_request_body:
        thinkingConfig is absent from get_supported_openai_params(), so
        get_optional_params echoes it back verbatim and a test at that stage is
        satisfied by its own input. _transform_request_body is the stage that
        filters against GenerationConfig.__annotations__ and can actually drop it.
        """
        from litellm.llms.vertex_ai.gemini.transformation import _transform_request_body

        from src.backend.crew_ai.optimization.learning_config import (
            _get_conflict_detection_completion_kwargs,
        )
        from src.backend.core import config as config_module

        with patch.object(config_module.settings, "MODEL_PROVIDER", "vertex"), \
                patch.object(config_module.settings, "ONLINE_MODEL", "gemini-3.5-flash"):
            kwargs = _get_conflict_detection_completion_kwargs()

        body = _transform_request_body(
            messages=[{"role": "user", "content": "ping"}],
            model="gemini-3.5-flash",
            optional_params=dict(kwargs),
            custom_llm_provider="vertex_ai",
            litellm_params={},
            cached_content=None,
        )
        generation_config = body.get("generationConfig", {})

        assert generation_config.get("thinkingConfig", {}).get("thinkingBudget") == 0, (
            "the conflict-detection guard did not survive into the request body "
            f"— generationConfig was {generation_config!r}"
        )

    def test_ollama_api_base_still_threaded_through(self):
        """The guard must not displace the api_base the local provider needs."""
        from src.backend.crew_ai.optimization.learning_config import (
            _get_conflict_detection_completion_kwargs,
        )
        from src.backend.core import config as config_module

        with patch.object(config_module.settings, "MODEL_PROVIDER", "local"), \
                patch.object(config_module.settings, "ONLINE_MODEL", "llama3"):
            kwargs = _get_conflict_detection_completion_kwargs()

        assert "api_base" in kwargs
        assert "thinkingConfig" not in kwargs


class TestGetLlmResponseFormat:
    """Tests for get_llm's response_format pass-through with provider gating (Task 22).

    Contract: response_format (a Pydantic model class) is forwarded to
    CleanedLLMWrapper ONLY when LiteLLM's capability table says the routed
    model supports response schemas. Unsupported or unknown models silently
    fall back to the legacy free-text contract (Option A: Ollama keeps the
    guardrail path).
    """

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_response_format_forwarded_when_supported(self, MockCleanedLLMWrapper):
        """Supported model (vertex gemini): response_format reaches the wrapper."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm
        from src.backend.crew_ai.tasks import PlanOutput

        with patch("litellm.utils.supports_response_schema", return_value=True):
            get_llm(model_provider="vertex", model_name="gemini-2.5-flash",
                    response_format=PlanOutput)

        call_kwargs = MockCleanedLLMWrapper.call_args.kwargs
        assert call_kwargs["response_format"] is PlanOutput

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_response_format_gated_off_when_unsupported(self, MockCleanedLLMWrapper):
        """Unsupported model (ollama): wrapper is built WITHOUT response_format."""
        import os
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm
        from src.backend.crew_ai.tasks import PlanOutput

        os.environ.pop("OLLAMA_API_BASE", None)
        with patch("litellm.utils.supports_response_schema", return_value=False):
            get_llm(model_provider="local", model_name="qwen2.5-coder:14b",
                    response_format=PlanOutput)

        call_kwargs = MockCleanedLLMWrapper.call_args.kwargs
        assert "response_format" not in call_kwargs

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_response_format_gated_off_on_capability_check_error(self, MockCleanedLLMWrapper):
        """Capability check blowing up (unknown model) must NOT break get_llm —
        falls back to legacy contract instead of raising."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm
        from src.backend.crew_ai.tasks import PlanOutput

        with patch("litellm.utils.supports_response_schema",
                   side_effect=Exception("model not in DB")):
            llm = get_llm(model_provider="vertex", model_name="unknown-model-xyz",
                          response_format=PlanOutput)

        assert llm == MockCleanedLLMWrapper.return_value
        call_kwargs = MockCleanedLLMWrapper.call_args.kwargs
        assert "response_format" not in call_kwargs

    @patch("src.backend.crew_ai.cleaned_llm_wrapper.CleanedLLMWrapper")
    def test_no_response_format_keeps_legacy_call_shape(self, MockCleanedLLMWrapper):
        """Callers that don't pass response_format get the exact legacy kwargs
        (no stray response_format=None leaking into the wrapper)."""
        from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

        with patch.dict('os.environ', {"VERTEXAI_PROJECT": "p"}):
            get_llm(model_provider="vertex", model_name="gemini-2.5-flash")

        call_kwargs = MockCleanedLLMWrapper.call_args.kwargs
        assert "response_format" not in call_kwargs
