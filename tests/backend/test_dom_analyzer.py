"""Unit tests for the DOM analyzer."""

import pytest
from bs4 import BeautifulSoup

from src.backend.services.dom_analyzer import DOMAnalyzer
from src.backend.core.models.healing_models import ElementFingerprint, LocatorStrategy


class TestDOMAnalyzer:
    """Test cases for DOMAnalyzer."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.analyzer = DOMAnalyzer()
        
        # Sample DOM for testing
        self.sample_dom = """
        <html>
            <head><title>Test Page</title></head>
            <body>
                <div class="container">
                    <header class="header">
                        <h1>Test Application</h1>
                        <nav class="navigation">
                            <a href="/home">Home</a>
                            <a href="/about">About</a>
                        </nav>
                    </header>
                    <main class="content">
                        <form id="login-form" class="form">
                            <div class="form-group">
                                <label for="username">Username:</label>
                                <input id="username" name="username" type="text" class="form-control" />
                            </div>
                            <div class="form-group">
                                <label for="password">Password:</label>
                                <input id="password" name="password" type="password" class="form-control" />
                            </div>
                            <div class="form-actions">
                                <button id="submit-btn" type="submit" class="btn btn-primary">Login</button>
                                <button id="cancel-btn" type="button" class="btn btn-secondary">Cancel</button>
                            </div>
                        </form>
                    </main>
                    <footer class="footer">
                        <p>© 2024 Test App</p>
                    </footer>
                </div>
            </body>
        </html>
        """
    
    def test_extract_parent_context_with_id_locator(self):
        """Test extracting parent context using ID locator."""
        locator = "id=submit-btn"
        parent_context = self.analyzer.extract_parent_context(self.sample_dom, locator)
        
        assert len(parent_context) > 0
        assert any("form-actions" in context for context in parent_context)
        assert any("login-form" in context for context in parent_context)
        assert any("content" in context for context in parent_context)
    
    def test_extract_parent_context_with_css_locator(self):
        """Test extracting parent context using CSS locator."""
        locator = "css=.btn.btn-primary"
        parent_context = self.analyzer.extract_parent_context(self.sample_dom, locator)
        
        assert len(parent_context) > 0
        # Should find form-actions, form, main as parents
        assert len(parent_context) >= 2
    
    def test_extract_parent_context_nonexistent_element(self):
        """Test extracting parent context for nonexistent element."""
        locator = "id=nonexistent"
        parent_context = self.analyzer.extract_parent_context(self.sample_dom, locator)
        
        assert parent_context == []
    
    def test_extract_sibling_context_with_id_locator(self):
        """Test extracting sibling context using ID locator."""
        locator = "id=submit-btn"
        sibling_context = self.analyzer.extract_sibling_context(self.sample_dom, locator)
        
        assert len(sibling_context) > 0
        # Should find cancel button as next sibling
        assert any("next:" in context and "cancel-btn" in context for context in sibling_context)
    
    def test_extract_sibling_context_with_input_element(self):
        """Test extracting sibling context for input element."""
        locator = "id=username"
        sibling_context = self.analyzer.extract_sibling_context(self.sample_dom, locator)
        
        assert len(sibling_context) > 0
        # Should find label as previous sibling
        assert any("prev:" in context and "label" in context for context in sibling_context)
    
    def test_generate_dom_path_with_id(self):
        """Test DOM path generation for element with ID."""
        locator = "id=submit-btn"
        dom_path = self.analyzer.generate_dom_path(self.sample_dom, locator)
        
        assert "html" in dom_path
        assert "body" in dom_path
        assert "div.container" in dom_path
        assert "main.content" in dom_path
        assert "form#login-form" in dom_path
        assert "button#submit-btn" in dom_path
        assert " > " in dom_path  # Should use proper separator
    
    def test_generate_dom_path_with_class(self):
        """Test DOM path generation for element with class."""
        locator = "css=.form-control"
        dom_path = self.analyzer.generate_dom_path(self.sample_dom, locator)
        
        assert "html" in dom_path
        assert "input.form-control" in dom_path or "input#username" in dom_path
    
    def test_find_similar_elements_by_tag(self):
        """Test finding similar elements by tag name."""
        fingerprint = ElementFingerprint(
            tag_name="button",
            attributes={},
            text_content="",
            parent_context=[],
            sibling_context=[],
            dom_path=""
        )
        
        candidates = self.analyzer.find_similar_elements(self.sample_dom, fingerprint)
        
        assert len(candidates) == 2  # Should find both buttons
        assert all(candidate["tag_name"] == "button" for candidate in candidates)
        
        # Check that both buttons are found
        button_ids = [candidate["attributes"].get("id") for candidate in candidates]
        assert "submit-btn" in button_ids
        assert "cancel-btn" in button_ids
    
    def test_find_similar_elements_no_matches(self):
        """Test finding similar elements when none exist."""
        fingerprint = ElementFingerprint(
            tag_name="video",  # No video elements in sample DOM
            attributes={},
            text_content="",
            parent_context=[],
            sibling_context=[],
            dom_path=""
        )
        
        candidates = self.analyzer.find_similar_elements(self.sample_dom, fingerprint)
        
        assert len(candidates) == 0
    
    def test_generate_locator_for_element_with_id(self):
        """Test locator generation for element with ID."""
        soup = BeautifulSoup(self.sample_dom, 'html.parser')
        element = soup.find(id="submit-btn")
        
        element_info = {"element": element}
        locator = self.analyzer.generate_locator_for_element(element_info)
        
        assert locator == "id=submit-btn"
    
    def test_generate_locator_for_element_with_name(self):
        """Test locator generation for element with name attribute."""
        soup = BeautifulSoup(self.sample_dom, 'html.parser')
        element = soup.find(attrs={"name": "username"})
        
        element_info = {"element": element}
        locator = self.analyzer.generate_locator_for_element(element_info)
        
        # Should prefer ID over name if both exist
        assert locator == "id=username"
    
    def test_generate_locator_for_element_with_class(self):
        """Test locator generation for element with only class."""
        # Create element without ID or name
        dom_with_class_only = """
        <div class="special-element unique-class">Content</div>
        """
        soup = BeautifulSoup(dom_with_class_only, 'html.parser')
        element = soup.find(class_="special-element")
        
        element_info = {"element": element}
        locator = self.analyzer.generate_locator_for_element(element_info)
        
        assert "css=" in locator
        assert "special-element" in locator
        assert "unique-class" in locator
    
    def test_generate_locator_for_element_fallback_xpath(self):
        """Test locator generation fallback to XPath."""
        # Create element with no unique attributes
        dom_simple = """
        <div>
            <p>First paragraph</p>
            <p>Second paragraph</p>
        </div>
        """
        soup = BeautifulSoup(dom_simple, 'html.parser')
        paragraphs = soup.find_all("p")
        element = paragraphs[1]  # Second paragraph
        
        element_info = {"element": element}
        locator = self.analyzer.generate_locator_for_element(element_info)
        
        # Should generate some form of locator
        assert locator != ""
        # Could be XPath or text-based
        assert "xpath=" in locator or "css=" in locator
    
    def test_parse_locator_id(self):
        """Test parsing ID locator."""
        strategy, value = self.analyzer._parse_locator("id=submit-btn")
        assert strategy == LocatorStrategy.ID
        assert value == "submit-btn"
        
        # Test with quotes
        strategy, value = self.analyzer._parse_locator('id="submit-btn"')
        assert strategy == LocatorStrategy.ID
        assert value == "submit-btn"
    
    def test_parse_locator_css(self):
        """Test parsing CSS locator."""
        strategy, value = self.analyzer._parse_locator("css=.btn.btn-primary")
        assert strategy == LocatorStrategy.CSS
        assert value == ".btn.btn-primary"
        
        # Test with quotes
        strategy, value = self.analyzer._parse_locator('css=".btn.btn-primary"')
        assert strategy == LocatorStrategy.CSS
        assert value == ".btn.btn-primary"
    
    def test_parse_locator_xpath(self):
        """Test parsing XPath locator."""
        strategy, value = self.analyzer._parse_locator("xpath=//button[@id='submit']")
        assert strategy == LocatorStrategy.XPATH
        assert value == "//button[@id='submit']"
    
    def test_parse_locator_no_strategy(self):
        """Test parsing locator without explicit strategy."""
        strategy, value = self.analyzer._parse_locator(".btn-primary")
        assert strategy == LocatorStrategy.CSS
        assert value == ".btn-primary"
    
    def test_find_element_by_locator_id(self):
        """Test finding element by ID locator."""
        soup = BeautifulSoup(self.sample_dom, 'html.parser')
        element = self.analyzer._find_element_by_locator(soup, "id=submit-btn")
        
        assert element is not None
        assert element.get("id") == "submit-btn"
        assert element.name == "button"
    
    def test_find_element_by_locator_css(self):
        """Test finding element by CSS locator."""
        soup = BeautifulSoup(self.sample_dom, 'html.parser')
        element = self.analyzer._find_element_by_locator(soup, "css=.btn.btn-primary")
        
        assert element is not None
        assert "btn" in element.get("class", [])
        assert "btn-primary" in element.get("class", [])
    
    def test_find_element_by_locator_name(self):
        """Test finding element by name locator."""
        soup = BeautifulSoup(self.sample_dom, 'html.parser')
        element = self.analyzer._find_element_by_locator(soup, "name=username")
        
        assert element is not None
        assert element.get("name") == "username"
        assert element.name == "input"
    
    def test_find_element_by_locator_nonexistent(self):
        """Test finding nonexistent element."""
        soup = BeautifulSoup(self.sample_dom, 'html.parser')
        element = self.analyzer._find_element_by_locator(soup, "id=nonexistent")
        
        assert element is None
    
    def test_describe_element_with_id(self):
        """Test element description with ID."""
        soup = BeautifulSoup(self.sample_dom, 'html.parser')
        element = soup.find(id="submit-btn")
        
        description = self.analyzer._describe_element(element)
        
        assert "button" in description
        assert "#submit-btn" in description
    
    def test_describe_element_with_class(self):
        """Test element description with class."""
        soup = BeautifulSoup(self.sample_dom, 'html.parser')
        element = soup.find(class_="container")
        
        description = self.analyzer._describe_element(element)
        
        assert "div" in description
        assert ".container" in description
    
    def test_describe_element_with_text(self):
        """Test element description with short text."""
        dom_with_text = '<button>Click</button>'
        soup = BeautifulSoup(dom_with_text, 'html.parser')
        element = soup.find("button")
        
        description = self.analyzer._describe_element(element)
        
        assert "button" in description
        assert "[Click]" in description
    
    def test_describe_element_with_long_text(self):
        """Test element description with long text (should be truncated)."""
        dom_with_long_text = '<button>This is a very long button text that should be truncated</button>'
        soup = BeautifulSoup(dom_with_long_text, 'html.parser')
        element = soup.find("button")
        
        description = self.analyzer._describe_element(element)
        
        assert "button" in description
        # Long text should not be included
        assert "[This is a very long" not in description
    
    def test_find_unique_attributes(self):
        """Test finding unique attributes for element."""
        soup = BeautifulSoup(self.sample_dom, 'html.parser')
        element = soup.find(id="username")
        
        unique_attrs = self.analyzer._find_unique_attributes(element)
        
        # Should find type attribute as unique
        assert "type" in unique_attrs
        assert unique_attrs["type"] == "text"
    
    def test_find_unique_attributes_with_data_testid(self):
        """Test finding unique attributes with data-testid."""
        dom_with_testid = '<button data-testid="submit-button" class="btn">Submit</button>'
        soup = BeautifulSoup(dom_with_testid, 'html.parser')
        element = soup.find("button")
        
        unique_attrs = self.analyzer._find_unique_attributes(element)
        
        assert "data-testid" in unique_attrs
        assert unique_attrs["data-testid"] == "submit-button"
    
    def test_generate_xpath_for_element(self):
        """Test XPath generation for element."""
        soup = BeautifulSoup(self.sample_dom, 'html.parser')
        element = soup.find(id="submit-btn")
        
        xpath = self.analyzer._generate_xpath_for_element(element)
        
        assert xpath.startswith("/")
        assert "button" in xpath
        # Should handle element position among siblings
        assert "[" in xpath or xpath.count("/") > 1
    
    def test_generate_xpath_for_unique_element(self):
        """Test XPath generation for unique element."""
        dom_simple = '<div><p>Only paragraph</p></div>'
        soup = BeautifulSoup(dom_simple, 'html.parser')
        element = soup.find("p")
        
        xpath = self.analyzer._generate_xpath_for_element(element)
        
        assert xpath == "/div/p"  # Should be simple path for unique element
    
    def test_find_by_xpath_simple(self):
        """Test finding element by simple XPath."""
        soup = BeautifulSoup(self.sample_dom, 'html.parser')
        
        # Test simple path
        element = self.analyzer._find_by_xpath(soup, "/html/body/div")
        assert element is not None
        assert element.get("class") == ["container"]
    
    def test_find_by_xpath_with_index(self):
        """Test finding element by XPath with index."""
        dom_with_multiple = """
        <div>
            <p>First</p>
            <p>Second</p>
            <p>Third</p>
        </div>
        """
        soup = BeautifulSoup(dom_with_multiple, 'html.parser')
        
        # Find second paragraph
        element = self.analyzer._find_by_xpath(soup, "/div/p[2]")
        assert element is not None
        assert element.get_text() == "Second"
    
    def test_find_by_xpath_nonexistent(self):
        """Test finding nonexistent element by XPath."""
        soup = BeautifulSoup(self.sample_dom, 'html.parser')
        
        element = self.analyzer._find_by_xpath(soup, "/nonexistent/path")
        assert element is None
    
    def test_error_handling_invalid_dom(self):
        """Test error handling with invalid DOM."""
        invalid_dom = "<<invalid>>dom<<content>>"
        
        # Should not raise exceptions, return empty results
        parent_context = self.analyzer.extract_parent_context(invalid_dom, "id=test")
        assert parent_context == []
        
        sibling_context = self.analyzer.extract_sibling_context(invalid_dom, "id=test")
        assert sibling_context == []
        
        dom_path = self.analyzer.generate_dom_path(invalid_dom, "id=test")
        assert dom_path == ""
    
    def test_error_handling_malformed_locator(self):
        """Test error handling with malformed locator."""
        soup = BeautifulSoup(self.sample_dom, 'html.parser')
        
        # Should handle malformed locators gracefully
        element = self.analyzer._find_element_by_locator(soup, "invalid_locator_format")
        # Should default to CSS and try to find element
        assert element is None  # Won't find anything with this locator
    
    def test_context_extraction_depth_limit(self):
        """Test that context extraction respects depth limits."""
        # Create deeply nested DOM
        deep_dom = """
        <html>
            <body>
                <div class="level1">
                    <div class="level2">
                        <div class="level3">
                            <div class="level4">
                                <div class="level5">
                                    <button id="deep-button">Deep Button</button>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </body>
        </html>
        """
        
        parent_context = self.analyzer.extract_parent_context(deep_dom, "id=deep-button")
        
        # Should limit to 3 levels of parents
        assert len(parent_context) <= 3
        assert any("level5" in context for context in parent_context)
        assert any("level4" in context for context in parent_context)
        assert any("level3" in context for context in parent_context)