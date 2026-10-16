"""
One base text for the assembler's main call (owner decision, 2026-10-16).

Before: the assembler's library-knowledge slot forked on learning. Learning ON
(production) sent the zero-context text (intro line + core_rules); learning OFF
(every bench) sent code_assembly_context, so the bench measured a prompt
production never sends. Now both paths send the base text, learning only
appends its keyword block, and the dryrun repair call keeps
code_assembly_context.

ZERO below is the literal frame, written out here — NOT computed by the code
under test. The first three tests pin the bytes production sends today, so they
must pass both before and after the change.
"""

from unittest.mock import MagicMock, patch

from src.backend.crew_ai.library_context import BrowserLibraryContext
from src.backend.crew_ai.optimization.smart_keyword_provider import (
    SmartKeywordProvider,
)

CTX = BrowserLibraryContext()
ZERO = (
    "\nYou are an expert Robot Framework developer using Browser.\n\n"
    + CTX.core_rules
    + "\n"
)
STRUCTURE_MARKER = "--- BROWSER LIBRARY CODE STRUCTURE ---"


def _provider(vector_store):
    return SmartKeywordProvider(
        library_context=CTX,
        pattern_matcher=MagicMock(),
        vector_store=vector_store,
    )


def _backstory(assembler_context):
    """Backstory of the assembler agent, built through the real RobotAgents."""
    from src.backend.crew_ai.agents import RobotAgents
    from src.backend.crew_ai.cleaned_llm_wrapper import CleanedLLMWrapper

    plain_llm = MagicMock(name="plain", spec=CleanedLLMWrapper)
    planner_llm = MagicMock(name="planner", spec=CleanedLLMWrapper)
    plain_llm._monitor = MagicMock(name="monitor")
    plain_llm._token_usage = {}
    with patch("src.backend.crew_ai.agents.get_llm",
               side_effect=[plain_llm, planner_llm]):
        agents = RobotAgents("vertex", "gemini-2.5-flash",
                             library_context=CTX,
                             assembler_context=assembler_context)
    return agents.code_assembler_agent().backstory


def test_learning_on_zero_context_bytes_unchanged():
    provider = _provider(MagicMock())
    assert provider._format_zero_context("assembler") == ZERO


def test_learning_on_predicted_context_bytes_unchanged():
    vs = MagicMock()
    vs.get_keyword_doc.side_effect = lambda lib, name: {
        "name": name, "args": ["a1", "a2", "a3"], "description": "x" * 60,
    }
    provider = _provider(vs)
    assert provider._format_predicted_context(
        ["Click", "Fill Text"], "assembler"
    ) == (
        ZERO
        + "\n**RELEVANT KEYWORDS (from similar queries):**\n"
        + "• Click(a1, a2, ...): " + "x" * 50
        + "\n• Fill Text(a1, a2, ...): " + "x" * 50
        + "\n"
    )


def test_learning_on_backstory_ends_with_the_zero_context():
    assert _backstory(ZERO).endswith("\n\n" + ZERO)


def test_learning_off_backstory_equals_learning_on_zero_context():
    off = _backstory(None)
    assert off == _backstory(ZERO)
    assert STRUCTURE_MARKER not in off


def test_empty_learned_context_falls_back_to_the_base_text():
    assert _backstory("") == _backstory(ZERO)
