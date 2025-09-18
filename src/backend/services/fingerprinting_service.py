"""Element fingerprinting service for test self-healing system."""

import hashlib
import json
import logging
from typing import Dict, List, Optional, Any, Set
from datetime import datetime, timedelta
from pathlib import Path

from ..core.models.healing_models import ElementFingerprint, MatchResult
from .dom_analyzer import DOMAnalyzer


logger = logging.getLogger(__name__)


class FingerprintingService:
    """Service for creating, storing, and matching element fingerprints."""
    
    def __init__(self, storage_path: Optional[str] = None):
        """Initialize the fingerprinting service.
        
        Args:
            storage_path: Path to store fingerprint data. If None, uses default.
        """
        self.storage_path = Path(storage_path or "data/fingerprints")
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.dom_analyzer = DOMAnalyzer()
        self._fingerprint_cache: Dict[str, ElementFingerprint] = {}
        
    def create_fingerprint(self, element_info: Dict[str, Any]) -> ElementFingerprint:
        """Create an ElementFingerprint from element information.
        
        Args:
            element_info: Dictionary containing element details with keys:
                - tag_name: HTML tag name
                - attributes: Dict of element attributes
                - text_content: Text content of the element
                - dom_context: Full DOM context for analysis
                - locator: Original locator that found this element
                
        Returns:
            ElementFingerprint object
        """
        try:
            # Extract basic element information
            tag_name = element_info.get("tag_name", "").lower()
            attributes = element_info.get("attributes", {})
            text_content = element_info.get("text_content", "").strip()
            dom_context = element_info.get("dom_context", "")
            
            # Analyze DOM context to extract parent and sibling information
            parent_context = self.dom_analyzer.extract_parent_context(
                dom_context, element_info.get("locator", "")
            )
            sibling_context = self.dom_analyzer.extract_sibling_context(
                dom_context, element_info.get("locator", "")
            )
            dom_path = self.dom_analyzer.generate_dom_path(
                dom_context, element_info.get("locator", "")
            )
            
            # Generate visual hash if visual properties are available
            visual_hash = None
            if "visual_properties" in element_info:
                visual_hash = self._generate_visual_hash(
                    element_info["visual_properties"]
                )
            
            fingerprint = ElementFingerprint(
                tag_name=tag_name,
                attributes=self._normalize_attributes(attributes),
                text_content=text_content,
                parent_context=parent_context,
                sibling_context=sibling_context,
                dom_path=dom_path,
                visual_hash=visual_hash
            )
            
            logger.debug(f"Created fingerprint for {tag_name} element")
            return fingerprint
            
        except Exception as e:
            logger.error(f"Failed to create fingerprint: {e}")
            raise
    
    def store_fingerprint(self, test_id: str, step_id: str, 
                         fingerprint: ElementFingerprint) -> None:
        """Store a fingerprint for later retrieval.
        
        Args:
            test_id: Unique identifier for the test
            step_id: Unique identifier for the test step
            fingerprint: ElementFingerprint to store
        """
        try:
            storage_key = f"{test_id}_{step_id}"
            storage_file = self.storage_path / f"{storage_key}.json"
            
            # Store fingerprint data
            fingerprint_data = {
                "test_id": test_id,
                "step_id": step_id,
                "fingerprint": fingerprint.to_dict(),
                "stored_at": datetime.now().isoformat()
            }
            
            with open(storage_file, 'w', encoding='utf-8') as f:
                json.dump(fingerprint_data, f, indent=2, ensure_ascii=False)
            
            # Cache the fingerprint
            self._fingerprint_cache[storage_key] = fingerprint
            
            logger.debug(f"Stored fingerprint for {test_id}:{step_id}")
            
        except Exception as e:
            logger.error(f"Failed to store fingerprint: {e}")
            raise
    
    def retrieve_fingerprint(self, test_id: str, step_id: str) -> Optional[ElementFingerprint]:
        """Retrieve a stored fingerprint.
        
        Args:
            test_id: Unique identifier for the test
            step_id: Unique identifier for the test step
            
        Returns:
            ElementFingerprint if found, None otherwise
        """
        try:
            storage_key = f"{test_id}_{step_id}"
            
            # Check cache first
            if storage_key in self._fingerprint_cache:
                return self._fingerprint_cache[storage_key]
            
            # Load from storage
            storage_file = self.storage_path / f"{storage_key}.json"
            if not storage_file.exists():
                return None
            
            with open(storage_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            fingerprint = ElementFingerprint.from_dict(data["fingerprint"])
            
            # Cache for future use
            self._fingerprint_cache[storage_key] = fingerprint
            
            logger.debug(f"Retrieved fingerprint for {test_id}:{step_id}")
            return fingerprint
            
        except Exception as e:
            logger.error(f"Failed to retrieve fingerprint: {e}")
            return None
    
    def match_fingerprint(self, current_dom: str, 
                         fingerprint: ElementFingerprint) -> MatchResult:
        """Match a fingerprint against current DOM to find similar elements.
        
        Args:
            current_dom: Current DOM content as string
            fingerprint: ElementFingerprint to match against
            
        Returns:
            MatchResult with matching information
        """
        try:
            # Find potential matching elements in current DOM
            candidates = self.dom_analyzer.find_similar_elements(
                current_dom, fingerprint
            )
            
            if not candidates:
                return MatchResult(
                    matched=False,
                    confidence_score=0.0,
                    matching_elements=[],
                    best_match_locator=None
                )
            
            # Score each candidate
            scored_candidates = []
            for candidate in candidates:
                score = self._calculate_match_score(fingerprint, candidate)
                scored_candidates.append((candidate, score))
            
            # Sort by score (highest first)
            scored_candidates.sort(key=lambda x: x[1], reverse=True)
            best_candidate, best_score = scored_candidates[0]
            
            # Generate locators for matching elements
            matching_locators = [
                self.dom_analyzer.generate_locator_for_element(candidate)
                for candidate, _ in scored_candidates
            ]
            
            match_details = {
                "total_candidates": len(candidates),
                "scored_candidates": len(scored_candidates),
                "best_score": best_score,
                "score_breakdown": self._get_score_breakdown(fingerprint, best_candidate)
            }
            
            return MatchResult(
                matched=best_score >= 0.7,  # Configurable threshold
                confidence_score=best_score,
                matching_elements=matching_locators,
                best_match_locator=matching_locators[0] if matching_locators else None,
                match_details=match_details
            )
            
        except Exception as e:
            logger.error(f"Failed to match fingerprint: {e}")
            return MatchResult(
                matched=False,
                confidence_score=0.0,
                matching_elements=[],
                best_match_locator=None
            )
    
    def cleanup_old_fingerprints(self, retention_days: int = 7) -> int:
        """Clean up old fingerprint files.
        
        Args:
            retention_days: Number of days to retain fingerprints
            
        Returns:
            Number of files cleaned up
        """
        try:
            cutoff_date = datetime.now() - timedelta(days=retention_days)
            cleaned_count = 0
            
            for fingerprint_file in self.storage_path.glob("*.json"):
                try:
                    with open(fingerprint_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    
                    stored_at = datetime.fromisoformat(data["stored_at"])
                    if stored_at < cutoff_date:
                        fingerprint_file.unlink()
                        cleaned_count += 1
                        
                        # Remove from cache if present
                        cache_key = fingerprint_file.stem
                        self._fingerprint_cache.pop(cache_key, None)
                        
                except Exception as e:
                    logger.warning(f"Failed to process {fingerprint_file}: {e}")
                    continue
            
            logger.info(f"Cleaned up {cleaned_count} old fingerprint files")
            return cleaned_count
            
        except Exception as e:
            logger.error(f"Failed to cleanup fingerprints: {e}")
            return 0
    
    def _normalize_attributes(self, attributes: Dict[str, str]) -> Dict[str, str]:
        """Normalize element attributes for consistent fingerprinting.
        
        Args:
            attributes: Raw element attributes
            
        Returns:
            Normalized attributes dictionary
        """
        normalized = {}
        
        # Important attributes to preserve
        important_attrs = {'id', 'class', 'name', 'type', 'role', 'data-testid'}
        
        for key, value in attributes.items():
            if key.lower() in important_attrs:
                # Normalize class names (sort them)
                if key.lower() == 'class':
                    # Handle both string and list formats (BeautifulSoup returns lists for class)
                    if isinstance(value, list):
                        classes = sorted(value)
                    else:
                        classes = sorted(str(value).split())
                    normalized[key] = ' '.join(classes)
                else:
                    # Convert to string to handle any non-string values
                    normalized[key] = str(value)
        
        return normalized
    
    def _generate_visual_hash(self, visual_properties: Dict[str, Any]) -> str:
        """Generate a hash from visual properties.
        
        Args:
            visual_properties: Dictionary of visual properties
            
        Returns:
            Hash string representing visual signature
        """
        # Extract relevant visual properties
        relevant_props = {
            'width': visual_properties.get('width'),
            'height': visual_properties.get('height'),
            'x': visual_properties.get('x'),
            'y': visual_properties.get('y'),
            'visible': visual_properties.get('visible'),
            'color': visual_properties.get('color'),
            'background_color': visual_properties.get('background_color')
        }
        
        # Create hash from properties
        props_str = json.dumps(relevant_props, sort_keys=True)
        return hashlib.md5(props_str.encode()).hexdigest()
    
    def _calculate_match_score(self, fingerprint: ElementFingerprint, 
                              candidate: Dict[str, Any]) -> float:
        """Calculate match score between fingerprint and candidate element.
        
        Args:
            fingerprint: Original element fingerprint
            candidate: Candidate element information
            
        Returns:
            Match score between 0.0 and 1.0
        """
        score = 0.0
        total_weight = 0.0
        
        # Tag name match (weight: 0.2)
        if fingerprint.tag_name == candidate.get('tag_name', '').lower():
            score += 0.2
        total_weight += 0.2
        
        # Attributes match (weight: 0.3)
        attr_score = self._calculate_attribute_score(
            fingerprint.attributes, candidate.get('attributes', {})
        )
        score += attr_score * 0.3
        total_weight += 0.3
        
        # Text content match (weight: 0.2)
        text_score = self._calculate_text_score(
            fingerprint.text_content, candidate.get('text_content', '')
        )
        score += text_score * 0.2
        total_weight += 0.2
        
        # Parent context match (weight: 0.15)
        parent_score = self._calculate_context_score(
            fingerprint.parent_context, candidate.get('parent_context', [])
        )
        score += parent_score * 0.15
        total_weight += 0.15
        
        # Sibling context match (weight: 0.15)
        sibling_score = self._calculate_context_score(
            fingerprint.sibling_context, candidate.get('sibling_context', [])
        )
        score += sibling_score * 0.15
        total_weight += 0.15
        
        return score / total_weight if total_weight > 0 else 0.0
    
    def _calculate_attribute_score(self, fp_attrs: Dict[str, str], 
                                  cand_attrs: Dict[str, str]) -> float:
        """Calculate attribute similarity score."""
        if not fp_attrs and not cand_attrs:
            return 1.0
        
        if not fp_attrs or not cand_attrs:
            return 0.0
        
        # Normalize candidate attributes (handle BeautifulSoup attribute format)
        raw_cand_attrs = {}
        for key, value in cand_attrs.items():
            if isinstance(value, list):
                # BeautifulSoup returns class as list
                raw_cand_attrs[key] = ' '.join(value) if key.lower() == 'class' else str(value[0]) if value else ''
            else:
                raw_cand_attrs[key] = str(value)
        
        norm_cand_attrs = self._normalize_attributes(raw_cand_attrs)
        
        matches = 0
        total = len(fp_attrs)
        
        for key, value in fp_attrs.items():
            if key in norm_cand_attrs and norm_cand_attrs[key] == value:
                matches += 1
        
        return matches / total if total > 0 else 0.0
    
    def _calculate_text_score(self, fp_text: str, cand_text: str) -> float:
        """Calculate text content similarity score."""
        fp_text = fp_text.strip().lower()
        cand_text = cand_text.strip().lower()
        
        if not fp_text and not cand_text:
            return 1.0
        
        if not fp_text or not cand_text:
            return 0.0
        
        if fp_text == cand_text:
            return 1.0
        
        # Simple similarity based on common words
        fp_words = set(fp_text.split())
        cand_words = set(cand_text.split())
        
        if not fp_words or not cand_words:
            return 0.0
        
        intersection = fp_words.intersection(cand_words)
        union = fp_words.union(cand_words)
        
        return len(intersection) / len(union)
    
    def _calculate_context_score(self, fp_context: List[str], 
                                cand_context: List[str]) -> float:
        """Calculate context similarity score."""
        if not fp_context and not cand_context:
            return 1.0
        
        if not fp_context or not cand_context:
            return 0.0
        
        # Calculate overlap between contexts
        fp_set = set(fp_context)
        cand_set = set(cand_context)
        
        intersection = fp_set.intersection(cand_set)
        union = fp_set.union(cand_set)
        
        return len(intersection) / len(union) if union else 0.0
    
    def _get_score_breakdown(self, fingerprint: ElementFingerprint, 
                           candidate: Dict[str, Any]) -> Dict[str, float]:
        """Get detailed score breakdown for debugging."""
        return {
            "tag_match": 1.0 if fingerprint.tag_name == candidate.get('tag_name', '').lower() else 0.0,
            "attribute_score": self._calculate_attribute_score(
                fingerprint.attributes, candidate.get('attributes', {})
            ),
            "text_score": self._calculate_text_score(
                fingerprint.text_content, candidate.get('text_content', '')
            ),
            "parent_score": self._calculate_context_score(
                fingerprint.parent_context, candidate.get('parent_context', [])
            ),
            "sibling_score": self._calculate_context_score(
                fingerprint.sibling_context, candidate.get('sibling_context', [])
            )
        }