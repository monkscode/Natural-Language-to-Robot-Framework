"""
Enhanced Locator Generation Tool using Advanced DOM Analysis.

This tool integrates the Similo-based similarity scoring and comprehensive
element fingerprinting to provide robust locator generation for healing.
"""

from services.similarity_scorer import SimilarityScorer, ElementProperties
from services.dom_analyzer import DOMAnalyzer
import json
import logging
from typing import Any, Dict, List, Optional
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

# Import the enhanced services
import sys
from pathlib import Path

# Add the src/backend directory to the path
backend_path = Path(__file__).parent.parent / "src" / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from services.dom_analyzer import DOMAnalyzer
from services.similarity_scorer import SimilarityScorer, ElementProperties


logger = logging.getLogger(__name__)


class ElementFingerprintInput(BaseModel):
    """Input schema for element fingerprint extraction."""
    dom_content: str = Field(..., description="HTML DOM content to analyze")
    locator: str = Field(..., description="Current locator that may be failing (e.g., 'id=submit-btn')")


class MultiLocatorGenerationInput(BaseModel):
    """Input schema for multi-locator generation."""
    dom_content: str = Field(..., description="HTML DOM content to analyze")
    target_element_description: str = Field(
        ..., 
        description="Description of the target element (e.g., 'Submit button with text Log In')"
    )
    original_locator: Optional[str] = Field(
        None,
        description="Original failing locator for context"
    )


class SimilarityMatchingInput(BaseModel):
    """Input schema for similarity-based element matching."""
    target_properties: Dict[str, Any] = Field(
        ...,
        description="Properties of the target element from old version (JSON with keys: tag, id, name, aria_label, visible_text, etc.)"
    )
    current_dom: str = Field(..., description="Current HTML DOM content")
    threshold: float = Field(
        0.7,
        description="Similarity threshold (0.0-1.0, default 0.7). Higher = stricter matching."
    )


class EnhancedLocatorTool(BaseTool):
    """
    Tool for extracting comprehensive element properties and generating multiple alternative locators.

    Features:
    - Extracts 17+ element properties (id, name, aria-label, visible_text, location, etc.)
    - Generates 8 alternative locators with priority ranking
    - Uses Similo algorithm for similarity-based element matching
    - Follows BrowserStack best practices for locator stability
    """

    name: str = "enhanced_locator_tool"
    description: str = """
    Extract comprehensive element properties and generate multiple alternative locators.
    
    Use this tool when:
    - A locator has failed and you need to find the element in updated HTML
    - You need to extract 17+ properties from an element for robust identification
    - You want to generate 8 alternative locators ranked by stability
    - You need to find an element using similarity matching when direct locators fail
    
    Capabilities:
    1. Extract Comprehensive Properties: Get 17+ properties including id, name, aria-label, 
       visible_text, location, dimensions, neighbor_texts, parent_tag, sibling_tags, etc.
    
    2. Generate Multi-Locators: Create 8 alternative locators following BrowserStack hierarchy:
       ID (95% stable) → Name (90%) → Aria-label (92%) → Data-attrs (88%) → 
       Class (70%) → Text-XPath (85%) → Relative-XPath (65%) → Absolute-XPath (50%)
    
    3. Similarity Matching: Find elements using Similo algorithm when original locators break.
       Compares 17+ properties with weighted similarity scoring.
    
    Input format examples:
    - For fingerprinting: {"dom_content": "<html>...", "locator": "id=submit-btn"}
    - For multi-locator generation: {"dom_content": "<html>...", "target_element_description": "Login button"}
    - For similarity matching: {"target_properties": {...}, "current_dom": "<html>...", "threshold": 0.7}
    """

    def __init__(self):
        super().__init__()
        self.dom_analyzer = DOMAnalyzer()
        self.similarity_scorer = SimilarityScorer()

    def _run(self, operation: str, **kwargs) -> str:
        """
        Execute the specified operation.

        Args:
            operation: One of 'extract_properties', 'generate_locators', 'find_by_similarity'
            **kwargs: Operation-specific parameters

        Returns:
            JSON string with operation results
        """
        try:
            if operation == "extract_properties":
                return self._extract_properties(**kwargs)
            elif operation == "generate_locators":
                return self._generate_locators(**kwargs)
            elif operation == "find_by_similarity":
                return self._find_by_similarity(**kwargs)
            else:
                return json.dumps({
                    "error": f"Unknown operation: {operation}",
                    "supported_operations": [
                        "extract_properties",
                        "generate_locators",
                        "find_by_similarity"
                    ]
                })
        except Exception as e:
            logger.error(f"Enhanced locator tool error: {e}", exc_info=True)
            return json.dumps({
                "error": str(e),
                "operation": operation
            })

    def _extract_properties(self, dom_content: str, locator: str) -> str:
        """Extract comprehensive properties from an element."""
        try:
            props = self.dom_analyzer.extract_comprehensive_properties(
                dom_content, locator)

            if props is None:
                return json.dumps({
                    "success": False,
                    "error": "Element not found with provided locator",
                    "locator": locator
                })

            # Convert ElementProperties to dict
            props_dict = {
                "tag": props.tag,
                "id": props.id,
                "name": props.name,
                "type": props.type,
                "aria_label": props.aria_label,
                "class_name": props.class_name,
                "href": props.href,
                "alt": props.alt,
                "src": props.src,
                "role": props.role,
                "absolute_xpath": props.absolute_xpath,
                "relative_xpath": props.relative_xpath,
                "location_x": props.location_x,
                "location_y": props.location_y,
                "width": props.width,
                "height": props.height,
                "area": props.area,
                "aspect_ratio": props.aspect_ratio,
                "visible_text": props.visible_text,
                "placeholder": props.placeholder,
                "value": props.value,
                "neighbor_texts": props.neighbor_texts,
                "parent_tag": props.parent_tag,
                "sibling_tags": props.sibling_tags,
                "is_button": props.is_button,
                "is_clickable": props.is_clickable,
                "is_input": props.is_input,
                "attributes": props.attributes
            }

            return json.dumps({
                "success": True,
                "properties": props_dict,
                "total_properties_extracted": len([v for v in props_dict.values() if v is not None]),
                "locator": locator
            }, indent=2)

        except Exception as e:
            logger.error(f"Property extraction error: {e}", exc_info=True)
            return json.dumps({
                "success": False,
                "error": str(e),
                "locator": locator
            })

    def _generate_locators(self, dom_content: str, target_element_description: str,
                           original_locator: Optional[str] = None) -> str:
        """Generate multiple alternative locators for an element."""
        try:
            # First, try to get properties using original locator if available
            props = None
            if original_locator:
                props = self.dom_analyzer.extract_comprehensive_properties(
                    dom_content, original_locator)

            # If no props yet, try to find element by description
            if props is None:
                # Use basic element extraction from DOM analyzer
                # For now, just use the first relevant element found
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(dom_content, 'html.parser')

                # Try to intelligently find the element based on description
                description_lower = target_element_description.lower()

                # Search strategy based on description keywords
                if "button" in description_lower:
                    elements = soup.find_all(
                        ['button', 'input', 'a'], limit=10)
                elif "input" in description_lower or "field" in description_lower:
                    elements = soup.find_all(['input', 'textarea'], limit=10)
                elif "link" in description_lower:
                    elements = soup.find_all('a', limit=10)
                else:
                    # Generic search
                    elements = soup.find_all(
                        ['button', 'input', 'a', 'div', 'span'], limit=10)

                # Extract properties from first matching element
                if elements:
                    props = self.dom_analyzer._extract_properties_from_element(
                        elements[0])

            if props is None:
                return json.dumps({
                    "success": False,
                    "error": "Could not find element matching description",
                    "description": target_element_description
                })

            # Generate multiple locators
            locators = self.dom_analyzer.generate_multi_locators(
                dom_content, props)

            return json.dumps({
                "success": True,
                "locators": locators,
                "total_alternatives": len(locators),
                "element_description": target_element_description,
                "recommendations": [
                    f"Try locators in priority order (1-8)",
                    f"Primary strategy: {locators[0]['strategy'] if locators else 'N/A'}",
                    f"Most stable: {[loc for loc in locators if loc.get('stability', 0) > 0.85]}",
                    "If all fail, use find_by_similarity operation"
                ]
            }, indent=2)

        except Exception as e:
            logger.error(f"Locator generation error: {e}", exc_info=True)
            return json.dumps({
                "success": False,
                "error": str(e),
                "description": target_element_description
            })

    def _find_by_similarity(self, target_properties: Dict[str, Any],
                            current_dom: str, threshold: float = 0.7) -> str:
        """Find an element using similarity matching."""
        try:
            # Convert dict to ElementProperties (already imported at module level)
            target_props = ElementProperties(
                tag=target_properties.get('tag'),
                id=target_properties.get('id'),
                name=target_properties.get('name'),
                type=target_properties.get('type'),
                aria_label=target_properties.get('aria_label'),
                class_name=target_properties.get('class_name'),
                href=target_properties.get('href'),
                alt=target_properties.get('alt'),
                src=target_properties.get('src'),
                role=target_properties.get('role'),
                absolute_xpath=target_properties.get('absolute_xpath'),
                relative_xpath=target_properties.get('relative_xpath'),
                location_x=target_properties.get('location_x'),
                location_y=target_properties.get('location_y'),
                width=target_properties.get('width'),
                height=target_properties.get('height'),
                visible_text=target_properties.get('visible_text'),
                placeholder=target_properties.get('placeholder'),
                value=target_properties.get('value'),
                neighbor_texts=target_properties.get('neighbor_texts', []),
                parent_tag=target_properties.get('parent_tag'),
                sibling_tags=target_properties.get('sibling_tags', []),
                is_button=target_properties.get('is_button', False),
                is_clickable=target_properties.get('is_clickable', False),
                is_input=target_properties.get('is_input', False),
                attributes=target_properties.get('attributes', {})
            )

            # Find matching element
            result = self.dom_analyzer.find_element_by_similarity(
                target_props, current_dom, threshold)

            if result is None:
                return json.dumps({
                    "success": False,
                    "error": f"No matching element found with similarity >= {threshold}",
                    "threshold": threshold,
                    "recommendation": "Try lowering threshold to 0.6 or 0.55"
                })

            # Convert properties to dict
            matched_props = result['properties']
            matched_props_dict = {
                "tag": matched_props.tag,
                "id": matched_props.id,
                "name": matched_props.name,
                "aria_label": matched_props.aria_label,
                "visible_text": matched_props.visible_text,
                "class_name": matched_props.class_name,
                # Add other important properties
            }

            return json.dumps({
                "success": True,
                "similarity_score": result['similarity_score'],
                "recommended_locator": result['recommended_locator'],
                "all_locators": result['all_locators'],
                "matched_properties": matched_props_dict,
                "threshold": threshold,
                "confidence": "high" if result['similarity_score'] > 0.85 else
                "medium" if result['similarity_score'] > 0.7 else "low"
            }, indent=2)

        except Exception as e:
            logger.error(f"Similarity matching error: {e}", exc_info=True)
            return json.dumps({
                "success": False,
                "error": str(e),
                "threshold": threshold
            })


# Example usage for testing
if __name__ == "__main__":
    tool = EnhancedLocatorTool()

    # Test HTML
    test_html = """
    <html>
        <body>
            <form id="login-form">
                <input type="text" name="username" aria-label="Username" placeholder="Enter username">
                <input type="password" name="password" aria-label="Password" placeholder="Enter password">
                <button id="submit-btn" type="submit" aria-label="Submit login form" data-action="login">
                    Log In
                </button>
            </form>
        </body>
    </html>
    """

    # Test 1: Extract properties
    print("=== TEST 1: Extract Properties ===")
    result1 = tool._run("extract_properties",
                        dom_content=test_html, locator="id=submit-btn")
    print(result1)

    # Test 2: Generate locators
    print("\n=== TEST 2: Generate Locators ===")
    result2 = tool._run("generate_locators",
                        dom_content=test_html,
                        target_element_description="Submit button with text Log In",
                        original_locator="id=submit-btn")
    print(result2)
