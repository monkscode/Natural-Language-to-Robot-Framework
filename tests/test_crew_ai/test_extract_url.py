"""
Unit tests for extract_url_from_query in src.backend.crew_ai.crew.

Task 14 contract (E-series URL extraction fix):
  Pattern 1 - Full URLs with http:// or https:// (unchanged)
  Pattern 2 - Dotted hostnames validated by final-label TLD whitelist.
              The WHOLE dotted hostname is captured, so multi-label
              domains like mycompany.co.uk are never truncated to
              mycompany.co, and filename-shaped tokens like test.py
              are rejected rather than minted as domains.
  No match  - Returns None. The old Pattern 3 (preposition + word ->
              fabricated https://www.<word>.com) and the literal
              "website mentioned in query" placeholder are gone:
              a wrong domain poisons the learning store's key space,
              while None just degrades one run to generic hints.
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
    """Pattern 2: dotted hostnames with a whitelisted final label."""

    def test_com_tld(self):
        url = extract_url_from_query("Login to amazon.com and search for laptops")
        assert url == "https://amazon.com"

    def test_in_tld(self):
        url = extract_url_from_query("Search on flipkart.in for shoes")
        assert url == "https://flipkart.in"

    def test_org_tld(self):
        url = extract_url_from_query("Visit wikipedia.org and search Python")
        assert url == "https://wikipedia.org"

    def test_net_tld(self):
        url = extract_url_from_query("Check speedtest.net results")
        assert url == "https://speedtest.net"

    def test_io_tld(self):
        url = extract_url_from_query("Open github.io page")
        assert url == "https://github.io"

    def test_ai_tld(self):
        url = extract_url_from_query("Test claude.ai chat interface")
        assert url == "https://claude.ai"

    def test_app_tld(self):
        url = extract_url_from_query("Login to myapp.app and submit a form")
        assert url == "https://myapp.app"

    def test_dev_tld(self):
        url = extract_url_from_query("Go to staging.dev environment")
        assert url == "https://staging.dev"

    def test_www_prefix_preserved(self):
        url = extract_url_from_query("Navigate to www.example.com")
        assert url == "https://www.example.com"

    def test_https_prepended_when_missing(self):
        url = extract_url_from_query("Open example.com")
        assert url == "https://example.com"

    def test_multi_label_domain_not_truncated(self):
        """mycompany.co.uk must key as mycompany.co.uk — never mycompany.co.

        The old regex matched the first whitelisted TLD it saw, so .co.uk
        domains were truncated to a domain that isn't the customer's.
        """
        url = extract_url_from_query("verify checkout on mycompany.co.uk")
        assert url == "https://mycompany.co.uk"

    def test_multi_label_with_subdomain(self):
        url = extract_url_from_query("login at portal.mycompany.co.uk and check orders")
        assert url == "https://portal.mycompany.co.uk"

    def test_deep_subdomain_single_tld(self):
        url = extract_url_from_query("open billing.mycompany.org and login")
        assert url == "https://billing.mycompany.org"

    def test_filename_not_minted_as_domain(self):
        """test.py must not become a domain even though .py is a real ccTLD."""
        url = extract_url_from_query("run test.py against the site")
        assert url is None

    def test_unknown_tld_rejected_whole_not_truncated(self):
        """A hostname ending in a non-whitelisted label is rejected entirely,
        not truncated back to an earlier whitelisted label."""
        url = extract_url_from_query("check mycompany.co.zz for errors")
        assert url is None

    def test_rejected_candidate_does_not_block_later_valid_one(self):
        """Scanning continues past an invalid candidate to a valid one."""
        url = extract_url_from_query("run test.py then open example.com")
        assert url == "https://example.com"

    def test_version_number_not_matched(self):
        url = extract_url_from_query("verify robot framework 7.1 is installed")
        assert url is None

    def test_uppercase_domain_matched(self):
        url = extract_url_from_query("Open EXAMPLE.COM")
        assert url is not None
        assert "example.com" in url.lower()


class TestExtractUrlNoFabrication:
    """The old Pattern 3 fabricated https://www.<word>.com from prepositions.
    All of these historically produced poisoned domains; they must return None."""

    def test_stopword_to_not_fabricated(self):
        """'log in to the system' used to produce https://www.to.com."""
        assert extract_url_from_query("log in to the system with admin") is None

    def test_settings_not_fabricated(self):
        """'open settings page' used to produce https://www.settings.com."""
        assert extract_url_from_query("open settings page and change password") is None

    def test_admin_not_fabricated(self):
        """'login at admin panel' used to produce https://www.admin.com."""
        assert extract_url_from_query("login at admin panel") is None

    def test_bare_site_name_not_guessed(self):
        """'on flipkart' must not guess flipkart.com — the user may mean
        flipkart.in, a staging host, or something else entirely."""
        assert extract_url_from_query("Search on flipkart for phones") is None

    def test_visit_word_not_guessed(self):
        assert extract_url_from_query("visit amazon and search for books") is None


class TestExtractUrlNoMatch:
    """No URL in the query -> None (placeholder string is gone)."""

    def test_no_url_in_query(self):
        assert extract_url_from_query("Click the submit button") is None

    def test_empty_string(self):
        assert extract_url_from_query("") is None

    def test_generic_text_no_url(self):
        assert extract_url_from_query("Verify the page title is correct") is None

    def test_partial_protocol_not_matched(self):
        """'http' without '://' should not match Pattern 1."""
        assert extract_url_from_query("The http protocol is used") is None
