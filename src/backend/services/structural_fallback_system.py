"""
Structural Fallback System for Self-Healing

This system provides structural similarity-based element finding when vision-based
methods fail. It uses the Similo algorithm to find similar elements in the current DOM
based on comprehensive properties of the old element.

Usage:
    When vision fails to find an element, this system:
    1. Extracts comprehensive properties from the old element
    2. Calculates similarity scores for all elements in current DOM using Similo
    3. Generates multiple locator strategies for top matches
    4. Validates each locator in DOM (uniqueness, correctness)
    5. Returns the best validated locator

Expected Success Rate: +5-10% additional (when vision fails)
"""

import logging
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from bs4 import BeautifulSoup

from ..core.models import FailureContext
from .similarity_scorer import SimilarityScorer, ElementProperties
from .dom_analyzer import DOMAnalyzer

logger = logging.getLogger(__name__)


@dataclass
class StructuralMatchCandidate:
    """Represents a potential match found through structural similarity."""
    element_index: int
    similarity_score: float
    element_properties: ElementProperties
    locators: List[Dict[str, Any]]
    validated_locators: List[Dict[str, Any]]
    best_locator: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'element_index': self.element_index,
            'similarity_score': self.similarity_score,
            'element_properties': asdict(self.element_properties),
            'locators': self.locators,
            'validated_locators': self.validated_locators,
            'best_locator': self.best_locator
        }


@dataclass
class StructuralFallbackResult:
    """Result of structural fallback element finding."""
    success: bool
    fallback_used: str = 'structural_similarity'
    best_locator: Optional[str] = None
    all_locators: List[Dict[str, Any]] = None
    similarity_score: float = 0.0
    confidence: float = 0.0
    candidates_evaluated: int = 0
    top_candidates: List[StructuralMatchCandidate] = None
    error_message: Optional[str] = None
    
    def __post_init__(self):
        if self.all_locators is None:
            self.all_locators = []
        if self.top_candidates is None:
            self.top_candidates = []
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'success': self.success,
            'fallback_used': self.fallback_used,
            'best_locator': self.best_locator,
            'all_locators': self.all_locators,
            'similarity_score': self.similarity_score,
            'confidence': self.confidence,
            'candidates_evaluated': self.candidates_evaluated,
            'top_candidates': [c.to_dict() for c in self.top_candidates],
            'error_message': self.error_message
        }


class StructuralFallbackSystem:
    """
    Structural Fallback System using Similo algorithm.
    
    This system finds elements by structural similarity when vision-based
    methods fail. It compares comprehensive properties of elements using
    10 distance metrics and generates validated locators.
    """
    
    def __init__(self, similarity_threshold: float = 0.70):
        """
        Initialize the structural fallback system.
        
        Args:
            similarity_threshold: Minimum similarity score to consider a match (0.0-1.0)
        """
        self.similarity_scorer = SimilarityScorer()  # SimilarityScorer doesn't take threshold
        self.dom_analyzer = DOMAnalyzer()
        self.similarity_threshold = similarity_threshold
        
        logger.info(f"Structural Fallback System initialized with threshold={similarity_threshold}")
    
    def find_similar_element(
        self,
        old_locator: str,
        old_dom: str,
        current_dom: str,
        top_k: int = 3
    ) -> StructuralFallbackResult:
        """
        Find similar element in current DOM based on old element properties.
        
        This is the main method that orchestrates the structural fallback process:
        1. Extract properties from old element
        2. Find similar elements in current DOM using Similo
        3. Generate and validate locators for top matches
        4. Return best validated locator
        
        Args:
            old_locator: Original locator that no longer works
            old_dom: HTML of the page when test was recorded
            current_dom: Current HTML of the page
            top_k: Number of top candidates to evaluate (default: 3)
        
        Returns:
            StructuralFallbackResult with best locator or error
        """
        logger.info(f"Starting structural fallback for locator: {old_locator}")
        
        try:
            # Step 1: Extract properties from old element
            old_properties = self._extract_old_element_properties(old_locator, old_dom)
            if not old_properties:
                return StructuralFallbackResult(
                    success=False,
                    error_message=f"Could not extract properties from old locator: {old_locator}"
                )
            
            logger.info(f"Extracted old element: tag={old_properties.tag}, text={old_properties.text[:30] if old_properties.text else 'N/A'}")
            
            # Step 2: Find similar elements using Similo algorithm
            similar_elements = self.similarity_scorer.find_similar_elements(
                target_properties=old_properties,
                current_dom_html=current_dom,
                top_k=top_k
            )
            
            if not similar_elements:
                return StructuralFallbackResult(
                    success=False,
                    candidates_evaluated=0,
                    error_message="No similar elements found in current DOM"
                )
            
            logger.info(f"Found {len(similar_elements)} similar elements (top {top_k})")
            
            # Step 3: Generate and validate locators for each candidate
            candidates = []
            for element_data in similar_elements:
                candidate = self._create_validated_candidate(
                    element_data=element_data,
                    current_dom=current_dom
                )
                if candidate:
                    candidates.append(candidate)
            
            if not candidates:
                return StructuralFallbackResult(
                    success=False,
                    candidates_evaluated=len(similar_elements),
                    error_message="No candidates generated valid locators"
                )
            
            # Step 4: Select best candidate (highest similarity with validated locators)
            best_candidate = self._select_best_candidate(candidates)
            
            if not best_candidate or not best_candidate.best_locator:
                return StructuralFallbackResult(
                    success=False,
                    candidates_evaluated=len(candidates),
                    top_candidates=candidates,
                    error_message="No candidate has validated locators"
                )
            
            # Success!
            logger.info(f"✅ Structural fallback SUCCESS: {best_candidate.best_locator} (similarity={best_candidate.similarity_score:.2f})")
            
            return StructuralFallbackResult(
                success=True,
                best_locator=best_candidate.best_locator,
                all_locators=best_candidate.validated_locators,
                similarity_score=best_candidate.similarity_score,
                confidence=self._calculate_confidence(best_candidate),
                candidates_evaluated=len(similar_elements),
                top_candidates=candidates
            )
            
        except Exception as e:
            logger.error(f"Error in structural fallback: {e}", exc_info=True)
            return StructuralFallbackResult(
                success=False,
                error_message=f"Exception during structural fallback: {str(e)}"
            )
    
    def _extract_old_element_properties(
        self,
        old_locator: str,
        old_dom: str
    ) -> Optional[ElementProperties]:
        """
        Extract comprehensive properties from the old element.
        
        Args:
            old_locator: Original locator (e.g., "id=login-btn", "css=.button")
            old_dom: HTML of the page when test was recorded
        
        Returns:
            ElementProperties object or None if extraction fails
        """
        try:
            soup = BeautifulSoup(old_dom, 'html.parser')
            
            # Parse locator to find element
            element = self._find_element_by_locator(soup, old_locator)
            
            if not element:
                logger.warning(f"Could not find element with locator: {old_locator}")
                return None
            
            # Use DOMAnalyzer to extract comprehensive properties
            properties = self.dom_analyzer.extract_comprehensive_properties(
                dom_html=old_dom,
                element=element
            )
            
            return properties
            
        except Exception as e:
            logger.error(f"Error extracting old element properties: {e}")
            return None
    
    def _find_element_by_locator(self, soup: BeautifulSoup, locator: str):
        """
        Find element in BeautifulSoup using Robot Framework style locator.
        
        Args:
            soup: BeautifulSoup parsed HTML
            locator: Locator string (e.g., "id=login", "css=.btn", "xpath=//button")
        
        Returns:
            BeautifulSoup element or None
        """
        try:
            if locator.startswith('id='):
                element_id = locator.split('=', 1)[1]
                return soup.find(id=element_id)
            
            elif locator.startswith('name='):
                name = locator.split('=', 1)[1]
                return soup.find(attrs={'name': name})
            
            elif locator.startswith('css='):
                css_selector = locator.split('=', 1)[1]
                # Basic CSS selector support
                if '#' in css_selector:
                    element_id = css_selector.split('#')[1].split('.')[0].split('[')[0]
                    return soup.find(id=element_id)
                elif '.' in css_selector:
                    classes = css_selector.split('.')[1:]
                    return soup.find(class_=classes)
                elif '[' in css_selector:
                    # Attribute selector like [data-testid="value"]
                    attr_match = css_selector.split('[')[1].split(']')[0]
                    if '=' in attr_match:
                        attr, value = attr_match.split('=', 1)
                        value = value.strip('"\'')
                        return soup.find(attrs={attr: value})
            
            elif locator.startswith('xpath='):
                # XPath is complex, try basic patterns
                xpath = locator.split('=', 1)[1]
                if '@id=' in xpath:
                    id_match = xpath.split('@id=')[1].split('"')[1] if '"' in xpath else xpath.split('@id=')[1].split("'")[1]
                    return soup.find(id=id_match)
            
            # Fallback: treat as text search
            return soup.find(string=locator)
            
        except Exception as e:
            logger.warning(f"Error parsing locator {locator}: {e}")
            return None
    
    def _create_validated_candidate(
        self,
        element_data: Dict[str, Any],
        current_dom: str
    ) -> Optional[StructuralMatchCandidate]:
        """
        Create a candidate with validated locators.
        
        Args:
            element_data: Dict with 'element', 'properties', 'similarity_score'
            current_dom: Current DOM HTML for validation
        
        Returns:
            StructuralMatchCandidate or None if no valid locators
        """
        try:
            element = element_data['element']
            properties = element_data['properties']
            similarity_score = element_data['similarity_score']
            
            # Generate multiple locator strategies
            locators = self.dom_analyzer.generate_multi_locators(
                dom_html=current_dom,
                element=element
            )
            
            # Validate each locator in DOM
            validated_locators = []
            soup = BeautifulSoup(current_dom, 'html.parser')
            
            for locator_data in locators:
                locator = locator_data['locator']
                
                # Check if locator is unique and selects correct element
                is_valid, is_unique = self._validate_locator(
                    soup=soup,
                    locator=locator,
                    target_element=element
                )
                
                if is_valid:
                    validated_locators.append({
                        'locator': locator,
                        'type': locator_data['type'],
                        'priority': locator_data['priority'],
                        'unique': is_unique,
                        'confidence': 1.0 if is_unique else 0.7
                    })
            
            if not validated_locators:
                logger.debug(f"No validated locators for candidate (similarity={similarity_score:.2f})")
                return None
            
            # Sort by priority and uniqueness
            validated_locators.sort(key=lambda x: (x['unique'], -x['priority']), reverse=True)
            
            return StructuralMatchCandidate(
                element_index=element_data.get('index', -1),
                similarity_score=similarity_score,
                element_properties=properties,
                locators=locators,
                validated_locators=validated_locators,
                best_locator=validated_locators[0]['locator'] if validated_locators else None
            )
            
        except Exception as e:
            logger.warning(f"Error creating validated candidate: {e}")
            return None
    
    def _validate_locator(
        self,
        soup: BeautifulSoup,
        locator: str,
        target_element
    ) -> Tuple[bool, bool]:
        """
        Validate if locator selects the correct element and if it's unique.
        
        Args:
            soup: BeautifulSoup parsed HTML
            locator: Locator to validate
            target_element: The element we expect to select
        
        Returns:
            Tuple of (is_valid, is_unique)
        """
        try:
            # Find all elements matching this locator
            matches = self._find_all_by_locator(soup, locator)
            
            if not matches:
                return False, False  # Locator finds nothing
            
            # Check if target element is in matches
            is_valid = target_element in matches
            is_unique = len(matches) == 1
            
            return is_valid, is_unique
            
        except Exception as e:
            logger.debug(f"Error validating locator {locator}: {e}")
            return False, False
    
    def _find_all_by_locator(self, soup: BeautifulSoup, locator: str) -> List:
        """Find all elements matching locator."""
        try:
            if locator.startswith('id='):
                element_id = locator.split('=', 1)[1]
                element = soup.find(id=element_id)
                return [element] if element else []
            
            elif locator.startswith('name='):
                name = locator.split('=', 1)[1]
                return soup.find_all(attrs={'name': name})
            
            elif locator.startswith('css='):
                css_selector = locator.split('=', 1)[1]
                # Basic CSS support
                if '#' in css_selector:
                    element_id = css_selector.split('#')[1].split('.')[0]
                    element = soup.find(id=element_id)
                    return [element] if element else []
                elif '.' in css_selector:
                    classes = css_selector.split('.')[1:]
                    return soup.find_all(class_=classes)
            
            return []
            
        except Exception:
            return []
    
    def _select_best_candidate(
        self,
        candidates: List[StructuralMatchCandidate]
    ) -> Optional[StructuralMatchCandidate]:
        """
        Select the best candidate based on similarity score and validated locators.
        
        Args:
            candidates: List of candidates with validated locators
        
        Returns:
            Best candidate or None
        """
        if not candidates:
            return None
        
        # Sort by:
        # 1. Has validated locators (required)
        # 2. Similarity score (higher is better)
        # 3. Number of unique locators (more is better)
        
        valid_candidates = [c for c in candidates if c.validated_locators]
        
        if not valid_candidates:
            return None
        
        valid_candidates.sort(
            key=lambda c: (
                c.similarity_score,
                len([l for l in c.validated_locators if l['unique']])
            ),
            reverse=True
        )
        
        return valid_candidates[0]
    
    def _calculate_confidence(self, candidate: StructuralMatchCandidate) -> float:
        """
        Calculate overall confidence for a candidate.
        
        Confidence is based on:
        - Similarity score (60%)
        - Number of unique validated locators (20%)
        - Best locator uniqueness (20%)
        
        Args:
            candidate: Candidate to calculate confidence for
        
        Returns:
            Confidence score (0.0-1.0)
        """
        similarity_component = candidate.similarity_score * 0.6
        
        unique_count = len([l for l in candidate.validated_locators if l['unique']])
        unique_component = min(unique_count / 3, 1.0) * 0.2  # Max at 3 unique locators
        
        best_unique = 1.0 if (candidate.validated_locators and candidate.validated_locators[0]['unique']) else 0.5
        best_component = best_unique * 0.2
        
        confidence = similarity_component + unique_component + best_component
        
        return min(confidence, 1.0)
