"""
Unit tests for src.backend.core.url_utils — extract_domain.

Purpose: extract_domain normalises URLs to domain-only format for pattern
         matching and logging.  A regression means domains are incorrect,
         breaking pattern lookups in the learning system.

Tests:
  - Standard URL → domain
  - Subdomain URL → domain
  - No protocol → 'unknown'
  - URL with port → domain
  - Empty string → 'unknown'
  - None → 'unknown'
  - Malformed URL → 'unknown'
"""

import pytest
from src.backend.core.url_utils import extract_domain


class TestExtractDomain:
    """Tests for extract_domain function."""

    def test_standard_url(self):
        """Standard HTTPS URL → domain without www."""
        assert extract_domain("https://www.demoqa.com/elements") == "demoqa.com"

    def test_subdomain_url(self):
        """Subdomain URL preserved (not stripped)."""
        result = extract_domain("http://the-internet.herokuapp.com/login")
        assert result == "the-internet.herokuapp.com"

    def test_no_protocol(self):
        """URL without http/https → urlparse can't extract netloc → 'unknown'."""
        result = extract_domain("just-a-string")
        assert result == "unknown"

    def test_url_with_port(self):
        """URL with port → includes port in netloc."""
        result = extract_domain("http://localhost:8080/api")
        assert "localhost" in result

    def test_empty_string(self):
        """Empty string → 'unknown'."""
        assert extract_domain("") == "unknown"

    def test_none_input(self):
        """None input → 'unknown' (doesn't crash)."""
        assert extract_domain(None) == "unknown"

    def test_malformed_url(self):
        """Clearly malformed URL → 'unknown'."""
        assert extract_domain("://broken") == "unknown"
