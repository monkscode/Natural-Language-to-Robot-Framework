"""
Unit tests for the viewport instruction in the assembler's system-prompt
texts: the main call's base text (assembler_base_context, built from
core_rules) and the dryrun repair call's code_assembly_context.

viewport=None does not escape headless Chromium's 800x600 default window
size (only Playwright's viewport *emulation* layer is disabled) — confirmed
live against nutronsystems.com, where an 800x600 render collapses the nav
into a closed mobile menu and a same-text decoy element gets matched
instead of the real nav item.

Retargeted in Task 24R Stage 1: RobotTasks._cached_viewport (the old pin
target) was removed with the F1 description dedup. The main call's always-on
text is the base text (core_rules); the repair call's is code_assembly_context.
Both must carry the rule.
"""

import pytest

from src.backend.crew_ai.library_context import BrowserLibraryContext
from src.backend.crew_ai.library_context.base import assembler_base_context

_TEXTS = {
    "base_text": lambda: assembler_base_context(BrowserLibraryContext()),
    "repair_text": lambda: BrowserLibraryContext().code_assembly_context,
}


@pytest.fixture(params=sorted(_TEXTS))
def context(request):
    return _TEXTS[request.param]()


class TestViewportInstructions:
    def test_context_does_not_instruct_viewport_none(self, context):
        """The context may EXPLAIN that viewport=None does not work, but must
        never show it as the keyword call to make."""
        assert "New Context    viewport=None" not in context

    def test_context_instructs_explicit_desktop_size(self, context):
        assert "New Context    viewport={'width': 1920, 'height': 1080}" in context
