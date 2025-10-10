"""
DOM analysis utilities for element fingerprinting.

Enhanced with comprehensive property extraction based on Similo research:
- 17+ element properties for robust identification
- Multiple locator strategy generation
- Advanced similarity-based element matching
"""

import re
import logging
from typing import Dict, List, Optional, Any, Tuple
from bs4 import BeautifulSoup, Tag
from urllib.parse import urljoin

from ..core.models.healing_models import ElementFingerprint, LocatorStrategy

logger = logging.getLogger(__name__)

# Import the new similarity scorer
try:
    from .similarity_scorer import SimilarityScorer, ElementProperties
    SIMILARITY_SCORER_AVAILABLE = True
except ImportError:
    SIMILARITY_SCORER_AVAILABLE = False
    logger.warning("SimilarityScorer not available - advanced matching disabled")


class DOMAnalyzer:
    """
    Utility class for analyzing DOM structure and extracting element context.
    
    Enhanced with comprehensive element fingerprinting based on Similo research:
    - Extracts 17+ properties per element (vs previous 8-10)
    - Implements BrowserStack best practices for locator priorities
    - Supports advanced similarity-based element matching
    """
    
    def __init__(self):
        """Initialize the DOM analyzer."""
        self.locator_patterns = {
            LocatorStrategy.ID: r'id=(["\'])([^"\']+)\1',
            LocatorStrategy.NAME: r'name=(["\'])([^"\']+)\1',
            LocatorStrategy.CSS: r'css=(["\'])([^"\']+)\1',
            LocatorStrategy.XPATH: r'xpath=(["\'])([^"\']+)\1',
            LocatorStrategy.LINK_TEXT: r'link=(["\'])([^"\']+)\1',
            LocatorStrategy.CLASS_NAME: r'class=(["\'])([^"\']+)\1'
        }
        
        # Initialize similarity scorer if available
        self.similarity_scorer = SimilarityScorer() if SIMILARITY_SCORER_AVAILABLE else None
    
    # =================== COMPREHENSIVE ELEMENT FINGERPRINTING ===================
    
    def extract_comprehensive_properties(self, 
                                        dom_content: str, 
                                        locator: str) -> Optional[ElementProperties]:
        """
        Extract comprehensive element properties for advanced fingerprinting.
        
        Based on Similo research, extracts 17+ properties that are most stable
        and unique for element identification across website versions.
        
        Args:
            dom_content: Full DOM content as string
            locator: Original locator used to find the element
            
        Returns:
            ElementProperties object with comprehensive fingerprint, or None if element not found
        """
        try:
            soup = BeautifulSoup(dom_content, 'html.parser')
            element = self._find_element_by_locator(soup, locator)
            
            if not element:
                logger.warning(f"Element not found for locator: {locator}")
                return None
            
            # Extract all properties
            props = ElementProperties()
            
            # Core identifiers (highest stability: 96%+)
            props.tag = element.name or ""
            props.id = element.get('id', "")
            props.name = element.get('name', "")
            props.type = element.get('type', "")
            props.aria_label = element.get('aria-label', "") or element.get('aria-labelledby', "")
            
            # Attribute-based (moderate stability: 60-80%)
            props.class_name = ' '.join(element.get('class', []))
            props.href = element.get('href', "")
            props.alt = element.get('alt', "")
            props.src = element.get('src', "")
            props.role = element.get('role', "")
            
            # Structural paths (dynamic but valuable)
            props.absolute_xpath = self._generate_absolute_xpath(element)
            props.relative_xpath = self._generate_relative_xpath(element)
            
            # Visual properties (from element attributes - would need browser for actual values)
            # These would ideally come from browser-use, but we can extract style hints
            style = element.get('style', "")
            props.location_x = self._extract_style_property(style, 'left', 0)
            props.location_y = self._extract_style_property(style, 'top', 0)
            props.width = self._extract_style_property(style, 'width', 0)
            props.height = self._extract_style_property(style, 'height', 0)
            
            # Content-based (high stability: 80-95%)
            props.visible_text = element.get_text(strip=True)[:200]  # Limit length
            props.placeholder = element.get('placeholder', "")
            props.value = element.get('value', "")
            
            # Contextual information
            props.neighbor_texts = self._extract_neighbor_texts(element)
            props.parent_tag = element.parent.name if element.parent else ""
            props.sibling_tags = self._extract_sibling_tags(element)
            
            # Computed functional properties
            props.is_button = self._is_button_element(element)
            props.is_clickable = self._is_clickable_element(element)
            props.is_input = self._is_input_element(element)
            
            # Full attribute map (for comprehensive comparison)
            props.attributes = dict(element.attrs) if element.attrs else {}
            
            logger.info(f"Extracted comprehensive properties for element: {props.tag}#{props.id or props.name}")
            return props
            
        except Exception as e:
            logger.error(f"Failed to extract comprehensive properties: {e}", exc_info=True)
            return None
    
    def generate_multi_locators(self, 
                                dom_content: str, 
                                element_props: ElementProperties) -> List[Dict[str, Any]]:
        """
        Generate multiple alternative locators for the same element.
        
        Implements BrowserStack best practice hierarchy:
        1. ID (fastest and most reliable)
        2. Name (stable for form elements)
        3. CSS Selector (flexible and performant)
        4. XPath (last resort, most fragile)
        
        Args:
            dom_content: DOM content
            element_props: Comprehensive element properties
            
        Returns:
            List of locator dictionaries with strategy, value, and priority
        """
        locators = []
        
        # Priority 1: ID locator (highest priority)
        if element_props.id:
            locators.append({
                'strategy': 'id',
                'value': element_props.id,
                'priority': 1,
                'stability': 0.95,  # Research-based stability score
                'description': f'ID locator (most reliable)'
            })
        
        # Priority 2: Name locator (high priority for forms)
        if element_props.name:
            locators.append({
                'strategy': 'name',
                'value': element_props.name,
                'priority': 2,
                'stability': 0.90,
                'description': f'Name locator (stable for forms)'
            })
        
        # Priority 3: Aria-label (accessibility-based, very stable)
        if element_props.aria_label:
            locators.append({
                'strategy': 'css',
                'value': f'{element_props.tag}[aria-label="{element_props.aria_label}"]',
                'priority': 3,
                'stability': 0.92,
                'description': f'Aria-label CSS selector (accessible)'
            })
        
        # Priority 4: CSS with unique attributes
        if element_props.attributes:
            # Try data-* attributes (usually stable)
            for attr, value in element_props.attributes.items():
                if attr.startswith('data-') and value:
                    locators.append({
                        'strategy': 'css',
                        'value': f'{element_props.tag}[{attr}="{value}"]',
                        'priority': 4,
                        'stability': 0.88,
                        'description': f'Data attribute CSS selector'
                    })
                    break  # One data attribute is usually enough
        
        # Priority 5: CSS with class (less stable)
        if element_props.class_name:
            classes = element_props.class_name.replace(' ', '.')
            locators.append({
                'strategy': 'css',
                'value': f'{element_props.tag}.{classes}',
                'priority': 5,
                'stability': 0.70,
                'description': f'Class-based CSS selector'
            })
        
        # Priority 6: CSS with text content
        if element_props.visible_text and len(element_props.visible_text) < 50:
            # Use text-based CSS (pseudo-selector)
            locators.append({
                'strategy': 'xpath',
                'value': f'//{element_props.tag}[contains(text(), "{element_props.visible_text[:30]}")]',
                'priority': 6,
                'stability': 0.85,
                'description': f'Text-based XPath'
            })
        
        # Priority 7: Relative XPath (moderate stability)
        if element_props.relative_xpath:
            locators.append({
                'strategy': 'xpath',
                'value': element_props.relative_xpath,
                'priority': 7,
                'stability': 0.65,
                'description': f'Relative XPath'
            })
        
        # Priority 8: Absolute XPath (last resort - most fragile)
        if element_props.absolute_xpath:
            locators.append({
                'strategy': 'xpath',
                'value': element_props.absolute_xpath,
                'priority': 8,
                'stability': 0.50,
                'description': f'Absolute XPath (fragile)'
            })
        
        # Sort by priority (lower number = higher priority)
        locators.sort(key=lambda x: x['priority'])
        
        logger.info(f"Generated {len(locators)} alternative locators for element")
        return locators
    
    def find_element_by_similarity(self,
                                   target_props: ElementProperties,
                                   current_dom: str,
                                   threshold: float = 0.7) -> Optional[Dict[str, Any]]:
        """
        Find element in current DOM using similarity matching.
        
        Uses advanced Similo algorithm to find the best matching element
        when original locators fail.
        
        Args:
            target_props: Properties of the target element (from old version)
            current_dom: Current DOM content
            threshold: Minimum similarity threshold (default 0.7)
            
        Returns:
            Dictionary with matched element info and locator, or None
        """
        if not self.similarity_scorer:
            logger.warning("Similarity scorer not available")
            return None
        
        try:
            soup = BeautifulSoup(current_dom, 'html.parser')
            
            # Find all elements with the same tag (or similar tags)
            candidate_elements = soup.find_all(target_props.tag)
            
            # Extract properties for each candidate
            candidate_props = []
            for elem in candidate_elements:
                try:
                    props = self._extract_properties_from_element(elem)
                    candidate_props.append((elem, props))
                except Exception as e:
                    logger.warning(f"Failed to extract properties from candidate: {e}")
                    continue
            
            if not candidate_props:
                logger.warning("No candidate elements found")
                return None
            
            # Use similarity scorer to find best match
            candidates_only = [props for _, props in candidate_props]
            match_result = self.similarity_scorer.find_best_match(
                target_props, 
                candidates_only,
                threshold=threshold
            )
            
            if not match_result:
                logger.warning(f"No element found with similarity >= {threshold}")
                return None
            
            best_match_props, score = match_result
            
            # Find the corresponding element
            best_match_elem = None
            for elem, props in candidate_props:
                if props == best_match_props:
                    best_match_elem = elem
                    break
            
            if not best_match_elem:
                return None
            
            # Generate new locator for the matched element
            new_locators = self.generate_multi_locators(current_dom, best_match_props)
            best_locator = new_locators[0] if new_locators else None
            
            return {
                'element': best_match_elem,
                'properties': best_match_props,
                'similarity_score': score,
                'recommended_locator': best_locator,
                'all_locators': new_locators
            }
            
        except Exception as e:
            logger.error(f"Failed to find element by similarity: {e}", exc_info=True)
            return None
    
    # =================== HELPER METHODS FOR PROPERTY EXTRACTION ===================
    
    def _extract_properties_from_element(self, element: Tag) -> ElementProperties:
        """Extract ElementProperties from a BeautifulSoup element."""
        props = ElementProperties()
        
        props.tag = element.name or ""
        props.id = element.get('id', "")
        props.name = element.get('name', "")
        props.type = element.get('type', "")
        props.aria_label = element.get('aria-label', "") or element.get('aria-labelledby', "")
        props.class_name = ' '.join(element.get('class', []))
        props.href = element.get('href', "")
        props.alt = element.get('alt', "")
        props.src = element.get('src', "")
        props.role = element.get('role', "")
        props.visible_text = element.get_text(strip=True)[:200]
        props.placeholder = element.get('placeholder', "")
        props.value = element.get('value', "")
        props.neighbor_texts = self._extract_neighbor_texts(element)
        props.parent_tag = element.parent.name if element.parent else ""
        props.sibling_tags = self._extract_sibling_tags(element)
        props.is_button = self._is_button_element(element)
        props.is_clickable = self._is_clickable_element(element)
        props.is_input = self._is_input_element(element)
        props.attributes = dict(element.attrs) if element.attrs else {}
        
        return props
    
    def _generate_absolute_xpath(self, element: Tag) -> str:
        """Generate absolute XPath for an element."""
        try:
            path_parts = []
            current = element
            
            while current and current.name and current.name != '[document]':
                siblings = [s for s in current.parent.children if hasattr(s, 'name') and s.name == current.name] if current.parent else []
                
                if len(siblings) == 1:
                    path_parts.append(current.name)
                else:
                    position = siblings.index(current) + 1 if siblings else 1
                    path_parts.append(f"{current.name}[{position}]")
                
                current = current.parent
            
            path_parts.reverse()
            return '/' + '/'.join(path_parts) if path_parts else ""
            
        except Exception as e:
            logger.warning(f"Failed to generate absolute XPath: {e}")
            return ""
    
    def _generate_relative_xpath(self, element: Tag) -> str:
        """Generate relative XPath using ID or unique attributes."""
        try:
            # Try to find nearest parent with ID
            current = element.parent
            while current and current.name != 'html':
                if current.get('id'):
                    # Generate path from this ID-bearing parent
                    sub_path = []
                    temp = element
                    while temp != current:
                        siblings = [s for s in temp.parent.children if hasattr(s, 'name') and s.name == temp.name]
                        if len(siblings) == 1:
                            sub_path.append(temp.name)
                        else:
                            position = siblings.index(temp) + 1
                            sub_path.append(f"{temp.name}[{position}]")
                        temp = temp.parent
                    sub_path.reverse()
                    if sub_path:
                        return f"//*[@id='{current['id']}']/" + '/'.join(sub_path)
                    else:
                        return f"//*[@id='{current['id']}']"
                current = current.parent
            
            # Fallback to tag-based relative path
            return f"//{element.name}"
            
        except Exception as e:
            logger.warning(f"Failed to generate relative XPath: {e}")
            return ""
    
    def _extract_style_property(self, style: str, property_name: str, default: int) -> int:
        """Extract numeric property from inline style string."""
        try:
            pattern = rf'{property_name}:\s*(\d+)'
            match = re.search(pattern, style)
            if match:
                return int(match.group(1))
        except Exception:
            pass
        return default
    
    def _extract_neighbor_texts(self, element: Tag, max_neighbors: int = 5) -> List[str]:
        """Extract visible text from neighboring elements."""
        neighbors = []
        
        try:
            # Get parent's all children
            if element.parent:
                for sibling in element.parent.children:
                    if sibling == element or not hasattr(sibling, 'get_text'):
                        continue
                    text = sibling.get_text(strip=True)
                    if text and len(text) < 100:  # Avoid huge blocks
                        neighbors.append(text)
                    if len(neighbors) >= max_neighbors:
                        break
        except Exception as e:
            logger.warning(f"Failed to extract neighbor texts: {e}")
        
        return neighbors
    
    def _extract_sibling_tags(self, element: Tag, max_siblings: int = 5) -> List[str]:
        """Extract tag names of sibling elements."""
        siblings = []
        
        try:
            if element.parent:
                for sibling in element.parent.children:
                    if sibling == element or not hasattr(sibling, 'name'):
                        continue
                    if sibling.name:
                        siblings.append(sibling.name)
                    if len(siblings) >= max_siblings:
                        break
        except Exception as e:
            logger.warning(f"Failed to extract sibling tags: {e}")
        
        return siblings
    
    def _is_button_element(self, element: Tag) -> bool:
        """Determine if element is functionally a button."""
        if element.name == 'button':
            return True
        if element.name == 'input' and element.get('type') in ['button', 'submit', 'reset']:
            return True
        if element.name == 'a' and ('btn' in element.get('class', []) or 'button' in element.get('class', [])):
            return True
        return False
    
    def _is_clickable_element(self, element: Tag) -> bool:
        """Determine if element is clickable."""
        clickable_tags = ['a', 'button', 'input', 'select', 'textarea']
        if element.name in clickable_tags:
            return True
        if element.get('onclick') or element.get('role') == 'button':
            return True
        return False
    
    def _is_input_element(self, element: Tag) -> bool:
        """Determine if element is an input."""
        return element.name in ['input', 'textarea', 'select']
    
    # =================== EXISTING METHODS (preserved) ===================
    
    def extract_parent_context(self, dom_content: str, locator: str) -> List[str]:
        """Extract parent element context from DOM.
        
        Args:
            dom_content: Full DOM content as string
            locator: Original locator used to find the element
            
        Returns:
            List of parent element descriptions
        """
        try:
            soup = BeautifulSoup(dom_content, 'html.parser')
            element = self._find_element_by_locator(soup, locator)

            if not element:
                return []

            parent_context = []
            current = element.parent

            # Traverse up to 3 levels of parents
            for _ in range(3):
                if not current or current.name == 'html':
                    break

                parent_desc = self._describe_element(current)
                if parent_desc:
                    parent_context.append(parent_desc)

                current = current.parent

            return parent_context

        except Exception as e:
            logger.warning(f"Failed to extract parent context: {e}")
            return []

    def extract_sibling_context(self, dom_content: str, locator: str) -> List[str]:
        """Extract sibling element context from DOM.

        Args:
            dom_content: Full DOM content as string
            locator: Original locator used to find the element

        Returns:
            List of sibling element descriptions
        """
        try:
            soup = BeautifulSoup(dom_content, 'html.parser')
            element = self._find_element_by_locator(soup, locator)

            if not element:
                return []

            sibling_context = []

            # Get previous siblings
            prev_sibling = element.previous_sibling
            prev_count = 0
            while prev_sibling and prev_count < 2:
                if hasattr(prev_sibling, 'name') and prev_sibling.name:
                    desc = self._describe_element(prev_sibling)
                    if desc:
                        sibling_context.append(f"prev:{desc}")
                        prev_count += 1
                prev_sibling = prev_sibling.previous_sibling

            # Get next siblings
            next_sibling = element.next_sibling
            next_count = 0
            while next_sibling and next_count < 2:
                if hasattr(next_sibling, 'name') and next_sibling.name:
                    desc = self._describe_element(next_sibling)
                    if desc:
                        sibling_context.append(f"next:{desc}")
                        next_count += 1
                next_sibling = next_sibling.next_sibling

            return sibling_context

        except Exception as e:
            logger.warning(f"Failed to extract sibling context: {e}")
            return []

    def generate_dom_path(self, dom_content: str, locator: str) -> str:
        """Generate a DOM path for the element.

        Args:
            dom_content: Full DOM content as string
            locator: Original locator used to find the element

        Returns:
            DOM path string (e.g., "html > body > div.container > button#submit")
        """
        try:
            soup = BeautifulSoup(dom_content, 'html.parser')
            element = self._find_element_by_locator(soup, locator)

            if not element:
                return ""

            path_parts = []
            current = element

            while current and current.name != 'html':
                part = current.name

                # Add ID if present
                if current.get('id'):
                    part += f"#{current['id']}"
                # Add first class if present
                elif current.get('class'):
                    classes = current['class']
                    if classes:
                        part += f".{classes[0]}"

                path_parts.append(part)
                current = current.parent

            # Add html at the end
            if current and current.name == 'html':
                path_parts.append('html')

            # Reverse to get top-down path
            path_parts.reverse()
            return ' > '.join(path_parts)

        except Exception as e:
            logger.warning(f"Failed to generate DOM path: {e}")
            return ""

    def find_similar_elements(self, dom_content: str,
                              fingerprint: ElementFingerprint) -> List[Dict[str, Any]]:
        """Find elements in DOM that are similar to the fingerprint.

        Args:
            dom_content: Current DOM content as string
            fingerprint: ElementFingerprint to match against

        Returns:
            List of candidate element information dictionaries
        """
        try:
            soup = BeautifulSoup(dom_content, 'html.parser')
            candidates = []

            # Find all elements with the same tag name
            elements = soup.find_all(fingerprint.tag_name)

            for element in elements:
                candidate_info = {
                    'tag_name': element.name,
                    'attributes': dict(element.attrs) if element.attrs else {},
                    'text_content': element.get_text(strip=True),
                    'parent_context': self._get_element_parent_context(element),
                    'sibling_context': self._get_element_sibling_context(element),
                    'element': element  # Keep reference for locator generation
                }
                candidates.append(candidate_info)

            return candidates

        except Exception as e:
            logger.warning(f"Failed to find similar elements: {e}")
            return []

    def generate_locator_for_element(self, element_info: Dict[str, Any]) -> str:
        """Generate a locator for the given element.

        Args:
            element_info: Element information dictionary with 'element' key

        Returns:
            Generated locator string
        """
        try:
            element = element_info.get('element')
            if not element:
                return ""

            # Try different locator strategies in priority order

            # 1. ID locator (highest priority)
            if element.get('id'):
                return f"id={element['id']}"

            # 2. Name locator
            if element.get('name'):
                return f"name={element['name']}"

            # 3. CSS locator with class
            if element.get('class'):
                classes = element['class']
                if classes:
                    class_selector = '.'.join(classes)
                    return f"css={element.name}.{class_selector}"

            # 4. CSS locator with attributes
            unique_attrs = self._find_unique_attributes(element)
            if unique_attrs:
                attr_selector = ''.join(
                    [f'[{k}="{v}"]' for k, v in unique_attrs.items()])
                return f"css={element.name}{attr_selector}"

            # 5. XPath locator
            xpath = self._generate_xpath_for_element(element)
            if xpath:
                return f"xpath={xpath}"

            # 6. Fallback to tag name with text
            text = element.get_text(strip=True)
            if text:
                return f"xpath=//{element.name}[contains(text(), '{text[:50]}')]"

            # 7. Last resort - tag name only
            return f"css={element.name}"

        except Exception as e:
            logger.warning(f"Failed to generate locator: {e}")
            return ""

    def _find_element_by_locator(self, soup: BeautifulSoup, locator: str) -> Optional[Tag]:
        """Find element in soup using the given locator.

        Args:
            soup: BeautifulSoup object
            locator: Locator string to parse and use

        Returns:
            Found element or None
        """
        try:
            # Parse locator to determine strategy and value
            strategy, value = self._parse_locator(locator)

            if strategy == LocatorStrategy.ID:
                return soup.find(attrs={'id': value})
            elif strategy == LocatorStrategy.NAME:
                return soup.find(attrs={'name': value})
            elif strategy == LocatorStrategy.CSS:
                return soup.select_one(value)
            elif strategy == LocatorStrategy.CLASS_NAME:
                return soup.find(class_=value)
            elif strategy == LocatorStrategy.XPATH:
                # Basic XPath support - this is simplified
                return self._find_by_xpath(soup, value)
            elif strategy == LocatorStrategy.LINK_TEXT:
                return soup.find('a', string=value)

            return None

        except Exception as e:
            logger.warning(
                f"Failed to find element by locator '{locator}': {e}")
            return None

    def _parse_locator(self, locator: str) -> Tuple[LocatorStrategy, str]:
        """Parse locator string to extract strategy and value.

        Args:
            locator: Locator string (e.g., "id=submit", "css=.button")

        Returns:
            Tuple of (strategy, value)
        """
        locator = locator.strip()

        # Try to match against known patterns
        for strategy, pattern in self.locator_patterns.items():
            match = re.match(pattern, locator)
            if match:
                return strategy, match.group(2)

        # Check for simple patterns without quotes
        if '=' in locator:
            strategy_str, value = locator.split('=', 1)
            strategy_str = strategy_str.strip().lower()

            strategy_map = {
                'id': LocatorStrategy.ID,
                'name': LocatorStrategy.NAME,
                'css': LocatorStrategy.CSS,
                'xpath': LocatorStrategy.XPATH,
                'link': LocatorStrategy.LINK_TEXT,
                'class': LocatorStrategy.CLASS_NAME
            }

            if strategy_str in strategy_map:
                return strategy_map[strategy_str], value.strip()

        # Default to CSS if no strategy specified
        return LocatorStrategy.CSS, locator

    def _describe_element(self, element: Tag) -> str:
        """Create a description of an element for context.

        Args:
            element: BeautifulSoup Tag element

        Returns:
            Element description string
        """
        if not element or not hasattr(element, 'name'):
            return ""

        desc = element.name

        # Add ID if present
        if element.get('id'):
            desc += f"#{element['id']}"

        # Add first class if present
        elif element.get('class'):
            classes = element['class']
            if classes:
                desc += f".{classes[0]}"

        # Add text content if short
        text = element.get_text(strip=True)
        if text and len(text) <= 20:
            desc += f"[{text}]"

        return desc

    def _get_element_parent_context(self, element: Tag) -> List[str]:
        """Get parent context for an element."""
        parent_context = []
        current = element.parent

        for _ in range(3):
            if not current or current.name == 'html':
                break

            desc = self._describe_element(current)
            if desc:
                parent_context.append(desc)

            current = current.parent

        return parent_context

    def _get_element_sibling_context(self, element: Tag) -> List[str]:
        """Get sibling context for an element."""
        sibling_context = []

        # Previous siblings
        prev_sibling = element.previous_sibling
        prev_count = 0
        while prev_sibling and prev_count < 2:
            if hasattr(prev_sibling, 'name') and prev_sibling.name:
                desc = self._describe_element(prev_sibling)
                if desc:
                    sibling_context.append(f"prev:{desc}")
                    prev_count += 1
            prev_sibling = prev_sibling.previous_sibling

        # Next siblings
        next_sibling = element.next_sibling
        next_count = 0
        while next_sibling and next_count < 2:
            if hasattr(next_sibling, 'name') and next_sibling.name:
                desc = self._describe_element(next_sibling)
                if desc:
                    sibling_context.append(f"next:{desc}")
                    next_count += 1
            next_sibling = next_sibling.next_sibling

        return sibling_context

    def _find_unique_attributes(self, element: Tag) -> Dict[str, str]:
        """Find unique attributes for an element."""
        unique_attrs = {}

        # Prioritize certain attributes
        priority_attrs = ['data-testid', 'data-test',
                          'role', 'type', 'placeholder']

        for attr in priority_attrs:
            if element.get(attr):
                unique_attrs[attr] = element[attr]
                break  # One unique attribute is usually enough

        return unique_attrs

    def _generate_xpath_for_element(self, element: Tag) -> str:
        """Generate XPath for an element."""
        try:
            path_parts = []
            current = element

            while current and current.name != 'html' and current.parent:
                # Count siblings of the same type
                siblings = [s for s in current.parent.children
                            if hasattr(s, 'name') and s.name == current.name]

                if len(siblings) == 1:
                    path_parts.append(current.name)
                else:
                    # Find position among siblings
                    position = siblings.index(current) + 1
                    path_parts.append(f"{current.name}[{position}]")

                current = current.parent

            path_parts.reverse()
            return '/' + '/'.join(path_parts)

        except Exception as e:
            logger.warning(f"Failed to generate XPath: {e}")
            return ""

    def _find_by_xpath(self, soup: BeautifulSoup, xpath: str) -> Optional[Tag]:
        """Basic XPath support for finding elements.

        This is a simplified XPath implementation that handles basic cases.
        """
        try:
            # Remove leading slash
            xpath = xpath.lstrip('/')

            # Split path into parts
            parts = xpath.split('/')
            current_elements = [soup]

            for part in parts:
                next_elements = []

                # Handle indexed elements like div[2]
                if '[' in part and ']' in part:
                    tag_name = part.split('[')[0]
                    index_str = part.split('[')[1].split(']')[0]

                    try:
                        index = int(index_str) - 1  # XPath is 1-indexed
                        for elem in current_elements:
                            children = elem.find_all(tag_name, recursive=False)
                            if 0 <= index < len(children):
                                next_elements.append(children[index])
                    except ValueError:
                        # Handle text() or attribute conditions
                        continue
                else:
                    # Simple tag name
                    for elem in current_elements:
                        next_elements.extend(
                            elem.find_all(part, recursive=False))

                current_elements = next_elements
                if not current_elements:
                    break

            return current_elements[0] if current_elements else None

        except Exception as e:
            logger.warning(f"Failed to find by XPath '{xpath}': {e}")
            return None
