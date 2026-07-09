"""
Unit tests for tools.browser_use_tool.BatchBrowserUseTool.

Focus: the locator_mapping dict-building block (lines 386–404 in _run()) —
the only path that propagates dropdown_framework and select_id from
browser-service results into the LLM-visible locator context.  If these
fields drop here, Tom Select routing fails downstream with no error or
exception — just a silently wrong keyword choice.

All external calls are mocked; no browser-service or network required.
"""

from unittest.mock import patch, MagicMock
import pytest
from tools.browser_use_tool import BatchBrowserUseTool, BrowserUseAPI


# ─── result builders ─────────────────────────────────────────────────────────

def _found(element_id, best_locator, **extra):
    """Build a found element result as browser-service returns it."""
    base = {
        "element_id": element_id,
        "found": True,
        "best_locator": best_locator,
        "all_locators": [best_locator],
        "validation": {},
        "element_info": {},
    }
    base.update(extra)
    return base


def _not_found(element_id, error="Element not found"):
    return {"element_id": element_id, "found": False, "error": error}


def _completed_response(element_results):
    """Build the status payload query_task_status returns on completion."""
    found_count = sum(1 for e in element_results if e.get("found"))
    return {
        "status": "completed",
        "data": {
            "results": {
                "results": element_results,
                "summary": {
                    "total_elements": len(element_results),
                    "successful": found_count,
                    "failed": len(element_results) - found_count,
                    "success_rate": found_count / max(len(element_results), 1),
                    "total_llm_calls": 0,
                    "actual_cost": 0.0,
                    "total_tokens": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cached_tokens": 0,
                    "custom_actions_enabled": False,
                    "element_approach_metrics": [],
                },
                "success": True,
                "execution_time": 1.0,
                "session_id": "test-session",
            }
        },
    }


def _mock_post_202():
    m = MagicMock()
    m.status_code = 202
    m.json.return_value = {"task_id": "test-task-1"}
    return m


# ─── fixture ─────────────────────────────────────────────────────────────────

@pytest.fixture
def tool(mock_settings):
    return BatchBrowserUseTool()


def _run_mapping(tool, element_results):
    """Drive _run() with mocked HTTP and return only the locator_mapping."""
    elements = [{"id": r["element_id"], "description": "test element"} for r in element_results]
    with patch.object(BatchBrowserUseTool, "_health_check_with_retry", return_value=True), \
         patch("tools.browser_use_tool.requests.post", return_value=_mock_post_202()), \
         patch.object(BrowserUseAPI, "query_task_status", return_value=_completed_response(element_results)):
        response = tool._run(elements=elements, url="https://example.com")
    return response["locator_mapping"]


# ─── locator_mapping: found entries ──────────────────────────────────────────

class TestLocatorMappingFoundEntries:

    def test_tom_select_fields_forwarded(self, tool):
        """Both dropdown_framework and select_id must reach locator_mapping
        when browser-service returns them on a found element."""
        mapping = _run_mapping(tool, [_found(
            "elem_1", "css=#pricelist_id-ts-control",
            dropdown_framework="tom-select",
            select_id="pricelist_id",
        )])
        assert mapping["elem_1"]["dropdown_framework"] == "tom-select"
        assert mapping["elem_1"]["select_id"] == "pricelist_id"

    def test_select_id_none_forwarded_for_auto_generated_id(self, tool):
        """When select_id is None (auto-generated TomSelect id the browser-service
        skips), None must be in the mapping — not a missing key."""
        mapping = _run_mapping(tool, [_found(
            "elem_1", "xpath=//div[contains(@class,'ts-control')]",
            dropdown_framework="tom-select",
            select_id=None,
        )])
        assert "select_id" in mapping["elem_1"]
        assert mapping["elem_1"]["select_id"] is None

    def test_dropdown_framework_defaults_to_empty_string_when_absent(self, tool):
        """When browser-service omits dropdown_framework (non-TomSelect element)
        the field must default to '' — the contract IdentifiedElement expects."""
        mapping = _run_mapping(tool, [_found("elem_1", "id=country")])
        assert mapping["elem_1"]["dropdown_framework"] == ""

    def test_select_id_defaults_to_none_when_absent(self, tool):
        """When browser-service omits select_id the field must default to None."""
        mapping = _run_mapping(tool, [_found("elem_1", "id=country")])
        assert mapping["elem_1"]["select_id"] is None

    def test_empty_dropdown_framework_forwarded_as_empty_string(self, tool):
        """An explicit empty string from browser-service must pass through
        unchanged — it signals 'framework detected, but not a special one'."""
        mapping = _run_mapping(tool, [_found(
            "elem_1", "id=country",
            dropdown_framework="",
            select_id=None,
        )])
        assert mapping["elem_1"]["dropdown_framework"] == ""

    def test_datepicker_framework_forwarded(self, tool):
        """Task D repair: browser-service returns top-level
        datepicker_framework='flatpickr' but the locator_mapping builder
        never copied it — the identify agent was instructed to extract a
        key that could not exist, so DATE_PICKER_HANDLING never routed to
        the setDate idiom and Fill Text timed out on readonly inputs.
        Same silent-drop shape as the Tom Select fields this file guards."""
        mapping = _run_mapping(tool, [_found(
            "elem_1", "id=customer_cdr_from_date",
            element_type="date-picker",
            datepicker_framework="flatpickr",
        )])
        assert mapping["elem_1"]["datepicker_framework"] == "flatpickr"

    def test_datepicker_framework_defaults_to_empty_string_when_absent(self, tool):
        """Non-datepicker elements omit the key — must default to ''
        (the contract IdentifiedElement expects)."""
        mapping = _run_mapping(tool, [_found("elem_1", "id=username")])
        assert mapping["elem_1"]["datepicker_framework"] == ""

    def test_found_entry_includes_all_standard_fields(self, tool):
        """A found entry must carry best_locator, all_locators, validation,
        element_info, and found=True alongside the TomSelect fields."""
        mapping = _run_mapping(tool, [_found(
            "elem_1", "id=username",
            all_locators=["id=username", "css=#username"],
            validation={"unique": True},
            element_info={"tagName": "input"},
        )])
        entry = mapping["elem_1"]
        assert entry["best_locator"] == "id=username"
        assert entry["all_locators"] == ["id=username", "css=#username"]
        assert entry["validation"] == {"unique": True}
        assert entry["element_info"] == {"tagName": "input"}
        assert entry["found"] is True


# ─── locator_mapping: not-found entries ──────────────────────────────────────

class TestLocatorMappingNotFoundEntries:

    def test_not_found_entry_has_correct_shape(self, tool):
        """A not-found result must carry found=False and the error string."""
        mapping = _run_mapping(tool, [_not_found("elem_1", "Timeout waiting for element")])
        assert mapping["elem_1"]["found"] is False
        assert "Timeout" in mapping["elem_1"]["error"]

    def test_not_found_entry_excludes_locator_and_tomselect_fields(self, tool):
        """TomSelect fields and locator fields must be absent from not-found
        entries — stale metadata would mislead the Code Assembler."""
        mapping = _run_mapping(tool, [_not_found("elem_1")])
        entry = mapping["elem_1"]
        assert "dropdown_framework" not in entry
        assert "select_id" not in entry
        assert "best_locator" not in entry
        assert "element_info" not in entry

    def test_not_found_error_defaults_when_absent(self, tool):
        """When browser-service omits the error field the default message
        must be used so the LLM always receives a meaningful string."""
        mapping = _run_mapping(tool, [{"element_id": "elem_1", "found": False}])
        assert mapping["elem_1"]["error"] == "Element not found"


# ─── locator_mapping: mixed batch ────────────────────────────────────────────

class TestLocatorMappingMixedBatch:

    def test_mixed_batch_all_entries_present(self, tool):
        """Every element_id in the browser-service response must appear as a
        key in locator_mapping regardless of found status."""
        results = [
            _found("elem_1", "css=#pricelist_id-ts-control",
                   dropdown_framework="tom-select", select_id="pricelist_id"),
            _found("elem_2", "id=country", dropdown_framework="", select_id=None),
            _not_found("elem_3"),
        ]
        mapping = _run_mapping(tool, results)
        assert set(mapping.keys()) == {"elem_1", "elem_2", "elem_3"}

    def test_mixed_batch_tom_select_entry(self, tool):
        """TomSelect entry in a mixed batch must have framework and select_id."""
        results = [
            _found("elem_1", "css=#pricelist_id-ts-control",
                   dropdown_framework="tom-select", select_id="pricelist_id"),
            _found("elem_2", "id=country"),
            _not_found("elem_3"),
        ]
        mapping = _run_mapping(tool, results)
        assert mapping["elem_1"]["dropdown_framework"] == "tom-select"
        assert mapping["elem_1"]["select_id"] == "pricelist_id"
        assert mapping["elem_1"]["found"] is True

    def test_mixed_batch_native_entry(self, tool):
        """Native (non-TomSelect) entry in a mixed batch must have '' framework."""
        results = [
            _found("elem_1", "css=#pricelist_id-ts-control",
                   dropdown_framework="tom-select", select_id="pricelist_id"),
            _found("elem_2", "id=country"),
            _not_found("elem_3"),
        ]
        mapping = _run_mapping(tool, results)
        assert mapping["elem_2"]["dropdown_framework"] == ""
        assert mapping["elem_2"]["select_id"] is None
        assert mapping["elem_2"]["found"] is True

    def test_mixed_batch_not_found_entry(self, tool):
        """Not-found entry in a mixed batch must be isolated from found metadata."""
        results = [
            _found("elem_1", "css=#pricelist_id-ts-control",
                   dropdown_framework="tom-select", select_id="pricelist_id"),
            _found("elem_2", "id=country"),
            _not_found("elem_3"),
        ]
        mapping = _run_mapping(tool, results)
        assert mapping["elem_3"]["found"] is False
        assert "dropdown_framework" not in mapping["elem_3"]
