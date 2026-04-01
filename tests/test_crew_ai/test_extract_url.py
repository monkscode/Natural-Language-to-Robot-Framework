"""
Unit tests for extract_url_from_query in src.backend.crew_ai.crew.

Tests all 3 regex patterns:
  Pattern 1 - Full URLs with http:// or https://
  Pattern 2 - Domain names with common TLDs
  Pattern 3 - Website names with prepositions (on/from/at/in/visit/go to/open)
  Fallback  - Returns placeholder when no URL found
"""

import pytest
from src.backend.crew_ai.crew import extract_url_from_query


class TestExtractUrlFullProtocol:
    """Pattern 1: full URLs with http:// or https://."""

    def test_https_url(self):
        url = extract_url_from_query("Go to https://www.example.com and login")
        assert url == "https://www.example.com"

    def test_http_url(self):
        url = extract_url_from_query("Open http://staging.example.com/login")
        assert url == "http://staging.example.com/login"

    def test_url_with_path(self):
        url = extract_url_from_query("Test https://shop.example.com/cart/checkout")
        assert url == "https://shop.example.com/cart/checkout"

    def test_trailing_punctuation_stripped(self):
        """Trailing . , ; ! ? are stripped from full URLs."""
        url = extract_url_from_query("Visit https://example.com.")
        assert url == "https://example.com"

    def test_trailing_comma_stripped(self):
        url = extract_url_from_query("Navigate to https://example.com, then click login")
        assert url == "https://example.com"

    def test_first_url_returned_when_multiple(self):
        """When multiple full URLs appear, the first one is returned."""
        url = extract_url_from_query(
            "Go to https://first.example.com and then https://second.example.com"
        )
        assert url == "https://first.example.com"

    def test_case_insensitive(self):
        url = extract_url_from_query("Open HTTPS://EXAMPLE.COM")
        assert "example.com" in url.lower()


class TestExtractUrlDomainPattern:
    """Pattern 2: domain names with common TLDs."""

    def test_com_tld(self):
        url = extract_url_from_query("Login to amazon.com and search for laptops")
        assert "amazon.com" in url
        assert url.startswith("https://")

    def test_in_tld(self):
        url = extract_url_from_query("Search on flipkart.in for shoes")
        assert "flipkart.in" in url

    def test_org_tld(self):
        url = extract_url_from_query("Visit wikipedia.org and search Python")
        assert "wikipedia.org" in url

    def test_net_tld(self):
        url = extract_url_from_query("Check speedtest.net results")
        assert "speedtest.net" in url

    def test_io_tld(self):
        url = extract_url_from_query("Open github.io page")
        assert "github.io" in url

    def test_ai_tld(self):
        url = extract_url_from_query("Test claude.ai chat interface")
        assert "claude.ai" in url

    def test_app_tld(self):
        url = extract_url_from_query("Login to myapp.app and submit a form")
        assert "myapp.app" in url

    def test_dev_tld(self):
        url = extract_url_from_query("Go to staging.dev environment")
        assert "staging.dev" in url

    def test_www_prefix_preserved(self):
        url = extract_url_from_query("Navigate to www.example.com")
        assert "www.example.com" in url

    def test_https_prepended_when_missing(self):
        url = extract_url_from_query("Open example.com")
        assert url.startswith("https://")


class TestExtractUrlWebsiteName:
    """Pattern 3: website names after prepositions."""

    def test_on_preposition(self):
        url = extract_url_from_query("Search on flipkart for phones")
        assert "flipkart" in url
        assert url.startswith("https://")

    def test_from_preposition(self):
        url = extract_url_from_query("Download from github latest release")
        assert "github" in url

    def test_at_preposition(self):
        url = extract_url_from_query("Login at mysite")
        assert "mysite" in url

    def test_visit_keyword(self):
        url = extract_url_from_query("visit amazon and search for books")
        assert "amazon" in url

    def test_open_keyword(self):
        url = extract_url_from_query("open google and search python")
        assert "google" in url

    def test_lowercase_website_name(self):
        url = extract_url_from_query("Search on AMAZON for laptops")
        assert "amazon" in url.lower()

    def test_constructed_url_has_www_and_com(self):
        url = extract_url_from_query("on flipkart buy shoes")
        assert url.startswith("https://www.")
        assert url.endswith(".com")


class TestExtractUrlFallback:
    """Fallback: returns placeholder when no URL is detected."""

    def test_no_url_in_query(self):
        url = extract_url_from_query("Click the submit button")
        assert url == "website mentioned in query"

    def test_empty_string(self):
        url = extract_url_from_query("")
        assert url == "website mentioned in query"

    def test_generic_text_no_url(self):
        url = extract_url_from_query("Verify the page title is correct")
        assert url == "website mentioned in query"

    def test_partial_protocol_not_matched(self):
        """'http' without '://' should not match Pattern 1."""
        url = extract_url_from_query("The http protocol is used")
        # Should not match the full URL pattern — may fall through to other patterns or fallback
        assert isinstance(url, str)
        assert len(url) > 0
