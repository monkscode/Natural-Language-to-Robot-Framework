"""
Failure Analyzer — 3-layer hybrid error classifier for Robot Framework output.xml.

Parses Robot Framework's output.xml and classifies every failure into
the taxonomy (A1-E4). Three classification layers:

  Layer 1: Seed regex patterns (~85% coverage, instant, ~0.1ms)
  Layer 2: Learned error mappings from SQLite (Phase 2 placeholder)
  Layer 3: LLM fallback classification (Phase 3 placeholder)

Additionally, CompositeFailureDetector performs cross-signal analysis
combining user query intent with generated code structure to catch
structural logic failures (e.g., missing FOR loop) that regex can't detect.

Design doc reference: Section 7 (Component 2)
Failure taxonomy: docs/tasks/shared/FAILURE_TAXONOMY.md
Depends on: execution_memory.py (DAY_01) for CodeStructureExtractor.
"""

import os
import re
import logging
from dataclasses import dataclass, field
from typing import List, Optional
from xml.etree import ElementTree

from src.backend.crew_ai.optimization.execution_memory import CodeStructureExtractor

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class KeywordResult:
    """One keyword execution extracted from output.xml.

    Maps the ``<kw>`` element structure. The ``owner`` XML attribute
    is stored in the ``library`` field for semantic clarity.
    """

    name: str                  # kw[@name]
    library: str               # kw[@owner] in XML → "library" in code
    status: str                # "PASS" | "FAIL" | "NOT RUN"
    message: str               # Error message from <msg level="FAIL">, else ""
    elapsed_ms: int            # Parsed from status[@elapsed] (seconds → ms)
    arguments: List[str]       # All <arg> child elements


@dataclass
class FailureAnalysis:
    """Result of analyzing a test failure.

    Produced by FailureClassifier, CompositeFailureDetector, or the
    FailureAnalyzer orchestrator.  Downstream consumers (engines,
    feedback loop) use ``category`` + ``specific_type`` for routing.
    """

    category: str              # Taxonomy code: "A1", "B1", "C1", … or "unknown" / "none"
    specific_type: str         # Detailed type within category
    confidence: float          # 0.0–1.0
    source: str                # "regex" | "learned" | "llm" | "composite" | "exit_code_only" | "output_xml" | "none" | "unmatched"
    failed_keyword: Optional[str] = None
    failed_keyword_args: Optional[List[str]] = None
    error_message: Optional[str] = None
    keyword_chain: Optional[List[KeywordResult]] = None


# ---------------------------------------------------------------------------
# OutputXmlParser
# ---------------------------------------------------------------------------

class OutputXmlParser:
    """Parses Robot Framework output.xml to extract keyword chain and failure details.

    output.xml location: ``robot_tests/<run_id>/output.xml`` (shared Docker volume
    or local working directory).

    Graceful degradation: When Robot Framework crashes before writing
    output.xml, the file simply won't exist — callers handle that via
    ``FailureAnalyzer._analyze_without_xml()``.
    """

    def parse(self, output_xml_path: str) -> dict:
        """Extract structured test data from output.xml.

        Returns a dict with:
        - test_status: "passed" | "failed" | "error"
        - keyword_chain: List[KeywordResult]
        - failed_keyword: str | None
        - failed_keyword_args: List[str] | None
        - error_message: str | None
        - test_elapsed_ms: int
        """
        try:
            tree = ElementTree.parse(output_xml_path)
            root = tree.getroot()

            # Navigate: <robot> → <suite> → <test>
            test = root.find(".//test")
            if test is None:
                return {
                    "test_status": "error",
                    "error_message": "No test found in output.xml",
                }

            # Test-level status
            status_elem = test.find("status")
            test_status = (
                "passed"
                if status_elem is not None and status_elem.get("status") == "PASS"
                else "failed"
            )

            # Test elapsed time
            test_elapsed_ms = self._parse_elapsed(status_elem)

            # Extract keyword chain (direct <kw> children + nested)
            keyword_chain = self._extract_keyword_chain(test)

            # Identify the first failed keyword and its error
            failed_kw = None
            error_message = None
            failed_keyword_args = None
            for kw in keyword_chain:
                if kw.status == "FAIL":
                    failed_kw = kw
                    error_message = kw.message
                    failed_keyword_args = kw.arguments
                    break

            # If no keyword-level error found, fall back to test-level status text
            if error_message is None and test_status == "failed" and status_elem is not None:
                error_message = (status_elem.text or "").strip()

            return {
                "test_status": test_status,
                "keyword_chain": keyword_chain,
                "failed_keyword": failed_kw.name if failed_kw else None,
                "failed_keyword_args": failed_keyword_args,
                "error_message": error_message,
                "test_elapsed_ms": test_elapsed_ms,
            }

        except Exception as e:
            logger.warning("[LEARNING] Failed to parse output.xml: %s", e)
            return {"test_status": "error", "error_message": str(e)}

    def _extract_keyword_chain(self, test_element) -> List[KeywordResult]:
        """Walk all ``<kw>`` elements under the test and build KeywordResult list."""
        keyword_chain: List[KeywordResult] = []

        for kw in test_element.findall(".//kw"):
            kw_status_elem = kw.find("status")

            # Status string
            kw_status = "FAIL"
            if kw_status_elem is not None:
                kw_status = kw_status_elem.get("status", "FAIL")

            # Error message: prefer <msg level="FAIL">, fall back to <status> text
            kw_message = ""
            fail_msg = kw.find("msg[@level='FAIL']")
            if fail_msg is not None and fail_msg.text:
                kw_message = fail_msg.text.strip()
            elif kw_status_elem is not None and kw_status_elem.text:
                kw_message = kw_status_elem.text.strip()

            # Elapsed time (seconds float → ms int)
            elapsed_ms = self._parse_elapsed(kw_status_elem)

            # Arguments
            arguments = [
                arg.text for arg in kw.findall("arg") if arg.text
            ]

            keyword_chain.append(KeywordResult(
                name=kw.get("name", "unknown"),
                library=kw.get("owner", ""),   # XML uses "owner", we store as "library"
                status=kw_status,
                message=kw_message,
                elapsed_ms=elapsed_ms,
                arguments=arguments,
            ))

        return keyword_chain

    @staticmethod
    def _parse_elapsed(status_elem) -> int:
        """Convert ``elapsed`` attribute (seconds as float string) to milliseconds int."""
        if status_elem is None:
            return 0
        try:
            return int(float(status_elem.get("elapsed", "0")) * 1000)
        except (ValueError, TypeError):
            return 0


# ---------------------------------------------------------------------------
# FailureClassifier — 3-Layer Hybrid
# ---------------------------------------------------------------------------

class FailureClassifier:
    """3-layer hybrid failure classifier.

    Layer 1: Seed regex patterns (covers ~85% of known RF errors)
    Layer 2: Learned error mappings from SQLite (grows over time — Phase 2)
    Layer 3: LLM fallback (Phase 3 only — returns "unknown" in Phase 1)

    Pattern ordering: More-specific patterns (e.g., ``browser_library_mismatch``)
    are placed BEFORE generic catch-all patterns (e.g., ``wrong_keyword``) so
    the first match wins correctly.
    """

    # Layer 1: Seed regex patterns (from FAILURE_TAXONOMY.md)
    # NOTE: Order matters — more-specific patterns must come first.
    SEED_PATTERNS = {
        # Category A: Structural
        "variable_not_found": {
            "regex": r"Variable '\$\{(\w+)\}' not found",
            "category": "A2",
            "specific_type": "missing_variable_assignment",
        },
        "variable_resolution": {
            "regex": r"Resolving variable '\$\{(\w+)\}' failed",
            "category": "A2",
            "specific_type": "variable_resolution_error",
        },
        "robot_not_generated": {
            "regex": r"Robot Framework reports were not generated",
            "category": "A5",
            "specific_type": "robot_crash_no_output",
        },

        # Category B: Keyword/API
        # B6 MUST come before B1 — "Input Text", "Open Browser", etc. are
        # SeleniumLibrary keywords used with Browser Library (library mismatch),
        # not generic "wrong keyword name" errors.
        "browser_library_mismatch": {
            "regex": r"No keyword with name '(Open Browser|Input Text|Click Element|Close All Browsers)' found",
            "category": "B6",
            "specific_type": "library_mismatch",
        },
        "wrong_keyword": {
            "regex": r"No keyword with name '(.+)' found",
            "category": "B1",
            "specific_type": "wrong_keyword_name",
        },
        "wrong_arg_count": {
            "regex": r"Expected \d+ (?:to \d+ )?arguments?, got \d+",
            "category": "B4",
            "specific_type": "wrong_argument_count",
        },
        "invalid_state": {
            "regex": r"'(.+)' is not a valid (.+)",
            "category": "B3",
            "specific_type": "invalid_parameter_value",
        },
        "unexpected_param": {
            "regex": r"(?:Got unexpected|Unknown) (?:keyword argument|parameter) '(.+)'",
            "category": "B4",
            "specific_type": "unexpected_parameter",
        },

        # Category C: Locator
        "element_not_found": {
            "regex": r"(?:Could not find|Error:.*Timeout.*waiting for) (?:element|selector)",
            "category": "C1",
            "specific_type": "element_not_found",
        },
        "strict_mode": {
            "regex": r"strict mode violation.*resolved to (\d+) elements",
            "category": "C2",
            "specific_type": "multiple_elements_found",
        },
        "not_attached": {
            "regex": r"(?:Element is not attached|Target closed)",
            "category": "C3",
            "specific_type": "stale_element",
        },
        "not_visible": {
            "regex": r"(?:Element is not visible|element is outside of the viewport)",
            "category": "C1",
            "specific_type": "element_not_visible",
        },

        # Category D: Timing
        "timeout": {
            "regex": r"Timeout(?:Error)?(?::\s|\s).*?(\d+(?:\.\d+)?)(?:ms|s)?\s*exceeded",
            "category": "D1",
            "specific_type": "page_load_timeout",
        },
        "page_load_timeout": {
            "regex": r"(?:Navigation|Page load) timeout",
            "category": "D1",
            "specific_type": "page_load_timeout",
        },
        "click_intercepted": {
            "regex": r"(?:click intercepted|Element.*is not clickable at point)",
            "category": "D4",
            "specific_type": "popup_overlay_blocking",
        },
        "not_interactable": {
            "regex": r"Element is not (?:interactable|enabled)",
            "category": "D3",
            "specific_type": "animation_or_disabled",
        },

        # Category E: Assertion
        "should_contain_failed": {
            "regex": r"'(.+)' should contain '(.+)'",
            "category": "E1",
            "specific_type": "assertion_contain_failed",
        },
        "should_be_equal_failed": {
            "regex": r"'(.+)' should be equal to '(.+)'",
            "category": "E1",
            "specific_type": "assertion_equality_failed",
        },
        "should_be_true_failed": {
            "regex": r"'(.+)' should be true",
            "category": "E1",
            "specific_type": "assertion_truth_failed",
        },
    }

    def classify(
        self,
        error_message: str,
        keyword_chain: Optional[List[KeywordResult]] = None,
    ) -> FailureAnalysis:
        """Classify an error using the 3-layer approach.

        Returns FailureAnalysis with category, type, and confidence.
        """
        if not error_message:
            return FailureAnalysis(
                category="unknown",
                specific_type="no_error_message",
                confidence=0.1,
                source="none",
            )

        # Layer 1: Regex patterns
        result = self._check_regex_patterns(error_message)
        if result:
            return result

        # Layer 2: Learned patterns (from SQLite — Phase 2 placeholder)
        result = self._check_learned_patterns(error_message)
        if result:
            return result

        # Layer 3: LLM fallback (Phase 3 — return unknown for now)
        return FailureAnalysis(
            category="unknown",
            specific_type="unclassified",
            confidence=0.3,
            source="unmatched",
            error_message=error_message,
        )

    def _check_regex_patterns(self, error_message: str) -> Optional[FailureAnalysis]:
        """Layer 1: Check seed regex patterns.

        Iterates in insertion order — more-specific patterns are placed
        first in SEED_PATTERNS to ensure correct classification.
        """
        for _pattern_name, pattern_def in self.SEED_PATTERNS.items():
            match = re.search(pattern_def["regex"], error_message)
            if match:
                return FailureAnalysis(
                    category=pattern_def["category"],
                    specific_type=pattern_def["specific_type"],
                    confidence=0.95,
                    source="regex",
                    error_message=error_message,
                )
        return None

    def _check_learned_patterns(  # pragma: no cover — Phase 2 placeholder
        self, error_message: str
    ) -> Optional[FailureAnalysis]:
        """Layer 2: Check learned error patterns from SQLite.

        Phase 2 placeholder — always returns None.  When implemented,
        this will query the ``learned_error_patterns`` table for
        substring-to-category mappings stored by previous LLM
        classifications (Layer 3).

        NOT IMPLEMENTED YET: Remove this comment and the pragma when
        Phase 2 adds the real implementation.
        """
        return None


# ---------------------------------------------------------------------------
# CompositeFailureDetector
# ---------------------------------------------------------------------------

class CompositeFailureDetector:
    """Cross-signal analysis combining query text, code structure, and keyword chain.

    Catches structural logic failures (Category A) that regex can't detect,
    such as "missing FOR loop" (A1) where the code is SYNTACTICALLY valid
    but LOGICALLY wrong.

    Uses multi-word trigger phrases (NOT single keywords) to reduce
    false positives — review finding applied.
    """

    # Multi-word triggers (NOT single keywords — review finding)
    ITERATION_TRIGGERS = [
        "all rows", "each row", "every entry", "every item",
        "all items", "each element", "for each", "iterate over",
        "loop through", "verify all", "check all", "check each",
    ]

    CONDITIONAL_TRIGGERS = [
        "if exists", "only if", "when present", "in case",
        "check if", "verify if", "conditionally",
    ]

    def detect(
        self,
        user_query: str,
        code_structure: str,
        keyword_chain: Optional[List[KeywordResult]] = None,
    ) -> Optional[FailureAnalysis]:
        """Detect structural issues by comparing query intent with code structure.

        Example:
        - Query: "verify all rows show 'Active'" (implies iteration)
        - Code structure: "linear" (no FOR loop)
        - → Category A1: Missing FOR loop
        """
        if not user_query:
            return None

        query_lower = user_query.lower()

        # Check for missing iteration
        needs_iteration = any(t in query_lower for t in self.ITERATION_TRIGGERS)
        if needs_iteration and code_structure == "linear":
            return FailureAnalysis(
                category="A1",
                specific_type="missing_for_loop",
                confidence=0.8,
                source="composite",
                error_message=(
                    f"Query implies iteration ('{user_query}') but code is linear"
                ),
            )

        # Check for missing conditional
        needs_conditional = any(t in query_lower for t in self.CONDITIONAL_TRIGGERS)
        if needs_conditional and code_structure == "linear":
            return FailureAnalysis(
                category="A7",
                specific_type="missing_conditional",
                confidence=0.6,
                source="composite",
            )

        return None


# ---------------------------------------------------------------------------
# FailureAnalyzer — Orchestrator
# ---------------------------------------------------------------------------

class FailureAnalyzer:
    """Main entry point for failure analysis.

    Combines OutputXmlParser, FailureClassifier, and CompositeFailureDetector.

    Graceful degradation:
    - When output.xml exists: Full analysis (regex → learned → composite)
    - When output.xml is missing: Exit-code-only classification (low confidence)
    """

    def __init__(self):
        self.parser = OutputXmlParser()
        self.classifier = FailureClassifier()
        self.composite_detector = CompositeFailureDetector()

    def analyze(
        self,
        output_xml_path: Optional[str],
        user_query: str = "",
        robot_code: str = "",
        exit_code: Optional[int] = None,
    ) -> FailureAnalysis:
        """Analyze a test failure.

        Args:
            output_xml_path: Path to output.xml (``robot_tests/<run_id>/output.xml``).
                            None or non-existent path triggers degraded mode.
            user_query: Original natural language query (for composite detection).
            robot_code: Generated .robot file content (for structure analysis).
            exit_code: Process exit code (used when output.xml is missing).

        Returns:
            FailureAnalysis with category, type, confidence, and source.
        """
        if output_xml_path and os.path.exists(output_xml_path):
            return self._analyze_with_xml(output_xml_path, user_query, robot_code)
        else:
            return self._analyze_without_xml(exit_code)

    def _analyze_with_xml(
        self, output_xml_path: str, user_query: str, robot_code: str
    ) -> FailureAnalysis:
        """Full analysis using output.xml."""
        parsed = self.parser.parse(output_xml_path)

        # Test passed — no failure to classify
        if parsed["test_status"] == "passed":
            return FailureAnalysis(
                category="none",
                specific_type="test_passed",
                confidence=1.0,
                source="output_xml",
            )

        # Step 1: Classify from error message (regex → learned)
        classification = self.classifier.classify(
            parsed.get("error_message", ""),
            parsed.get("keyword_chain", []),
        )

        # Step 2: Composite detection for structural issues
        code_structure = (
            CodeStructureExtractor.detect(robot_code) if robot_code else "unknown"
        )
        composite = self.composite_detector.detect(
            user_query,
            code_structure,
            parsed.get("keyword_chain", []),
        )

        # Step 3: Return highest-confidence result
        if composite and composite.confidence > classification.confidence:
            composite.keyword_chain = parsed.get("keyword_chain")
            composite.failed_keyword = parsed.get("failed_keyword")
            return composite

        classification.keyword_chain = parsed.get("keyword_chain")
        classification.failed_keyword = parsed.get("failed_keyword")
        classification.failed_keyword_args = parsed.get("failed_keyword_args")
        return classification

    def _analyze_without_xml(
        self, exit_code: Optional[int] = None
    ) -> FailureAnalysis:
        """Fallback when output.xml is missing (Robot Framework crashed).

        Low-confidence classifications should NOT generate learning rules —
        they go to execution_records only.
        """
        return FailureAnalysis(
            category="A5",
            specific_type="robot_crash_no_output",
            confidence=0.5,
            source="exit_code_only",
        )
