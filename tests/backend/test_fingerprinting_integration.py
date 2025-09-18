"""Integration tests for the fingerprinting system."""

import tempfile
import pytest
from pathlib import Path

from src.backend.services.fingerprinting_service import FingerprintingService
from src.backend.core.models.healing_models import ElementFingerprint


class TestFingerprintingIntegration:
    """Integration test cases for the complete fingerprinting workflow."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.service = FingerprintingService(storage_path=self.temp_dir)
        
        # Sample web page DOM
        self.original_dom = """
        <html>
            <head><title>Login Page</title></head>
            <body>
                <div class="container">
                    <form id="login-form" class="auth-form">
                        <h2>Please Login</h2>
                        <div class="form-group">
                            <label for="email">Email:</label>
                            <input id="email" name="email" type="email" class="form-control" placeholder="Enter email" />
                        </div>
                        <div class="form-group">
                            <label for="password">Password:</label>
                            <input id="password" name="password" type="password" class="form-control" placeholder="Enter password" />
                        </div>
                        <div class="form-actions">
                            <button id="login-btn" type="submit" class="btn btn-primary">Login</button>
                            <a href="/forgot-password" class="forgot-link">Forgot Password?</a>
                        </div>
                    </form>
                </div>
            </body>
        </html>
        """
        
        # Modified DOM (simulating UI changes)
        self.modified_dom = """
        <html>
            <head><title>Login Page - Updated</title></head>
            <body>
                <div class="main-container">
                    <form id="signin-form" class="auth-form modern">
                        <h2>Sign In</h2>
                        <div class="input-group">
                            <label for="user-email">Email Address:</label>
                            <input id="user-email" name="email" type="email" class="input-field" placeholder="Your email" />
                        </div>
                        <div class="input-group">
                            <label for="user-password">Password:</label>
                            <input id="user-password" name="password" type="password" class="input-field" placeholder="Your password" />
                        </div>
                        <div class="button-group">
                            <button id="signin-btn" type="submit" class="button primary-button">Sign In</button>
                            <a href="/reset-password" class="reset-link">Reset Password?</a>
                        </div>
                    </form>
                </div>
            </body>
        </html>
        """
    
    def test_complete_fingerprinting_workflow(self):
        """Test the complete fingerprinting workflow from creation to matching."""
        
        # Step 1: Create fingerprint from original element
        original_element_info = {
            "tag_name": "button",
            "attributes": {
                "id": "login-btn",
                "type": "submit",
                "class": "btn btn-primary"
            },
            "text_content": "Login",
            "dom_context": self.original_dom,
            "locator": "id=login-btn"
        }
        
        fingerprint = self.service.create_fingerprint(original_element_info)
        
        # Verify fingerprint was created correctly
        assert fingerprint.tag_name == "button"
        assert fingerprint.attributes["id"] == "login-btn"
        assert fingerprint.text_content == "Login"
        assert len(fingerprint.parent_context) > 0
        assert len(fingerprint.dom_path) > 0
        
        # Step 2: Store the fingerprint
        test_id = "login_test"
        step_id = "click_login_button"
        self.service.store_fingerprint(test_id, step_id, fingerprint)
        
        # Verify storage
        storage_file = Path(self.temp_dir) / f"{test_id}_{step_id}.json"
        assert storage_file.exists()
        
        # Step 3: Retrieve the fingerprint
        retrieved_fingerprint = self.service.retrieve_fingerprint(test_id, step_id)
        assert retrieved_fingerprint is not None
        assert retrieved_fingerprint.tag_name == fingerprint.tag_name
        
        # Step 4: Match against modified DOM
        match_result = self.service.match_fingerprint(self.modified_dom, fingerprint)
        
        # Verify matching found the updated button (even if confidence is low due to major changes)
        assert match_result.confidence_score > 0.2  # Should have some confidence
        assert len(match_result.matching_elements) > 0
        assert match_result.best_match_locator is not None
        
        # The best match should be the signin button
        assert "signin-btn" in match_result.best_match_locator
    
    def test_fingerprinting_with_similar_elements(self):
        """Test fingerprinting when multiple similar elements exist."""
        
        # DOM with multiple buttons
        multi_button_dom = """
        <html>
            <body>
                <div class="toolbar">
                    <button id="save-btn" class="btn btn-primary">Save</button>
                    <button id="cancel-btn" class="btn btn-secondary">Cancel</button>
                    <button id="delete-btn" class="btn btn-danger">Delete</button>
                </div>
                <div class="content">
                    <button id="submit-btn" class="btn btn-primary">Submit</button>
                    <button id="reset-btn" class="btn btn-secondary">Reset</button>
                </div>
            </body>
        </html>
        """
        
        # Create fingerprint for the submit button
        submit_element_info = {
            "tag_name": "button",
            "attributes": {
                "id": "submit-btn",
                "class": "btn btn-primary"
            },
            "text_content": "Submit",
            "dom_context": multi_button_dom,
            "locator": "id=submit-btn"
        }
        
        fingerprint = self.service.create_fingerprint(submit_element_info)
        
        # Match against the same DOM
        match_result = self.service.match_fingerprint(multi_button_dom, fingerprint)
        
        # Should find multiple button candidates but match the correct one
        assert match_result.confidence_score > 0.5  # Should have good confidence for exact match
        assert len(match_result.matching_elements) >= 1
        
        # Best match should be the submit button
        assert "submit-btn" in match_result.best_match_locator
    
    def test_fingerprinting_no_match_scenario(self):
        """Test fingerprinting when no suitable match exists."""
        
        # Create fingerprint for a video element
        video_element_info = {
            "tag_name": "video",
            "attributes": {
                "id": "main-video",
                "controls": "true"
            },
            "text_content": "",
            "dom_context": '<video id="main-video" controls>Video content</video>',
            "locator": "id=main-video"
        }
        
        fingerprint = self.service.create_fingerprint(video_element_info)
        
        # Try to match against DOM with no video elements
        match_result = self.service.match_fingerprint(self.original_dom, fingerprint)
        
        # Should not find any matches
        assert match_result.matched is False
        assert match_result.confidence_score == 0.0
        assert len(match_result.matching_elements) == 0
        assert match_result.best_match_locator is None
    
    def test_fingerprinting_with_context_changes(self):
        """Test fingerprinting when element context changes significantly."""
        
        # Original DOM structure
        original_context_dom = """
        <div class="page">
            <header class="header">
                <nav class="navigation">
                    <button id="menu-btn" class="nav-button">Menu</button>
                </nav>
            </header>
        </div>
        """
        
        # Modified DOM with different structure but same button
        modified_context_dom = """
        <div class="app">
            <aside class="sidebar">
                <div class="menu-section">
                    <button id="menu-btn" class="nav-button">Menu</button>
                </div>
            </aside>
        </div>
        """
        
        # Create fingerprint from original context
        menu_element_info = {
            "tag_name": "button",
            "attributes": {
                "id": "menu-btn",
                "class": "nav-button"
            },
            "text_content": "Menu",
            "dom_context": original_context_dom,
            "locator": "id=menu-btn"
        }
        
        fingerprint = self.service.create_fingerprint(menu_element_info)
        
        # Match against modified context
        match_result = self.service.match_fingerprint(modified_context_dom, fingerprint)
        
        # Should still match based on element attributes and text
        assert match_result.confidence_score > 0.5  # Good match despite context change
        assert "menu-btn" in match_result.best_match_locator
    
    def test_fingerprinting_cleanup_workflow(self):
        """Test the fingerprint cleanup workflow."""
        
        # Create and store multiple fingerprints
        for i in range(5):
            element_info = {
                "tag_name": "button",
                "attributes": {"id": f"btn-{i}"},
                "text_content": f"Button {i}",
                "dom_context": f'<button id="btn-{i}">Button {i}</button>',
                "locator": f"id=btn-{i}"
            }
            
            fingerprint = self.service.create_fingerprint(element_info)
            self.service.store_fingerprint(f"test_{i}", "step_1", fingerprint)
        
        # Verify all files were created
        storage_files = list(Path(self.temp_dir).glob("*.json"))
        assert len(storage_files) == 5
        
        # Cleanup with 0 day retention (should remove all)
        cleaned_count = self.service.cleanup_old_fingerprints(retention_days=0)
        
        # Verify cleanup
        assert cleaned_count == 5
        remaining_files = list(Path(self.temp_dir).glob("*.json"))
        assert len(remaining_files) == 0
    
    def test_fingerprinting_error_recovery(self):
        """Test error recovery in fingerprinting workflow."""
        
        # Test with malformed DOM
        malformed_element_info = {
            "tag_name": "button",
            "attributes": {"id": "test-btn"},
            "text_content": "Test",
            "dom_context": "<<malformed>>dom<<content>>",
            "locator": "id=test-btn"
        }
        
        # Should not raise exception, but create basic fingerprint
        fingerprint = self.service.create_fingerprint(malformed_element_info)
        assert fingerprint.tag_name == "button"
        assert fingerprint.attributes["id"] == "test-btn"
        
        # Matching against invalid DOM should return failed result
        match_result = self.service.match_fingerprint("invalid<dom>", fingerprint)
        assert match_result.matched is False
        assert match_result.confidence_score == 0.0