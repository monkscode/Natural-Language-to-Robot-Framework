"""
Advanced Similarity Scoring for Element Matching

This module implements the Similo algorithm for robust element re-identification
based on comprehensive similarity scoring across multiple element properties.

Based on research from:
- Similo (Nass et al.): https://arxiv.org/html/2505.16424v1
- COLOR (Kirinuki et al.): Industry best practices
- BrowserStack: Selenium locator strategies

Key Features:
- Multiple similarity functions (Levenshtein, Jaccard, Jaro-Winkler, Euclidean)
- Weighted property scoring based on stability research
- Ensemble-based element matching
- Configurable property weights based on element types
"""

import re
import math
import logging
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum


logger = logging.getLogger(__name__)


class SimilarityFunction(Enum):
    """Available similarity calculation functions."""
    EQUALITY = "equality"
    LEVENSHTEIN = "levenshtein"
    JACCARD = "jaccard"
    JARO_WINKLER = "jaro_winkler"
    EUCLIDEAN = "euclidean"
    MANHATTAN = "manhattan"
    EXPONENTIAL_DECAY = "exponential_decay"
    SET_SIMILARITY = "set_similarity"
    INTERSECT_VALUE = "intersect_value"
    INTERSECT_KEY = "intersect_key"


@dataclass
class PropertyConfig:
    """Configuration for a single element property."""
    name: str
    weight: float
    similarity_function: SimilarityFunction
    stable: bool = True  # Is this property stable across versions?
    
    
@dataclass
class ElementProperties:
    """Comprehensive element properties for similarity scoring."""
    # Core identifiers (highly stable)
    tag: str = ""
    id: str = ""
    name: str = ""
    type: str = ""
    aria_label: str = ""
    
    # Attribute-based (moderately stable)
    class_name: str = ""
    href: str = ""
    alt: str = ""
    src: str = ""
    role: str = ""
    
    # Structural (dynamic but useful)
    absolute_xpath: str = ""
    relative_xpath: str = ""
    
    # Visual properties (moderately stable)
    location_x: int = 0
    location_y: int = 0
    width: int = 0
    height: int = 0
    
    # Content-based (moderately stable)
    visible_text: str = ""
    placeholder: str = ""
    value: str = ""
    
    # Contextual (less stable but valuable)
    neighbor_texts: List[str] = None
    parent_tag: str = ""
    sibling_tags: List[str] = None
    
    # Computed properties
    is_button: bool = False
    is_clickable: bool = False
    is_input: bool = False
    
    # Full attribute map
    attributes: Dict[str, str] = None
    
    def __post_init__(self):
        """Initialize mutable defaults."""
        if self.neighbor_texts is None:
            self.neighbor_texts = []
        if self.sibling_tags is None:
            self.sibling_tags = []
        if self.attributes is None:
            self.attributes = {}
    
    @property
    def area(self) -> int:
        """Calculate element area."""
        return self.width * self.height
    
    @property
    def aspect_ratio(self) -> float:
        """Calculate element aspect ratio."""
        if self.height == 0:
            return 0.0
        return self.width / self.height


class SimilarityScorer:
    """
    Advanced similarity scorer implementing Similo algorithm with multiple
    distance metrics and weighted property scoring.
    
    This class calculates comprehensive similarity scores between elements
    to enable robust re-identification when locators break.
    """
    
    # Research-based optimal weights (from Similo++ optimization)
    DEFAULT_WEIGHTS = {
        'tag': 0.80,  # Stable but can change (e.g., button -> a)
        'id': 2.70,  # Highly stable and unique
        'name': 2.90,  # Very stable for form elements
        'type': 1.10,  # Stable for inputs
        'aria_label': 2.95,  # Very stable and accessible
        'class_name': 1.00,  # Moderately volatile
        'href': 0.30,  # Can change frequently
        'alt': 1.95,  # Stable for images
        'absolute_xpath': 0.50,  # Fragile to structural changes
        'relative_xpath': 0.50,  # Less fragile than absolute
        'is_button': 2.85,  # Very stable functional property
        'location_x': 2.00,  # Moderately stable (layout changes)
        'location_y': 2.00,  # Moderately stable
        'area': 1.30,  # Moderately stable
        'visible_text': 2.95,  # Very stable
        'neighbor_texts': 1.00,  # Contextual, less stable
        'attributes': 2.20,  # Intersection-based, fairly stable
    }
    
    def __init__(self, custom_weights: Optional[Dict[str, float]] = None):
        """
        Initialize the similarity scorer.
        
        Args:
            custom_weights: Optional dictionary to override default weights
        """
        self.weights = self.DEFAULT_WEIGHTS.copy()
        if custom_weights:
            self.weights.update(custom_weights)
        
        # Cache for expensive computations
        self._levenshtein_cache: Dict[Tuple[str, str], int] = {}
    
    def calculate_similarity(self, 
                           target: ElementProperties,
                           candidate: ElementProperties) -> float:
        """
        Calculate overall similarity score between target and candidate elements.
        
        This implements the core Similo algorithm:
        Score = Σ(similarity(target.prop, candidate.prop) * weight)
        
        Args:
            target: Properties of the target element (from old version)
            candidate: Properties of candidate element (from new version)
            
        Returns:
            Similarity score (higher is more similar)
        """
        total_score = 0.0
        max_possible_score = 0.0
        
        # Tag similarity (Jaccard for partial matches)
        if self.weights.get('tag', 0) > 0:
            sim = self._jaccard_similarity(target.tag, candidate.tag)
            total_score += sim * self.weights['tag']
            max_possible_score += self.weights['tag']
        
        # ID similarity (Levenshtein - can have minor changes)
        if self.weights.get('id', 0) > 0:
            sim = self._levenshtein_similarity(target.id, candidate.id)
            total_score += sim * self.weights['id']
            max_possible_score += self.weights['id']
        
        # Name similarity (Equality - should be exact)
        if self.weights.get('name', 0) > 0:
            sim = self._equality_similarity(target.name, candidate.name)
            total_score += sim * self.weights['name']
            max_possible_score += self.weights['name']
        
        # Type similarity (Equality)
        if self.weights.get('type', 0) > 0:
            sim = self._equality_similarity(target.type, candidate.type)
            total_score += sim * self.weights['type']
            max_possible_score += self.weights['type']
        
        # Aria-label similarity (Equality - accessibility label)
        if self.weights.get('aria_label', 0) > 0:
            sim = self._equality_similarity(target.aria_label, candidate.aria_label)
            total_score += sim * self.weights['aria_label']
            max_possible_score += self.weights['aria_label']
        
        # Class similarity (String set - classes can be reordered)
        if self.weights.get('class_name', 0) > 0:
            sim = self._set_similarity(target.class_name, candidate.class_name)
            total_score += sim * self.weights['class_name']
            max_possible_score += self.weights['class_name']
        
        # Href similarity (Levenshtein)
        if self.weights.get('href', 0) > 0:
            sim = self._levenshtein_similarity(target.href, candidate.href)
            total_score += sim * self.weights['href']
            max_possible_score += self.weights['href']
        
        # Alt similarity (Levenshtein)
        if self.weights.get('alt', 0) > 0:
            sim = self._levenshtein_similarity(target.alt, candidate.alt)
            total_score += sim * self.weights['alt']
            max_possible_score += self.weights['alt']
        
        # XPath similarities (Levenshtein)
        if self.weights.get('absolute_xpath', 0) > 0:
            sim = self._levenshtein_similarity(target.absolute_xpath, candidate.absolute_xpath)
            total_score += sim * self.weights['absolute_xpath']
            max_possible_score += self.weights['absolute_xpath']
        
        # Is button similarity (Boolean equality)
        if self.weights.get('is_button', 0) > 0:
            sim = 1.0 if target.is_button == candidate.is_button else 0.0
            total_score += sim * self.weights['is_button']
            max_possible_score += self.weights['is_button']
        
        # Location similarity (2D Euclidean distance with exponential decay)
        if self.weights.get('location_x', 0) > 0:
            distance = math.sqrt(
                (target.location_x - candidate.location_x) ** 2 +
                (target.location_y - candidate.location_y) ** 2
            )
            # Exponential decay with λ=0.005 (medium decay)
            sim = math.exp(-0.005 * distance)
            total_score += sim * self.weights['location_x']
            max_possible_score += self.weights['location_x']
        
        # Area similarity (ratio of smaller/larger)
        if self.weights.get('area', 0) > 0:
            target_area = target.area
            candidate_area = candidate.area
            if target_area > 0 and candidate_area > 0:
                sim = min(target_area, candidate_area) / max(target_area, candidate_area)
            else:
                sim = 1.0 if target_area == candidate_area else 0.0
            total_score += sim * self.weights['area']
            max_possible_score += self.weights['area']
        
        # Visible text similarity (Levenshtein)
        if self.weights.get('visible_text', 0) > 0:
            sim = self._levenshtein_similarity(target.visible_text, candidate.visible_text)
            total_score += sim * self.weights['visible_text']
            max_possible_score += self.weights['visible_text']
        
        # Neighbor texts similarity (Set similarity)
        if self.weights.get('neighbor_texts', 0) > 0:
            sim = self._list_set_similarity(target.neighbor_texts, candidate.neighbor_texts)
            total_score += sim * self.weights['neighbor_texts']
            max_possible_score += self.weights['neighbor_texts']
        
        # Attributes similarity (Intersect value compare)
        if self.weights.get('attributes', 0) > 0:
            sim = self._intersect_value_similarity(target.attributes, candidate.attributes)
            total_score += sim * self.weights['attributes']
            max_possible_score += self.weights['attributes']
        
        # Normalize score to [0, 1] range
        if max_possible_score > 0:
            return total_score / max_possible_score
        return 0.0
    
    def find_best_match(self,
                        target: ElementProperties,
                        candidates: List[ElementProperties],
                        threshold: float = 0.6) -> Optional[Tuple[ElementProperties, float]]:
        """
        Find the best matching candidate for the target element.
        
        Args:
            target: Target element properties (from old version)
            candidates: List of candidate elements (from new version)
            threshold: Minimum similarity threshold (default 0.6)
            
        Returns:
            Tuple of (best_match, score) or None if no match above threshold
        """
        if not candidates:
            return None
        
        best_match = None
        best_score = 0.0
        
        for candidate in candidates:
            score = self.calculate_similarity(target, candidate)
            if score > best_score and score >= threshold:
                best_score = score
                best_match = candidate
        
        if best_match:
            logger.info(f"Found best match with similarity score: {best_score:.3f}")
            return (best_match, best_score)
        
        logger.warning(f"No match found above threshold {threshold}")
        return None
    
    def rank_candidates(self,
                       target: ElementProperties,
                       candidates: List[ElementProperties],
                       top_k: int = 10) -> List[Tuple[ElementProperties, float]]:
        """
        Rank all candidates by similarity score.
        
        Args:
            target: Target element properties
            candidates: List of candidate elements
            top_k: Return only top K matches (default 10)
            
        Returns:
            List of (candidate, score) tuples sorted by score (descending)
        """
        scored_candidates = []
        
        for candidate in candidates:
            score = self.calculate_similarity(target, candidate)
            scored_candidates.append((candidate, score))
        
        # Sort by score (descending)
        scored_candidates.sort(key=lambda x: x[1], reverse=True)
        
        return scored_candidates[:top_k]
    
    # =================== Similarity Functions ===================
    
    def _equality_similarity(self, a: str, b: str) -> float:
        """Exact string equality."""
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        return 1.0 if a == b else 0.0
    
    def _levenshtein_similarity(self, a: str, b: str) -> float:
        """
        Levenshtein distance normalized to similarity score [0, 1].
        
        Cached for performance on repeated calls.
        """
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        
        # Check cache
        cache_key = (a, b)
        if cache_key in self._levenshtein_cache:
            distance = self._levenshtein_cache[cache_key]
        else:
            distance = self._levenshtein_distance(a, b)
            self._levenshtein_cache[cache_key] = distance
        
        # Normalize to [0, 1]
        max_len = max(len(a), len(b))
        if max_len == 0:
            return 1.0
        return 1.0 - (distance / max_len)
    
    def _levenshtein_distance(self, s1: str, s2: str) -> int:
        """Calculate Levenshtein (edit) distance between two strings."""
        if len(s1) < len(s2):
            return self._levenshtein_distance(s2, s1)
        
        if len(s2) == 0:
            return len(s1)
        
        previous_row = range(len(s2) + 1)
        for i, c1 in enumerate(s1):
            current_row = [i + 1]
            for j, c2 in enumerate(s2):
                # Cost of insertions, deletions, or substitutions
                insertions = previous_row[j + 1] + 1
                deletions = current_row[j] + 1
                substitutions = previous_row[j] + (c1 != c2)
                current_row.append(min(insertions, deletions, substitutions))
            previous_row = current_row
        
        return previous_row[-1]
    
    def _jaccard_similarity(self, a: str, b: str) -> float:
        """
        Jaccard similarity on character sets.
        
        J(A,B) = |A ∩ B| / |A ∪ B|
        """
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        
        set_a = set(a.lower())
        set_b = set(b.lower())
        
        intersection = len(set_a & set_b)
        union = len(set_a | set_b)
        
        if union == 0:
            return 0.0
        return intersection / union
    
    def _jaro_winkler_similarity(self, a: str, b: str, scaling: float = 0.1) -> float:
        """
        Jaro-Winkler similarity (optimized for strings with common prefixes).
        
        Args:
            a: First string
            b: Second string
            scaling: Prefix scaling factor (default 0.1)
        """
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        
        # Calculate Jaro similarity first
        jaro = self._jaro_similarity(a, b)
        
        # Find common prefix length (max 4 chars as per original algorithm)
        prefix_len = 0
        for i in range(min(len(a), len(b), 4)):
            if a[i] == b[i]:
                prefix_len += 1
            else:
                break
        
        # Calculate Jaro-Winkler
        return jaro + (prefix_len * scaling * (1 - jaro))
    
    def _jaro_similarity(self, a: str, b: str) -> float:
        """Calculate Jaro similarity."""
        if a == b:
            return 1.0
        
        len_a, len_b = len(a), len(b)
        if len_a == 0 or len_b == 0:
            return 0.0
        
        # Calculate match window
        match_window = max(len_a, len_b) // 2 - 1
        if match_window < 1:
            match_window = 1
        
        # Initialize match arrays
        a_matches = [False] * len_a
        b_matches = [False] * len_b
        
        matches = 0
        transpositions = 0
        
        # Find matches
        for i in range(len_a):
            start = max(0, i - match_window)
            end = min(i + match_window + 1, len_b)
            for j in range(start, end):
                if b_matches[j] or a[i] != b[j]:
                    continue
                a_matches[i] = b_matches[j] = True
                matches += 1
                break
        
        if matches == 0:
            return 0.0
        
        # Count transpositions
        k = 0
        for i in range(len_a):
            if not a_matches[i]:
                continue
            while not b_matches[k]:
                k += 1
            if a[i] != b[k]:
                transpositions += 1
            k += 1
        
        # Calculate Jaro similarity
        return (matches / len_a + matches / len_b + 
                (matches - transpositions / 2) / matches) / 3
    
    def _set_similarity(self, a: str, b: str, delimiter: str = ' ') -> float:
        """
        Set-based similarity (e.g., for CSS classes).
        
        Split strings into sets and calculate Jaccard similarity.
        """
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        
        set_a = set(a.lower().split(delimiter))
        set_b = set(b.lower().split(delimiter))
        
        # Remove empty strings
        set_a.discard('')
        set_b.discard('')
        
        if not set_a and not set_b:
            return 1.0
        if not set_a or not set_b:
            return 0.0
        
        intersection = len(set_a & set_b)
        union = len(set_a | set_b)
        
        return intersection / union if union > 0 else 0.0
    
    def _list_set_similarity(self, a: List[str], b: List[str]) -> float:
        """Set similarity for lists of strings."""
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        
        set_a = set(item.lower() for item in a)
        set_b = set(item.lower() for item in b)
        
        intersection = len(set_a & set_b)
        union = len(set_a | set_b)
        
        return intersection / union if union > 0 else 0.0
    
    def _intersect_value_similarity(self, a: Dict[str, str], b: Dict[str, str]) -> float:
        """
        Intersection value similarity for attribute dictionaries.
        
        Counts key-value pairs that are identical.
        """
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        
        common_keys = set(a.keys()) & set(b.keys())
        matching_pairs = sum(1 for k in common_keys if a[k] == b[k])
        
        max_size = max(len(a), len(b))
        return matching_pairs / max_size if max_size > 0 else 0.0
    
    def _intersect_key_similarity(self, a: Dict[str, str], b: Dict[str, str]) -> float:
        """
        Intersection key similarity for attribute dictionaries.
        
        Jaccard similarity on just the keys (ignoring values).
        """
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        
        keys_a = set(a.keys())
        keys_b = set(b.keys())
        
        intersection = len(keys_a & keys_b)
        union = len(keys_a | keys_b)
        
        return intersection / union if union > 0 else 0.0


# Example usage and testing
if __name__ == "__main__":
    # Example: Finding a button that changed from "Log In" to "Sign In"
    
    target = ElementProperties(
        tag="button",
        id="submit-btn",
        name="login_submit",
        type="submit",
        aria_label="Submit login form",
        class_name="btn btn-primary btn-lg",
        visible_text="Log In",
        location_x=100,
        location_y=200,
        width=120,
        height=40,
        is_button=True,
        neighbor_texts=["Forgot password?", "Register now"],
        attributes={"data-action": "login", "data-track": "button_click"}
    )
    
    # Candidate 1: Very similar (just text changed)
    candidate1 = ElementProperties(
        tag="button",
        id="submit-btn",
        name="login_submit",
        type="submit",
        aria_label="Submit login form",
        class_name="btn btn-primary btn-lg",
        visible_text="Sign In",  # Changed
        location_x=100,
        location_y=200,
        width=120,
        height=40,
        is_button=True,
        neighbor_texts=["Forgot password?", "Register now"],
        attributes={"data-action": "login", "data-track": "button_click"}
    )
    
    # Candidate 2: Different button
    candidate2 = ElementProperties(
        tag="button",
        id="cancel-btn",
        name="cancel",
        type="button",
        class_name="btn btn-secondary",
        visible_text="Cancel",
        location_x=250,
        location_y=200,
        width=100,
        height=40,
        is_button=True,
        neighbor_texts=["Back to home"],
        attributes={"data-action": "cancel"}
    )
    
    scorer = SimilarityScorer()
    
    score1 = scorer.calculate_similarity(target, candidate1)
    score2 = scorer.calculate_similarity(target, candidate2)
    
    print(f"Candidate 1 (similar button) similarity: {score1:.3f}")
    print(f"Candidate 2 (different button) similarity: {score2:.3f}")
    
    # Find best match
    best_match = scorer.find_best_match(target, [candidate1, candidate2])
    if best_match:
        match, score = best_match
        print(f"\nBest match: {match.visible_text} (score: {score:.3f})")
