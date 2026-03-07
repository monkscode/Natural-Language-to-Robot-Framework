"""
Shared URL utilities — used across the framework.

Provides generic URL parsing functions that are independent of any
specific subsystem (optimization, learning, etc.).
"""

from urllib.parse import urlparse


def extract_domain(url: str) -> str:
    """
    Extract domain from URL. Generic utility used across the framework.

    Strips the 'www.' prefix and normalises to lowercase.

    Examples:
        "https://www.demoqa.com/elements" → "demoqa.com"
        "http://the-internet.herokuapp.com" → "the-internet.herokuapp.com"
        "invalid" → "unknown"
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.netloc.lower().replace("www.", "")
        return hostname if hostname else "unknown"
    except Exception:
        return "unknown"
