"""
Unit tests for RobotTasks' viewport instruction block (src.backend.crew_ai.tasks).

viewport=None does not escape headless Chromium's 800x600 default window
size (only Playwright's viewport *emulation* layer is disabled) — confirmed
live against nutronsystems.com, where an 800x600 render collapses the nav
into a closed mobile menu and a same-text decoy element gets matched
instead of the real nav item. The prompt's own hardcoded example/claim must
not contradict the corrected library_context.get_viewport_config_code().
"""

from src.backend.crew_ai.tasks import RobotTasks
from src.backend.crew_ai.library_context import BrowserLibraryContext


class TestViewportInstructions:
    def test_cached_viewport_does_not_instruct_viewport_none(self):
        tasks = RobotTasks(library_context=BrowserLibraryContext())
        assert "viewport=None" not in tasks._cached_viewport

    def test_cached_viewport_instructs_explicit_desktop_size(self):
        tasks = RobotTasks(library_context=BrowserLibraryContext())
        assert "1920" in tasks._cached_viewport
        assert "1080" in tasks._cached_viewport
