"""
Unit tests for the viewport instruction in the assembler's system-prompt
context (BrowserLibraryContext.code_assembly_context).

viewport=None does not escape headless Chromium's 800x600 default window
size (only Playwright's viewport *emulation* layer is disabled) — confirmed
live against nutronsystems.com, where an 800x600 render collapses the nav
into a closed mobile menu and a same-text decoy element gets matched
instead of the real nav item.

Retargeted in Task 24R Stage 1: RobotTasks._cached_viewport (the old pin
target) was removed with the F1 description dedup — code_assembly_context
is now the rule's ONE home in the assembler request.
"""

from src.backend.crew_ai.library_context import BrowserLibraryContext


class TestViewportInstructions:
    def test_context_does_not_instruct_viewport_none(self):
        """The context may EXPLAIN that viewport=None does not work, but must
        never show it as the keyword call to make."""
        context = BrowserLibraryContext().code_assembly_context
        assert "New Context    viewport=None" not in context

    def test_context_instructs_explicit_desktop_size(self):
        context = BrowserLibraryContext().code_assembly_context
        assert "New Context    viewport={'width': 1920, 'height': 1080}" in context
