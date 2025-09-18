"""Unit tests for the fingerprinting service."""

import json
import tempfile
import pytest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from src.backend.services.fingerprinting_service import FingerprintingService
from src.backend.core.models.healing_models import ElementFingerprint, MatchResult


class TestFingerprintingService:
    """Test cases for FingerprintingService."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.service = FingerprintingService(storage_path=self.temp_dir)
        
        # Sample element information
        self.sample_element_info = {
            "tag_name": "button",
            "attributes": {
                "id": "submit-btn",
                "class": "btn btn-primary",
                "type": "button"
            },
            "text_content": "Submit Form",
            "dom_context": """
            <html>
                <body>
                    <div class="container">
                        <form id="test-form">
                            <input name="username" type="text" />
                            <button id="submit-btn" class="btn btn-primary" type="button">Submit Form</button>
                        </form>
                    </div>
                </body>
            </html>
            """,
            "locator": "id=submit-btn"
        }
        
        # Sample DOM for matching tests
        self.sample_dom = """
        <html>
            <body>
                <div class="container">
                    <form id="test-form">
                        <input name="username" type="text" />
                        <button id="submit-btn" class="btn btn-primary" type="button">Submit Form</button>
                        <button id="cancel-btn" class="btn btn-secondary" type="button">Cancel</button>
                    </form>
                    <div class="sidebar">
                        <button class="btn btn-primary" type="button">Another Button</button>
                    </div>
                </div>
            </body>
        </html>
        """
    
    def test_create_fingerprint_basic(self):
        """Test basic fingerprint creation."""
        fingerprint = self.service.create_fingerprint(self.sample_element_info)
        
        assert isinstance(fingerprint, ElementFingerprint)
        assert fingerprint.tag_name == "button"
        assert fingerprint.attributes["id"] == "submit-btn"
        assert fingerprint.text_content == "Submit Form"
        assert len(fingerprint.parent_context) > 0
        assert fingerprint.dom_path != ""
    
    def test_create_fingerprint_with_visual_properties(self):
        """Test fingerprint creation with visual properties."""
        element_info = self.sample_element_info.copy()
        element_info["visual_properties"] = {
            "width": 100,
            "height": 30,
            "x": 50,
            "y": 200,
            "visible": True,
            "color": "#ffffff",
            "background_color": "#007bff"
        }
        
        fingerprint = self.service.create_fingerprint(element_info)
        
        assert fingerprint.visual_hash is not None
        assert len(fingerprint.visual_hash) == 32  # MD5 hash length
    
    def test_create_fingerprint_minimal_info(self):
        """Test fingerprint creation with minimal element information."""
        minimal_info = {
            "tag_name": "div",
            "attributes": {},
            "text_content": "",
            "dom_context": "<div></div>",
            "locator": "css=div"
        }
        
        fingerprint = self.service.create_fingerprint(minimal_info)
        
        assert fingerprint.tag_name == "div"
        assert fingerprint.attributes == {}
        assert fingerprint.text_content == ""
    
    def test_store_and_retrieve_fingerprint(self):
        """Test storing and retrieving fingerprints."""
        fingerprint = self.service.create_fingerprint(self.sample_element_info)
        test_id = "test_login"
        step_id = "click_submit"
        
        # Store fingerprint
        self.service.store_fingerprint(test_id, step_id, fingerprint)
        
        # Verify file was created
        storage_file = Path(self.temp_dir) / f"{test_id}_{step_id}.json"
        assert storage_file.exists()
        
        # Retrieve fingerprint
        retrieved = self.service.retrieve_fingerprint(test_id, step_id)
        
        assert retrieved is not None
        assert retrieved.tag_name == fingerprint.tag_name
        assert retrieved.attributes == fingerprint.attributes
        assert retrieved.text_content == fingerprint.text_content
    
    def test_retrieve_nonexistent_fingerprint(self):
        """Test retrieving a fingerprint that doesn't exist."""
        result = self.service.retrieve_fingerprint("nonexistent", "step")
        assert result is None
    
    def test_fingerprint_caching(self):
        """Test that fingerprints are cached properly."""
        fingerprint = self.service.create_fingerprint(self.sample_element_info)
        test_id = "test_cache"
        step_id = "step1"
        
        # Store fingerprint
        self.service.store_fingerprint(test_id, step_id, fingerprint)
        
        # First retrieval (from file)
        retrieved1 = self.service.retrieve_fingerprint(test_id, step_id)
        
        # Second retrieval (from cache)
        retrieved2 = self.service.retrieve_fingerprint(test_id, step_id)
        
        assert retrieved1 is not None
        assert retrieved2 is not None
        assert retrieved1.tag_name == retrieved2.tag_name
    
    @patch('src.backend.services.fingerprinting_service.DOMAnalyzer')
    def test_match_fingerprint_success(self, mock_dom_analyzer):
        """Test successful fingerprint matching."""
        # Mock DOM analyzer
        mock_analyzer = Mock()
        mock_dom_analyzer.return_value = mock_analyzer
        
        # Mock candidate elements
        mock_candidates = [
            {
                "tag_name": "button",
                "attributes": {"id": "submit-btn", "class": "btn btn-primary"},
                "text_content": "Submit Form",
                "parent_context": ["form#test-form", "div.container"],
                "sibling_context": ["prev:input[name=username]"]
            }
        ]
        mock_analyzer.find_similar_elements.return_value = mock_candidates
        mock_analyzer.generate_locator_for_element.return_value = "id=submit-btn"
        
        # Create service with mocked analyzer
        service = FingerprintingService(storage_path=self.temp_dir)
        
        # Create fingerprint
        fingerprint = ElementFingerprint(
            tag_name="button",
            attributes={"id": "submit-btn", "class": "btn btn-primary"},
            text_content="Submit Form",
            parent_context=["form#test-form", "div.container"],
            sibling_context=["prev:input[name=username]"],
            dom_path="html > body > div.container > form#test-form > button#submit-btn"
        )
        
        # Test matching
        result = service.match_fingerprint(self.sample_dom, fingerprint)
        
        assert isinstance(result, MatchResult)
        assert result.matched is True
        assert result.confidence_score > 0.7
        assert len(result.matching_elements) > 0
        assert result.best_match_locator == "id=submit-btn"
    
    @patch('src.backend.services.fingerprinting_service.DOMAnalyzer')
    def test_match_fingerprint_no_candidates(self, mock_dom_analyzer):
        """Test fingerprint matching when no candidates are found."""
        # Mock DOM analyzer to return no candidates
        mock_analyzer = Mock()
        mock_dom_analyzer.return_value = mock_analyzer
        mock_analyzer.find_similar_elements.return_value = []
        
        service = FingerprintingService(storage_path=self.temp_dir)
        
        fingerprint = ElementFingerprint(
            tag_name="span",
            attributes={"class": "nonexistent"},
            text_content="Not Found",
            parent_context=[],
            sibling_context=[],
            dom_path=""
        )
        
        result = service.match_fingerprint(self.sample_dom, fingerprint)
        
        assert result.matched is False
        assert result.confidence_score == 0.0
        assert len(result.matching_elements) == 0
        assert result.best_match_locator is None
    
    def test_cleanup_old_fingerprints(self):
        """Test cleanup of old fingerprint files."""
        # Create some test fingerprints with different ages
        fingerprint = self.service.create_fingerprint(self.sample_element_info)
        
        # Create recent fingerprint
        self.service.store_fingerprint("recent_test", "step1", fingerprint)
        
        # Create old fingerprint by manually creating file with old timestamp
        old_data = {
            "test_id": "old_test",
            "step_id": "step1",
            "fingerprint": fingerprint.to_dict(),
            "stored_at": (datetime.now() - timedelta(days=10)).isoformat()
        }
        
        old_file = Path(self.temp_dir) / "old_test_step1.json"
        with open(old_file, 'w') as f:
            json.dump(old_data, f)
        
        # Verify both files exist
        recent_file = Path(self.temp_dir) / "recent_test_step1.json"
        assert recent_file.exists()
        assert old_file.exists()
        
        # Cleanup with 7 day retention
        cleaned_count = self.service.cleanup_old_fingerprints(retention_days=7)
        
        # Verify old file was removed, recent file remains
        assert cleaned_count == 1
        assert recent_file.exists()
        assert not old_file.exists()
    
    def test_normalize_attributes(self):
        """Test attribute normalization."""
        attributes = {
            "id": "test-id",
            "class": "btn primary large",
            "data-testid": "submit-button",
            "style": "color: red; background: blue;",  # Should be filtered out
            "onclick": "handleClick()"  # Should be filtered out
        }
        
        normalized = self.service._normalize_attributes(attributes)
        
        assert "id" in normalized
        assert "class" in normalized
        assert "data-testid" in normalized
        assert "style" not in normalized
        assert "onclick" not in normalized
        
        # Class should be sorted
        assert normalized["class"] == "btn large primary"
    
    def test_calculate_match_score_perfect_match(self):
        """Test match score calculation for perfect match."""
        fingerprint = ElementFingerprint(
            tag_name="button",
            attributes={"id": "test", "class": "btn primary"},
            text_content="Click Me",
            parent_context=["div.container"],
            sibling_context=["prev:input"],
            dom_path=""
        )
        
        candidate = {
            "tag_name": "button",
            "attributes": {"id": "test", "class": "btn primary"},
            "text_content": "Click Me",
            "parent_context": ["div.container"],
            "sibling_context": ["prev:input"]
        }
        
        score = self.service._calculate_match_score(fingerprint, candidate)
        assert score == 1.0
    
    def test_calculate_match_score_partial_match(self):
        """Test match score calculation for partial match."""
        fingerprint = ElementFingerprint(
            tag_name="button",
            attributes={"id": "test", "class": "btn primary"},
            text_content="Click Me",
            parent_context=["div.container"],
            sibling_context=["prev:input"],
            dom_path=""
        )
        
        candidate = {
            "tag_name": "button",  # Same tag
            "attributes": {"id": "test"},  # Partial attributes
            "text_content": "Different Text",  # Different text
            "parent_context": ["div.container"],  # Same parent
            "sibling_context": ["prev:span"]  # Different sibling
        }
        
        score = self.service._calculate_match_score(fingerprint, candidate)
        assert 0.0 < score < 1.0
    
    def test_calculate_match_score_no_match(self):
        """Test match score calculation for no match."""
        fingerprint = ElementFingerprint(
            tag_name="button",
            attributes={"id": "test"},
            text_content="Click Me",
            parent_context=["div.container"],
            sibling_context=["prev:input"],
            dom_path=""
        )
        
        candidate = {
            "tag_name": "span",  # Different tag
            "attributes": {"class": "different"},  # Different attributes
            "text_content": "Different Text",  # Different text
            "parent_context": ["section.sidebar"],  # Different parent
            "sibling_context": ["next:div"]  # Different sibling
        }
        
        score = self.service._calculate_match_score(fingerprint, candidate)
        assert score < 0.5  # Should be low score
    
    def test_calculate_attribute_score(self):
        """Test attribute similarity scoring."""
        fp_attrs = {"id": "test", "class": "btn primary"}
        
        # Perfect match
        cand_attrs = {"id": "test", "class": "btn primary"}
        score = self.service._calculate_attribute_score(fp_attrs, cand_attrs)
        assert score == 1.0
        
        # Partial match
        cand_attrs = {"id": "test", "class": "btn secondary"}
        score = self.service._calculate_attribute_score(fp_attrs, cand_attrs)
        assert score == 0.5
        
        # No match
        cand_attrs = {"data-test": "different"}
        score = self.service._calculate_attribute_score(fp_attrs, cand_attrs)
        assert score == 0.0
    
    def test_calculate_text_score(self):
        """Test text content similarity scoring."""
        # Exact match
        score = self.service._calculate_text_score("Click Me", "Click Me")
        assert score == 1.0
        
        # Case insensitive match
        score = self.service._calculate_text_score("Click Me", "click me")
        assert score == 1.0
        
        # Partial match (common words)
        score = self.service._calculate_text_score("Click Me Now", "Click Here Now")
        assert 0.0 < score < 1.0
        
        # No match
        score = self.service._calculate_text_score("Click Me", "Submit Form")
        assert score == 0.0
        
        # Empty strings
        score = self.service._calculate_text_score("", "")
        assert score == 1.0
    
    def test_calculate_context_score(self):
        """Test context similarity scoring."""
        fp_context = ["div.container", "form#test"]
        
        # Perfect match
        cand_context = ["div.container", "form#test"]
        score = self.service._calculate_context_score(fp_context, cand_context)
        assert score == 1.0
        
        # Partial match
        cand_context = ["div.container", "section.main"]
        score = self.service._calculate_context_score(fp_context, cand_context)
        assert 0.0 < score < 1.0
        
        # No match
        cand_context = ["section.sidebar", "nav.menu"]
        score = self.service._calculate_context_score(fp_context, cand_context)
        assert score == 0.0
        
        # Empty contexts
        score = self.service._calculate_context_score([], [])
        assert score == 1.0
    
    def test_generate_visual_hash(self):
        """Test visual hash generation."""
        visual_props = {
            "width": 100,
            "height": 30,
            "x": 50,
            "y": 200,
            "visible": True,
            "color": "#ffffff",
            "background_color": "#007bff"
        }
        
        hash1 = self.service._generate_visual_hash(visual_props)
        hash2 = self.service._generate_visual_hash(visual_props)
        
        # Same properties should generate same hash
        assert hash1 == hash2
        assert len(hash1) == 32  # MD5 hash length
        
        # Different properties should generate different hash
        visual_props["width"] = 200
        hash3 = self.service._generate_visual_hash(visual_props)
        assert hash1 != hash3
    
    def test_error_handling_invalid_dom(self):
        """Test error handling with invalid DOM content."""
        fingerprint = self.service.create_fingerprint(self.sample_element_info)
        
        # Test with invalid DOM
        result = self.service.match_fingerprint("invalid<>dom", fingerprint)
        
        # Should return failed match result, not raise exception
        assert result.matched is False
        assert result.confidence_score == 0.0
    
    def test_error_handling_storage_failure(self):
        """Test error handling when storage operations fail."""
        fingerprint = self.service.create_fingerprint(self.sample_element_info)
        
        # Try to store with invalid characters in filename (Windows doesn't allow certain chars)
        with pytest.raises(Exception):
            self.service.store_fingerprint("test/with/slashes", "step:with:colons", fingerprint)