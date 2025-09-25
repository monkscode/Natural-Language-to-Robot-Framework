"""DOM analysis utilities for element fingerprinting."""

import re
import logging
from typing import Dict, List, Optional, Any, Tuple
from bs4 import BeautifulSoup, Tag
from urllib.parse import urljoin

from ..core.models.healing_models import ElementFingerprint, LocatorStrategy


logger = logging.getLogger(__name__)


class DOMAnalyzer:
    """Utility class for analyzing DOM structure and extracting element context."""
    
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
