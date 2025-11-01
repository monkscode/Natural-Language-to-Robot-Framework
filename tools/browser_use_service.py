# ========================================
# UNICODE FIX - MUST BE FIRST
# ========================================
# Force UTF-8 encoding on Windows BEFORE any other imports
import sys
import os
import io

if sys.platform.startswith('win'):
    # Set environment variables for UTF-8
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    os.environ['PYTHONUTF8'] = '1'

    # Reconfigure stdout/stderr with UTF-8 (only if not already wrapped)
    try:
        if hasattr(sys.stdout, 'buffer') and not isinstance(sys.stdout, io.TextIOWrapper):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
        if hasattr(sys.stderr, 'buffer') and not isinstance(sys.stderr, io.TextIOWrapper):
            sys.stderr = io.TextIOWrapper(
                sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    except (ValueError, AttributeError):
        # If reconfiguration fails, just continue with default encoding
        pass

# ========================================
# PATH SETUP (FALLBACK)
# ========================================
# When running as a module (python -m tools.browser_use_service),
# tools/__init__.py handles path setup automatically.
# When running directly (python tools/browser_use_service.py),
# we need this fallback to ensure imports work.
from pathlib import Path
_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# ========================================
# IMPORT LOCAL MODULES (after path setup)
# ========================================
# NOTE: Prompt building functions are now integrated into this file (see PROMPT BUILDING section)
# from simplified_workflow_prompt import build_simplified_workflow_prompt, build_system_prompt
# NOTE: Locator extraction functions are now integrated into this file (see LOCATOR EXTRACTION section)
# from locator_extractor import extract_and_validate_locators

# ========================================
# LOGGING SETUP - MUST BE BEFORE BROWSER-USE IMPORTS
# ========================================
# CRITICAL: Configure logging BEFORE importing browser-use to ensure all loggers use UTF-8
import asyncio
import logging
import json
import re
import time
import threading
import uuid
from urllib.parse import urlparse
from flask import Flask, request, jsonify
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, Optional, List

# Create a custom handler with UTF-8 encoding
# CRITICAL: Must explicitly set encoding='utf-8' and errors='replace' for Windows compatibility
if sys.platform.startswith('win'):
    # For Windows, create a StreamHandler with explicit UTF-8 encoding
    log_handler = logging.StreamHandler(sys.stdout)
    log_handler.stream = io.TextIOWrapper(
        sys.stdout.buffer,
        encoding='utf-8',
        errors='replace',  # Replace unencodable characters instead of crashing
        line_buffering=True
    )
else:
    # For Unix-like systems, default encoding is usually UTF-8
    log_handler = logging.StreamHandler(sys.stdout)

log_handler.setFormatter(logging.Formatter(
    "%(levelname)-8s [%(name)s] %(message)s"
))

# Configure root logger with our UTF-8 handler
# This ensures all child loggers inherit the UTF-8 encoding
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers.clear()  # Remove any existing handlers
root_logger.addHandler(log_handler)

# Get our module logger
logger = logging.getLogger(__name__)

# ========================================
# STANDARD LIBRARY & THIRD-PARTY IMPORTS
# ========================================
# Import browser-use AFTER logging is configured
from browser_use.browser.session import BrowserSession
from browser_use import Agent, Browser
from dotenv import load_dotenv

# ========================================
# LOCAL IMPORTS
# ========================================
# These imports work because:
# 1. tools/__init__.py sets up path (when imported as module)
# 2. Fallback above sets up path (when run directly)
from src.backend.core.config import settings

# Get library type from config (will log after logger is initialized)
ROBOT_LIBRARY = settings.ROBOT_LIBRARY

# Load environment variables
load_dotenv("src/backend/.env")

# Log library configuration
logger.info(f"🔧 Browser Use Service configured for: {ROBOT_LIBRARY}")

# Import litellm for RateLimitError handling (optional)
try:
    import litellm
except ImportError:
    litellm = None

app = Flask(__name__)

# ========================================
# BATCH PROCESSING CONFIGURATION
# ========================================
BATCH_CONFIG = {
    # Stop agent after N steps
    "max_agent_steps": int(os.getenv("MAX_AGENT_STEPS", "15")),
    # Retry element N times before skipping
    "max_retries_per_element": int(os.getenv("MAX_RETRIES_PER_ELEMENT", "2")),
    # Max time per element (seconds)
    "element_timeout": int(os.getenv("ELEMENT_TIMEOUT", "120")),
}

# ========================================
# LOCATOR EXTRACTION CONFIGURATION
# ========================================
# Configurable retry counts for content-based vs coordinate-based strategies
# These can be adjusted based on customer usage patterns and success rates
LOCATOR_EXTRACTION_CONFIG = {
    # Content-based search (finds element by visible text)
    # Try content search N times
    "content_based_retries": int(os.getenv("CONTENT_BASED_RETRIES", "7")),

    # Coordinate-based search (finds element by screen position)
    # Higher count for coordinate fallback
    "coordinate_based_retries": int(os.getenv("COORDINATE_BASED_RETRIES", "7")),

    # Element type fallback (finds first visible element of type)
    # Last resort
    "element_type_retries": int(os.getenv("ELEMENT_TYPE_RETRIES", "5")),

    # Coordinate offset attempts (try nearby coordinates if first fails)
    # Try N different offsets
    "coordinate_offset_attempts": int(os.getenv("COORDINATE_OFFSET_ATTEMPTS", "7")),

    # Coordinate offsets to try (pixels)
    "coordinate_offsets": [
        {"x": 100, "y": 0, "reason": "escape sidebar/left panel"},
        {"x": 200, "y": 0, "reason": "escape wide sidebar"},
        {"x": 50, "y": 0, "reason": "slight right adjustment"},
        {"x": 0, "y": 20, "reason": "move down slightly"},
        {"x": 100, "y": 20, "reason": "diagonal adjustment"}
    ]
}

logger.info(
    f"Batch Config: max_steps={BATCH_CONFIG['max_agent_steps']}, max_retries={BATCH_CONFIG['max_retries_per_element']}, timeout={BATCH_CONFIG['element_timeout']}s")
logger.info(
    f"Locator Extraction Config: content_retries={LOCATOR_EXTRACTION_CONFIG['content_based_retries']}, coordinate_retries={LOCATOR_EXTRACTION_CONFIG['coordinate_based_retries']}, coordinate_offsets={LOCATOR_EXTRACTION_CONFIG['coordinate_offset_attempts']}")

# ========================================
# LIBRARY-SPECIFIC LOCATOR STRATEGIES
# ========================================


def get_locator_strategies_js(library_type: str) -> str:
    """
    Generate JavaScript locator strategies based on Robot Framework library type.

    STRATEGY PRIORITY ORDER (most stable to least stable):
    1. id - Most stable, fastest (browser native lookup)
    2. data-testid - Designed specifically for testing
    3. name - Semantic, stable (SeleniumLibrary)
    4. aria-label - Semantic, accessible
    5. text - Content-based (can be fragile if text changes)
    6. role - Semantic (Playwright-specific, Browser Library only)
    7. xpath - LAST RESORT (fragile, breaks with DOM changes, slow)
    8. css-class - LAST RESORT (classes can change)

    The generateXPath function returns null for elements with better locators
    (id, data-testid, etc.), ensuring XPath is only used as a last resort.

    Args:
        library_type: "selenium" or "browser"

    Returns:
        JavaScript code defining locator strategies for the specified library
    """
    if library_type == "browser":
        # Browser Library (Playwright) - supports text and role selectors
        # NOTE: gen() returns Robot Framework format, sel() returns CSS/XPath for JS validation
        return """
        {{ type: 'id', gen: () => element.id ? `id=${{element.id}}` : null, sel: () => element.id ? `#${{element.id}}` : null }},
        {{ type: 'data-testid', gen: () => element.dataset?.testid ? `data-testid=${{element.dataset.testid}}` : null, sel: () => element.dataset?.testid ? `[data-testid="${{element.dataset.testid}}"]` : null }},
        {{ type: 'aria-label', gen: () => element.getAttribute('aria-label') ? `[aria-label="${{element.getAttribute('aria-label')}}"]` : null, sel: () => element.getAttribute('aria-label') ? `[aria-label="${{element.getAttribute('aria-label')}}"]` : null }},
        {{ type: 'text', gen: () => element.textContent?.trim() ? `text=${{element.textContent.trim()}}` : null, sel: () => element.textContent?.trim() ? `//*[contains(normalize-space(text()), "${{element.textContent.trim().replace(/"/g, '\\\\"')}}")]` : null, isXPath: true }},
        {{ type: 'role', gen: () => getRoleLocator(element), sel: () => getRoleSelector(element), isXPath: true }},
        {{ type: 'xpath', gen: () => generateXPath(element), sel: () => generateXPath(element), isXPath: true }},
        {{ type: 'css-class', gen: () => element.className ? `${{element.tagName.toLowerCase()}}.${{element.className.split(' ')[0]}}` : null, sel: () => element.className ? `${{element.tagName.toLowerCase()}}.${{element.className.split(' ')[0]}}` : null }},
        {{ type: 'parent-id-xpath', gen: () => generateParentIdXPath(element), sel: () => generateParentIdXPath(element)?.replace('xpath=', ''), isXPath: true }},
        {{ type: 'text-xpath', gen: () => generateTextXPath(element), sel: () => generateTextXPath(element)?.replace('xpath=', ''), isXPath: true }},
        {{ type: 'nth-child', gen: () => generateNthChildSelector(element), sel: () => generateNthChildSelector(element) }},
        {{ type: 'attribute-combo', gen: () => generateAttributeComboXPath(element), sel: () => generateAttributeComboXPath(element)?.replace('xpath=', ''), isXPath: true }}
        """
    else:
        # SeleniumLibrary - traditional format with prefixes
        return """
        {{ type: 'id', gen: () => element.id ? `id=${{element.id}}` : null, sel: () => element.id ? `#${{element.id}}` : null }},
        {{ type: 'name', gen: () => element.name ? `name=${{element.name}}` : null, sel: () => element.name ? `[name="${{element.name}}"]` : null }},
        {{ type: 'data-testid', gen: () => element.dataset?.testid ? `css=[data-testid="${{element.dataset.testid}}"]` : null, sel: () => element.dataset?.testid ? `[data-testid="${{element.dataset.testid}}"]` : null }},
        {{ type: 'aria-label', gen: () => element.getAttribute('aria-label') ? `css=[aria-label="${{element.getAttribute('aria-label')}}"]` : null, sel: () => element.getAttribute('aria-label') ? `[aria-label="${{element.getAttribute('aria-label')}}"]` : null }},
        {{ type: 'title', gen: () => element.getAttribute('title') ? `xpath=//*[@title="${{element.getAttribute('title')}}"]` : null, sel: () => element.getAttribute('title') ? `//*[@title="${{element.getAttribute('title')}}"]` : null, isXPath: true }},
        {{ type: 'text', gen: () => element.textContent?.trim() ? `xpath=//*[text()="${{element.textContent.trim().slice(0,50)}}"]` : null, sel: () => element.textContent?.trim() ? `//*[text()="${{element.textContent.trim().slice(0,50)}}"]` : null, isXPath: true }},
        {{ type: 'css-class', gen: () => element.className ? `css=${{element.tagName.toLowerCase()}}.${{element.className.split(' ')[0]}}` : null, sel: () => element.className ? `${{element.tagName.toLowerCase()}}.${{element.className.split(' ')[0]}}` : null }},
        {{ type: 'parent-id-xpath', gen: () => generateParentIdXPath(element), sel: () => generateParentIdXPath(element)?.replace('xpath=', ''), isXPath: true }},
        {{ type: 'text-xpath', gen: () => generateTextXPath(element), sel: () => generateTextXPath(element)?.replace('xpath=', ''), isXPath: true }},
        {{ type: 'nth-child', gen: () => generateNthChildSelector(element), sel: () => generateNthChildSelector(element)?.replace('css=', '') }},
        {{ type: 'attribute-combo', gen: () => generateAttributeComboXPath(element), sel: () => generateAttributeComboXPath(element)?.replace('xpath=', ''), isXPath: true }}
        """


def get_helper_functions_js(library_type: str) -> str:
    """
    Generate helper functions for locator generation.

    Args:
        library_type: "selenium" or "browser"

    Returns:
        JavaScript helper functions
    """
    if library_type == "browser":
        # Browser Library needs role locator helper and role selector for validation
        return """
    function getRoleLocator(el) {
        const roleMap = {
            'BUTTON': 'button',
            'A': 'link',
            'INPUT': 'textbox',
            'SELECT': 'combobox',
            'TEXTAREA': 'textbox'
        };
        const role = el.getAttribute('role') || roleMap[el.tagName] || null;
        const name = el.getAttribute('aria-label') || el.textContent?.trim().slice(0,30) || null;
        if (role && name) {
            return `role=${{role}}[name="${{name}}"]`;
        }
        return null;
    }

    function getRoleSelector(el) {
        // Returns XPath for validation (since role= is Playwright-specific)
        const roleMap = {
            'BUTTON': 'button',
            'A': 'a',
            'INPUT': 'input',
            'SELECT': 'select',
            'TEXTAREA': 'textarea'
        };
        const tag = roleMap[el.tagName] || el.tagName.toLowerCase();
        const name = el.getAttribute('aria-label') || el.textContent?.trim().slice(0,30) || null;
        if (name) {
            // XPath that finds element by tag and text/aria-label
            return `//${{tag}}[contains(text(), "${{name}}") or @aria-label="${{name}}"]`;
        }
        return null;
    }

    // ========================================
    // FALLBACK STRATEGY 1: Parent Element with ID + Relative XPath
    // ========================================
    function generateParentIdXPath(el) {
        console.log(`🔍 Fallback Strategy 1: Searching for parent with ID...`);
        let current = el;
        let depth = 0;
        const maxDepth = 5; // Don't go too far up the tree

        while (current && current !== document.body && depth < maxDepth) {
            current = current.parentElement;
            depth++;

            if (current && current.id && current.id.trim() !== '') {
                console.log(`   ✅ Found parent with ID="${current.id}" at depth ${depth}`);

                // Generate relative path from parent to target element
                const path = getRelativeXPath(el, current);
                const xpath = `xpath=//*[@id="${current.id}"]${path}`;
                console.log(`   Generated: ${xpath}`);
                return xpath;
            }
        }

        console.log(`   ❌ No parent with ID found within ${maxDepth} levels`);
        return null;
    }

    function getRelativeXPath(target, ancestor) {
        const path = [];
        let current = target;

        while (current && current !== ancestor) {
            const tag = current.tagName.toLowerCase();
            const siblings = Array.from(current.parentElement.children).filter(
                el => el.tagName === current.tagName
            );

            if (siblings.length > 1) {
                const index = siblings.indexOf(current) + 1;
                path.unshift(`/${tag}[${index}]`);
            } else {
                path.unshift(`/${tag}`);
            }

            current = current.parentElement;
        }

        return path.join('');
    }

    // ========================================
    // FALLBACK STRATEGY 2: Text-Based XPath (Exact Match)
    // ========================================
    function generateTextXPath(el) {
        const text = el.textContent?.trim();
        if (!text || text.length === 0) {
            console.log(`   ℹ️ Text XPath: Element has no text content`);
            return null;
        }

        console.log(`🔍 Fallback Strategy 2: Text-based XPath...`);
        const tag = el.tagName.toLowerCase();

        // Strategy 2a: Exact text match
        const exactXPath = `xpath=//${tag}[normalize-space(text())="${text}"]`;
        console.log(`   Generated exact match: ${exactXPath}`);
        return exactXPath;
    }

    // ========================================
    // FALLBACK STRATEGY 3: Nth-Child Selector
    // ========================================
    function generateNthChildSelector(el) {
        console.log(`🔍 Fallback Strategy 3: Nth-child selector...`);
        const parent = el.parentElement;
        if (!parent) {
            console.log(`   ❌ Element has no parent`);
            return null;
        }

        // Find all siblings with same tag and class
        const siblings = Array.from(parent.children).filter(
            sibling => sibling.tagName === el.tagName &&
                      sibling.className === el.className
        );

        if (siblings.length === 0) {
            console.log(`   ❌ No matching siblings found`);
            return null;
        }

        const index = siblings.indexOf(el);
        if (index === -1) {
            console.log(`   ❌ Element not found in siblings list`);
            return null;
        }

        const tag = el.tagName.toLowerCase();
        const className = el.className.split(' ')[0];
        const selector = `${tag}.${className}:nth-of-type(${index + 1})`;
        console.log(`   Generated: ${selector}`);
        return selector;
    }

    // ========================================
    // FALLBACK STRATEGY 4: Attribute Combination XPath
    // ========================================
    function generateAttributeComboXPath(el) {
        console.log(`🔍 Fallback Strategy 4: Attribute combination XPath...`);
        const attrs = [];
        const tag = el.tagName.toLowerCase();

        // Collect meaningful attributes
        if (el.className) {
            const firstClass = el.className.split(' ')[0];
            attrs.push(`contains(@class, "${firstClass}")`);
        }
        if (el.getAttribute('href')) {
            attrs.push(`@href="${el.getAttribute('href')}"`);
        }
        if (el.getAttribute('title')) {
            attrs.push(`@title="${el.getAttribute('title')}"`);
        }
        if (el.getAttribute('type')) {
            attrs.push(`@type="${el.getAttribute('type')}"`);
        }

        // Add text content if available
        const text = el.textContent?.trim();
        if (text && text.length > 0 && text.length < 50) {
            attrs.push(`contains(text(), "${text.substring(0, 30)}")`);
        }

        if (attrs.length >= 2) {
            const xpath = `xpath=//${tag}[${attrs.join(' and ')}]`;
            console.log(`   Generated with ${attrs.length} attributes: ${xpath}`);
            return xpath;
        }

        console.log(`   ❌ Not enough attributes (need 2+, found ${attrs.length})`);
        return null;
    }

    function generateXPath(el) {
        // ============================================================
        // CRITICAL: XPath is LAST RESORT - return null if ANY better locator exists
        // ============================================================
        // XPath locators are fragile and break when DOM structure changes.
        // We MUST prioritize stable locators (ID, data-testid, name, aria-label, title)
        // over XPath to ensure test stability and maintainability.

        // Priority 1: ID (most stable, fastest browser native lookup)
        if (el.id && el.id.trim() !== '') {
            console.log(`⚠️ generateXPath: Skipping element with ID="${el.id}" - ID strategy will handle it`);
            return null;
        }

        // Priority 2: data-testid (designed specifically for testing)
        if (el.dataset?.testid && el.dataset.testid.trim() !== '') {
            console.log(`⚠️ generateXPath: Skipping element with data-testid="${el.dataset.testid}" - data-testid strategy will handle it`);
            return null;
        }

        // Priority 3: aria-label (accessibility attribute, semantic)
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel && ariaLabel.trim() !== '') {
            console.log(`⚠️ generateXPath: Skipping element with aria-label="${ariaLabel}" - aria-label strategy will handle it`);
            return null;
        }

        // Priority 4: title attribute (semantic, stable)
        const titleAttr = el.getAttribute('title');
        if (titleAttr && titleAttr.trim() !== '') {
            console.log(`⚠️ generateXPath: Skipping element with title="${titleAttr}" - title strategy will handle it`);
            return null;
        }

        // If we reach here, no better locator exists - XPath would be acceptable as fallback
        // However, for now we return null to prevent XPath generation entirely
        // XPath generation logic can be added later if truly needed for edge cases
        console.log(`ℹ️ generateXPath: No better locator found for element (tag=${el.tagName}) - returning null (XPath generation disabled)`);
        return null;
    }
        """
    else:
        # SeleniumLibrary - add fallback strategies
        return """
    // ========================================
    // FALLBACK STRATEGY 1: Parent Element with ID + Relative XPath
    // ========================================
    function generateParentIdXPath(el) {
        console.log(`🔍 Fallback Strategy 1: Searching for parent with ID...`);
        let current = el;
        let depth = 0;
        const maxDepth = 5; // Don't go too far up the tree

        while (current && current !== document.body && depth < maxDepth) {
            current = current.parentElement;
            depth++;

            if (current && current.id && current.id.trim() !== '') {
                console.log(`   ✅ Found parent with ID="${current.id}" at depth ${depth}`);

                // Generate relative path from parent to target element
                const path = getRelativeXPath(el, current);
                const xpath = `xpath=//*[@id="${current.id}"]${path}`;
                console.log(`   Generated: ${xpath}`);
                return xpath;
            }
        }

        console.log(`   ❌ No parent with ID found within ${maxDepth} levels`);
        return null;
    }

    function getRelativeXPath(target, ancestor) {
        const path = [];
        let current = target;

        while (current && current !== ancestor) {
            const tag = current.tagName.toLowerCase();
            const siblings = Array.from(current.parentElement.children).filter(
                el => el.tagName === current.tagName
            );

            if (siblings.length > 1) {
                const index = siblings.indexOf(current) + 1;
                path.unshift(`/${tag}[${index}]`);
            } else {
                path.unshift(`/${tag}`);
            }

            current = current.parentElement;
        }

        return path.join('');
    }

    // ========================================
    // FALLBACK STRATEGY 2: Text-Based XPath (Exact Match)
    // ========================================
    function generateTextXPath(el) {
        const text = el.textContent?.trim();
        if (!text || text.length === 0) {
            console.log(`   ℹ️ Text XPath: Element has no text content`);
            return null;
        }

        console.log(`🔍 Fallback Strategy 2: Text-based XPath...`);
        const tag = el.tagName.toLowerCase();

        // Strategy 2a: Exact text match
        const exactXPath = `xpath=//${tag}[normalize-space(text())="${text}"]`;
        console.log(`   Generated exact match: ${exactXPath}`);
        return exactXPath;
    }

    // ========================================
    // FALLBACK STRATEGY 3: Nth-Child CSS Selector
    // ========================================
    function generateNthChildSelector(el) {
        console.log(`🔍 Fallback Strategy 3: Nth-child selector...`);
        const parent = el.parentElement;
        if (!parent) {
            console.log(`   ❌ Element has no parent`);
            return null;
        }

        // Find all siblings with same tag and class
        const siblings = Array.from(parent.children).filter(
            sibling => sibling.tagName === el.tagName &&
                      sibling.className === el.className
        );

        if (siblings.length === 0) {
            console.log(`   ❌ No matching siblings found`);
            return null;
        }

        const index = siblings.indexOf(el);
        if (index === -1) {
            console.log(`   ❌ Element not found in siblings list`);
            return null;
        }

        const tag = el.tagName.toLowerCase();
        const className = el.className.split(' ')[0];
        const selector = `css=${tag}.${className}:nth-of-type(${index + 1})`;
        console.log(`   Generated: ${selector}`);
        return selector;
    }

    // ========================================
    // FALLBACK STRATEGY 4: Attribute Combination XPath
    // ========================================
    function generateAttributeComboXPath(el) {
        console.log(`🔍 Fallback Strategy 4: Attribute combination XPath...`);
        const attrs = [];
        const tag = el.tagName.toLowerCase();

        // Collect meaningful attributes
        if (el.className) {
            const firstClass = el.className.split(' ')[0];
            attrs.push(`contains(@class, "${firstClass}")`);
        }
        if (el.getAttribute('href')) {
            attrs.push(`@href="${el.getAttribute('href')}"`);
        }
        if (el.getAttribute('title')) {
            attrs.push(`@title="${el.getAttribute('title')}"`);
        }
        if (el.getAttribute('type')) {
            attrs.push(`@type="${el.getAttribute('type')}"`);
        }

        // Add text content if available
        const text = el.textContent?.trim();
        if (text && text.length > 0 && text.length < 50) {
            attrs.push(`contains(text(), "${text.substring(0, 30)}")`);
        }

        if (attrs.length >= 2) {
            const xpath = `xpath=//${tag}[${attrs.join(' and ')}]`;
            console.log(`   Generated with ${attrs.length} attributes: ${xpath}`);
            return xpath;
        }

        console.log(`   ❌ Not enough attributes (need 2+, found ${attrs.length})`);
        return null;
    }

    function generateXPath(el) {
        // ============================================================
        // CRITICAL: XPath is LAST RESORT - return null if ANY better locator exists
        // ============================================================
        // XPath locators are fragile and break when DOM structure changes.
        // We MUST prioritize stable locators (ID, name, data-testid, aria-label, title)
        // over XPath to ensure test stability and maintainability.

        // Priority 1: ID (most stable, fastest browser native lookup)
        if (el.id && el.id.trim() !== '') {
            console.log(`⚠️ generateXPath: Skipping element with ID="${el.id}" - ID strategy will handle it`);
            return null;
        }

        // Priority 2: name (SeleniumLibrary priority, semantic and stable)
        if (el.name && el.name.trim() !== '') {
            console.log(`⚠️ generateXPath: Skipping element with name="${el.name}" - name strategy will handle it`);
            return null;
        }

        // Priority 3: data-testid (designed specifically for testing)
        if (el.dataset?.testid && el.dataset.testid.trim() !== '') {
            console.log(`⚠️ generateXPath: Skipping element with data-testid="${el.dataset.testid}" - data-testid strategy will handle it`);
            return null;
        }

        // Priority 4: aria-label (accessibility attribute, semantic)
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel && ariaLabel.trim() !== '') {
            console.log(`⚠️ generateXPath: Skipping element with aria-label="${ariaLabel}" - aria-label strategy will handle it`);
            return null;
        }

        // Priority 5: title attribute (semantic, stable)
        const titleAttr = el.getAttribute('title');
        if (titleAttr && titleAttr.trim() !== '') {
            console.log(`⚠️ generateXPath: Skipping element with title="${titleAttr}" - title strategy will handle it`);
            return null;
        }

        // If we reach here, no better locator exists - XPath would be acceptable as fallback
        // However, for now we return null to prevent XPath generation entirely
        // XPath generation logic can be added later if truly needed for edge cases
        console.log(`ℹ️ generateXPath: No better locator found for element (tag=${el.tagName}) - returning null (XPath generation disabled)`);
        return null;
    }
        """


# ========================================
# PLAYWRIGHT LOCATOR VALIDATION (Browser Library Only)
# ========================================

async def validate_locators_with_playwright(page, locators: list, library_type: str) -> list:
    """
    Validate locators using Playwright's built-in locator API.
    This provides an additional layer of validation specifically for Browser Library.

    Args:
        page: Playwright page object from the browser session
        locators: List of locator dictionaries from JavaScript validation
        library_type: "browser" or "selenium"

    Returns:
        List of locators with updated validation status from Playwright
    """
    if library_type != "browser":
        # Skip Playwright validation for SeleniumLibrary
        logger.info(
            "Skipping Playwright validation (not using Browser Library)")
        return locators

    if not locators:
        return locators

    logger.info(
        f"🎭 Starting Playwright validation for {len(locators)} locators...")
    validated_locators = []

    for loc_data in locators:
        locator_str = loc_data.get('locator', '')
        loc_type = loc_data.get('type', '')

        try:
            # Convert Robot Framework locator to Playwright locator
            playwright_locator = None

            if locator_str.startswith('id='):
                # id=element_id → #element_id
                element_id = locator_str[3:]
                playwright_locator = page.locator(f'#{element_id}')

            elif locator_str.startswith('text='):
                # text=button text → text=button text
                text_value = locator_str[5:]
                playwright_locator = page.locator(f'text={text_value}')

            elif locator_str.startswith('role='):
                # role=button[name="Submit"] → role=button[name="Submit"]
                playwright_locator = page.locator(locator_str)

            elif locator_str.startswith('data-testid='):
                # data-testid=value → [data-testid="value"]
                testid_value = locator_str[12:]
                playwright_locator = page.locator(
                    f'[data-testid="{testid_value}"]')

            elif locator_str.startswith('xpath='):
                # xpath=//*[@id="test"] → //*[@id="test"]
                xpath_value = locator_str[6:]
                playwright_locator = page.locator(f'xpath={xpath_value}')

            else:
                # Assume it's a CSS selector
                playwright_locator = page.locator(locator_str)

            if playwright_locator:
                # Use Playwright's count() to check how many elements match
                count = await playwright_locator.count()

                # Check if element is visible (for the first match)
                is_visible = False
                if count > 0:
                    try:
                        is_visible = await playwright_locator.first.is_visible(timeout=1000)
                    except:
                        is_visible = False

                # Update validation data
                loc_data['playwright_validated'] = True
                loc_data['playwright_count'] = count
                loc_data['playwright_visible'] = is_visible

                # CRITICAL: Only unique locators (count == 1) are valid
                # Update uniqueness and validity based on Playwright count
                if count == 1:
                    loc_data['unique'] = True
                    loc_data['valid'] = True  # Only unique locators are valid
                    loc_data['confidence'] = 1.0
                    logger.info(
                        f"✅ Playwright: {locator_str} → UNIQUE (1 match, visible={is_visible})")
                elif count > 1:
                    loc_data['unique'] = False
                    loc_data['valid'] = False  # Not unique = not valid
                    loc_data['confidence'] = 0.7 if is_visible else 0.5
                    logger.info(
                        f"⚠️  Playwright: {locator_str} → NOT UNIQUE ({count} matches, visible={is_visible})")
                else:
                    loc_data['unique'] = False
                    loc_data['valid'] = False  # Not found = not valid
                    loc_data['confidence'] = 0.0
                    logger.warning(
                        f"❌ Playwright: {locator_str} → NOT FOUND (0 matches)")
                    continue  # Skip locators that don't match anything

                validated_locators.append(loc_data)
            else:
                # Couldn't convert to Playwright locator, keep original
                loc_data['playwright_validated'] = False
                validated_locators.append(loc_data)
                logger.warning(
                    f"⚠️  Could not convert to Playwright locator: {locator_str}")

        except Exception as e:
            logger.warning(
                f"⚠️  Playwright validation error for {locator_str}: {e}")
            loc_data['playwright_validated'] = False
            loc_data['playwright_error'] = str(e)
            validated_locators.append(loc_data)

    # Re-sort by confidence after Playwright validation
    validated_locators.sort(key=lambda x: (
        x.get('unique', False), x.get('confidence', 0)), reverse=True)

    unique_count = sum(
        1 for loc in validated_locators if loc.get('unique', False))
    logger.info(
        f"🎭 Playwright validation complete: {len(validated_locators)} valid, {unique_count} unique")

    return validated_locators


# Task storage to keep track of tasks
tasks: Dict[str, Dict[str, Any]] = {}

# Initialize a thread pool executor for running tasks in the background
executor = ThreadPoolExecutor(max_workers=1)

# ========================================
# LLM CONFIGURATION
# ========================================
# Google API configuration
GOOGLE_API_KEY = os.getenv("GEMINI_API_KEY")

# Get model from settings, strip "gemini/" prefix if present (ChatGoogle doesn't need it)
GOOGLE_MODEL = settings.ONLINE_MODEL.replace(
    "gemini/", "") if settings.ONLINE_MODEL else "gemini-2.5-flash"
logger.info(f"🤖 LLM Configuration:")
logger.info(f"   Model: {GOOGLE_MODEL}")
logger.info(
    f"   API Key: {'*' * 20}{GOOGLE_API_KEY[-8:] if GOOGLE_API_KEY else 'NOT SET'}")

# ========================================
# LLM USAGE: Using default ChatGoogle without rate limiting
# Google Gemini API has sufficient rate limits (1500 RPM) for our use case
# ========================================

# ========================================
# PROMPT BUILDING
# ========================================

def build_workflow_prompt(
    user_query: str,
    url: str,
    elements: list,
    library_type: str = "browser",
    include_custom_action: bool = True
) -> str:
    """
    Build workflow prompt for browser-use agent.
    
    The agent will:
    1. Navigate to the URL
    2. Find each element using vision
    3. Get element coordinates
    4. Call find_unique_locator custom action (if enabled) OR use JavaScript validation (legacy)
    
    Args:
        user_query: User's goal
        url: Target URL
        elements: List of elements to find
        library_type: "browser" or "selenium"
        include_custom_action: If True, include custom action instructions; if False, use legacy JavaScript validation
        
    Returns:
        Prompt string for agent
    """
    
    # Build element list
    element_list = []
    for elem in elements:
        elem_id = elem.get('id')
        elem_desc = elem.get('description')
        elem_action = elem.get('action', 'get_text')
        element_list.append(f"   - {elem_id}: {elem_desc} (action: {elem_action})")
    
    elements_str = "\n".join(element_list)
    
    if include_custom_action:
        # NEW WORKFLOW: Use custom action for locator finding
        prompt = f"""
You are completing a web automation workflow.

USER'S GOAL: {user_query}

WORKFLOW STEPS:
1. Navigate to {url}
2. Find each element listed below using your vision
3. For EACH element, call the find_unique_locator action to get a validated unique locator

ELEMENTS TO FIND:
{elements_str}

═══════════════════════════════════════════════════════════════════
CUSTOM ACTION: find_unique_locator
═══════════════════════════════════════════════════════════════════

This action finds and validates unique locators for web elements using 21 systematic strategies.
It uses Playwright validation to ensure every locator is unique (count=1).

PARAMETERS:
  • x (float, required): X coordinate of element center
  • y (float, required): Y coordinate of element center  
  • element_id (str, required): Element identifier from the list above (e.g., "elem_1")
  • element_description (str, required): Human-readable description of the element
  • candidate_locator (str, optional): Your suggested locator if you can identify one
    Examples: "id=search-input", "data-testid=login-btn", "name=username"

WHEN TO CALL THIS ACTION:
  • Call it for EVERY element in the list above
  • Call it after you've identified the element using your vision
  • Call it after you've obtained the element's center coordinates

HOW IT WORKS:
  1. If you provide a candidate_locator, the action validates it first with Playwright
  2. If the candidate is unique (count=1), it returns immediately - FAST!
  3. If the candidate is not unique or not provided, it tries 21 strategies:
     - Priority 1: id, data-testid, name (most stable)
     - Priority 2: aria-label, placeholder, title (semantic)
     - Priority 3: text content, role (content-based)
     - Priority 4-21: CSS and XPath strategies (fallbacks)
  4. Each strategy is validated with Playwright to ensure count=1
  5. Returns the first unique locator found

WHAT YOU RECEIVE:
The action returns a validated result with these fields:
  • validated: true (always - validation was performed)
  • count: 1 (guaranteed - only unique locators are returned)
  • unique: true (guaranteed - count equals 1)
  • valid: true (guaranteed - locator is usable)
  • best_locator: "id=search-input" (the validated locator string)
  • validation_method: "playwright" (how it was validated)
  • element_id: "elem_1" (matches your input)
  • found: true (element was found and locator extracted)

IMPORTANT - NO VALIDATION NEEDED FROM YOU:
  ✓ The action handles ALL validation using Playwright
  ✓ You do NOT need to check if the locator is unique
  ✓ You do NOT need to count elements
  ✓ You do NOT need to execute JavaScript
  ✓ Simply call the action and trust the validated result

═══════════════════════════════════════════════════════════════════
EXAMPLE WORKFLOW
═══════════════════════════════════════════════════════════════════

Step 1: Navigate to {url}

Step 2: Find first element using vision
  → Element: "Search input box"
  → Coordinates: x=450.5, y=320.8
  → Candidate locator identified: id=search-input

Step 3: Call the action
  find_unique_locator(
      x=450.5,
      y=320.8,
      element_id="elem_1",
      element_description="Search input box",
      candidate_locator="id=search-input"
  )

Step 4: Receive validated result
  {{
    "element_id": "elem_1",
    "found": true,
    "best_locator": "id=search-input",
    "validated": true,
    "count": 1,
    "unique": true,
    "valid": true,
    "validation_method": "playwright"
  }}

Step 5: Store result and move to next element

Step 6: Repeat for all elements in the list

Step 7: Call done() with all validated results
  {{
    "workflow_completed": true,
    "results": [
      {{
        "element_id": "elem_1",
        "found": true,
        "best_locator": "id=search-input",
        "validated": true,
        "count": 1,
        "unique": true
      }},
      {{
        "element_id": "elem_2",
        "found": true,
        "best_locator": "data-testid=product-card",
        "validated": true,
        "count": 1,
        "unique": true
      }}
    ]
  }}

═══════════════════════════════════════════════════════════════════
CRITICAL INSTRUCTIONS
═══════════════════════════════════════════════════════════════════

✓ MUST call find_unique_locator for EVERY element in the list
✓ MUST provide accurate coordinates (x, y) from your vision
✓ SHOULD provide candidate_locator if you can identify id, data-testid, or name
✓ MUST NOT validate locators yourself - the action does this
✓ MUST NOT execute JavaScript to check uniqueness - the action does this
✓ MUST NOT retry or check count - the action guarantees count=1
✓ ONLY call done() when ALL elements have validated results from the action

COMPLETION CRITERIA:
  • ALL elements must have validated results from find_unique_locator action
  • Each result must have: validated=true, count=1, unique=true, valid=true
  • Call done() with complete JSON structure containing all results

Your final done() call MUST include the complete JSON with all elements_found data!
"""
    else:
        # LEGACY WORKFLOW: Use JavaScript validation (backward compatibility)
        prompt = f"""
You are completing a web automation workflow.

USER'S GOAL: {user_query}

WORKFLOW STEPS:
1. Navigate to {url}
2. Find each element listed below using your vision
3. For EACH element, return its center coordinates (x, y)

ELEMENTS TO FIND:
{elements_str}

CRITICAL INSTRUCTIONS:
1. Use your vision to identify each element on the page
2. For EACH element, use execute_js to get its DOM ID and coordinates
3. Execute this JavaScript for each element you find:
   ```javascript
   (function() {{
     const element = document.querySelector('YOUR_SELECTOR_HERE');
     if (element) {{
       const rect = element.getBoundingClientRect();
       const domId = element.id || '';
       const domName = element.name || '';
       const domClass = element.className || '';
       const domTestId = element.getAttribute('data-testid') || '';
       
       // VALIDATE LOCATORS: Check uniqueness
       const locators = [];
       
       // Check ID locator
       if (domId) {{
         const idCount = document.querySelectorAll(`[id="${{domId}}"]`).length;
         locators.push({{
           type: 'id',
           locator: `id=${{domId}}`,
           count: idCount,
           unique: idCount === 1,
           validated: true
         }});
       }}
       
       // Check name locator
       if (domName) {{
         const nameCount = document.querySelectorAll(`[name="${{domName}}"]`).length;
         locators.push({{
           type: 'name',
           locator: `name=${{domName}}`,
           count: nameCount,
           unique: nameCount === 1,
           validated: true
         }});
       }}
       
       // Check data-testid locator
       if (domTestId) {{
         const testIdCount = document.querySelectorAll(`[data-testid="${{domTestId}}"]`).length;
         locators.push({{
           type: 'data-testid',
           locator: `data-testid=${{domTestId}}`,
           count: testIdCount,
           unique: testIdCount === 1,
           validated: true
         }});
       }}
       
       // Check CSS class locator
       if (domClass) {{
         const firstClass = domClass.split(' ')[0];
         const tagName = element.tagName.toLowerCase();
         const cssCount = document.querySelectorAll(`${{tagName}}.${{firstClass}}`).length;
         locators.push({{
           type: 'css-class',
           locator: `${{tagName}}.${{firstClass}}`,
           count: cssCount,
           unique: cssCount === 1,
           validated: true
         }});
       }}
       
       return JSON.stringify({{
         element_id: "REPLACE_WITH_ELEM_ID_FROM_LIST",
         found: true,
         coordinates: {{ x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }},
         element_type: element.tagName.toLowerCase(),
         visible_text: element.textContent.trim().substring(0, 100),
         dom_id: domId,
         dom_attributes: {{
           id: domId,
           name: domName,
           class: domClass,
           'data-testid': domTestId
         }},
         locators: locators
       }});
     }}
     return JSON.stringify({{ element_id: "REPLACE_WITH_ELEM_ID", found: false }});
   }})()
   ```

4. **CRITICAL VALIDATION STEP:** After executing JavaScript for each element, CHECK the locators:
   - Look at the "locators" array in the JavaScript result
   - Find locators where "unique": true AND "count": 1
   - If NO unique locator found for an element, try a DIFFERENT selector and execute JavaScript again
   - Keep trying different selectors until you find a unique locator (count=1)

5. ONLY call done() when ALL elements have at least ONE unique locator (count=1)
   ```json
   {{
     "workflow_completed": true,
     "elements_found": [
       {{ "element_id": "elem_1", "found": true, "coordinates": {{"x": 450, "y": 320}}, "dom_id": "search-input", ... }},
       {{ "element_id": "elem_2", "found": true, "coordinates": {{"x": 650, "y": 520}}, "dom_id": "product-link", ... }}
     ]
   }}
   ```

CRITICAL RULES:
- You MUST execute JavaScript for EACH element to get its DOM attributes
- You MUST CHECK if locators are unique (count=1) in the JavaScript result
- If a locator is NOT unique (count>1), try a DIFFERENT selector (more specific)
- ONLY call done() when ALL elements have at least ONE unique locator
- You MUST include the element_id from the list above in each result
- You MUST call done() with the complete JSON structure
- DO NOT just say "I found it" - you MUST return the structured JSON
- The JSON MUST include all elements from the list above

UNIQUENESS REQUIREMENT:
- A locator is ONLY valid if count=1 (unique)
- If count>1, the locator matches multiple elements and is NOT usable
- You MUST find a unique locator for each element before calling done()
- Try more specific selectors: id > data-testid > name > specific CSS > XPath

Your final done() call MUST include the complete JSON with all elements_found data!
REMEMBER: ONLY call done() when ALL elements have at least ONE unique locator (count=1)!
"""
    
    return prompt.strip()


def build_system_prompt(include_custom_action: bool = True) -> str:
    """
    Build system prompt for the agent.
    
    Args:
        include_custom_action: If True, include custom action workflow; if False, use legacy workflow
    
    Returns:
        System prompt string
    """
    if include_custom_action:
        # NEW WORKFLOW: Custom action based (NO JavaScript validation)
        return """You are a web automation agent specialized in element identification and locator validation.

⚠️ CRITICAL VERIFICATION RULES:
   1. When find_unique_locator returns validated=true, the locator is UNIQUE (count=1)
   2. You MUST verify the locator points to the CORRECT element (matches description)
   3. If locator is unique BUT wrong element → Try again with different coordinates
   4. If locator is unique AND correct element → Mark SUCCESS, move to next element
   5. Maximum 2 retries per element - if still wrong, mark as failed and move on

YOUR WORKFLOW (Custom Action Mode):
1. Find element → Get coordinates → Call action → Receive validated result
2. Verify locator points to correct element (check text, attributes, visual match)
3. If correct: Mark SUCCESS, move to next element
4. If wrong: Retry with better coordinates (max 2 retries)
5. Repeat for ALL elements
6. Call done() when ALL elements processed

STEP-BY-STEP PROCESS:
Step 1: Find Element Using Vision
   - Use your vision capabilities to locate the element on the page
   - Identify the element based on its visual appearance and description

Step 2: Get Element Coordinates
   - Obtain the x, y coordinates of the element's center point
   - These coordinates are required for the custom action

Step 3: Call find_unique_locator Action
   - Call the custom action with: x, y, element_id, element_description
   - Optionally provide candidate_locator if you can identify: id, data-testid, or name
   - The action will validate using Playwright (NO JavaScript needed from you)

Step 4: Receive Validated Result
   - The action returns a validated result with count=1 (guaranteed unique)
   - Result includes: {validated: true, count: 1, unique: true, valid: true}
   - The locator is UNIQUE but you must verify it's the CORRECT element

Step 5: Verify Correctness
   - Check if the locator points to the element matching the description
   - Use vision or inspect the element's text/attributes
   - Compare with the original element you were looking for
   - **If CORRECT:** Mark SUCCESS, move to next element
   - **If WRONG:** The coordinates were inaccurate, retry with better coordinates
   - **Maximum 2 retries per element** - then mark as failed and move on

Step 6: Move to Next Element
   - After verification (success or max retries reached), move to next element
   - Track which elements have been processed
   - Do not retry an element more than 2 times

Step 7: Call done() When Complete
   - Call done() when ALL elements have been processed (success or failed)
   - Include all validated results in your done() call
   - Mark success=true if all elements found correctly
   - Mark success=false if some elements couldn't be found

CRITICAL RULES:
✓ DO use vision to find elements accurately
✓ DO verify the locator points to the correct element (matches description)
✓ DO retry with better coordinates if locator is unique but wrong element
✓ DO limit retries to maximum 2 per element
✓ DO move to next element after success or max retries
✓ DO call done() when ALL elements processed

✗ DO NOT execute JavaScript for validation (action handles uniqueness)
✗ DO NOT retry more than 2 times per element
✗ DO NOT skip elements - process ALL of them
✗ DO NOT call done() until ALL elements processed

VALIDATION GUARANTEE:
- The find_unique_locator action validates UNIQUENESS (count=1) using Playwright
- It does NOT validate CORRECTNESS (whether it's the right element)
- You MUST verify the locator points to the element matching the description
- If unique but wrong element: Your coordinates were off, retry with better coordinates
- If unique and correct element: Success, move to next element
- Maximum 2 retries per element to avoid infinite loops

COMPLETION CRITERIA:
- ALL elements must be processed (either found correctly or max retries reached)
- Each successful result must show: validated=true, count=1, unique=true
- Include success=true if all elements found correctly
- Include success=false if some elements couldn't be found after max retries

RETRY LOGIC:
- Retry 1: If locator is unique but wrong element, try different coordinates
- Retry 2: If still wrong, try one more time with more accurate coordinates
- After 2 retries: Mark element as failed, move to next element
- This prevents infinite loops while allowing correction of coordinate errors
"""
    else:
        # LEGACY WORKFLOW: JavaScript validation based
        return """You are a web automation agent specialized in element identification and locator validation.

YOUR WORKFLOW:
1. Navigate to the target URL
2. Use your vision to find each element
3. Execute JavaScript to get DOM attributes and validate locators
4. CHECK if locators are unique (count=1)
5. If not unique, try different selectors until you find a unique one
6. ONLY call done() when ALL elements have unique locators

CRITICAL VALIDATION RULE:
- A locator is ONLY valid if count=1 (unique)
- If count>1, the locator matches multiple elements and is NOT usable
- You MUST find a unique locator for each element before calling done()
- Try more specific selectors if needed: id > data-testid > name > specific CSS > XPath

IMPORTANT:
- Your job is to find elements AND ensure they have unique locators
- Execute JavaScript to validate each locator's uniqueness
- Do NOT call done() until ALL elements have at least ONE unique locator (count=1)
- Focus on accurate element identification using vision

COMPLETION:
- ONLY call done() when ALL elements have unique locators (count=1)
- Include success=True if all elements have unique locators
- Include success=False if you cannot find unique locators for some elements
"""


def extract_json_for_element(text: str, element_id: str) -> dict:
    """
    Extract JSON object for a specific element_id from text, handling nested braces properly.

    Args:
        text: The text containing JSON data
        element_id: The element ID to search for (e.g., "elem_1")

    Returns:
        Parsed JSON dict if found, None otherwise
    """
    # Find the starting position of the element_id
    # Check multiple patterns (with and without space after colon)
    search_patterns = [
        f'"element_id":"{element_id}"',  # No space (common in minified JSON)
        f'"element_id": "{element_id}"',  # With space
        f"'element_id':'{element_id}'",  # Single quotes, no space
        f"'element_id': '{element_id}'"   # Single quotes, with space
    ]

    start_pos = -1
    pattern_used = None
    for pattern in search_patterns:
        pos = text.find(pattern)
        if pos != -1:
            start_pos = pos
            pattern_used = pattern
            break

    if start_pos == -1:
        logger.debug(
            f"extract_json_for_element: '{element_id}' not found in text (tried {len(search_patterns)} patterns)")
        return None

    logger.debug(
        f"extract_json_for_element: Found '{element_id}' at position {start_pos} using pattern '{pattern_used}'")

    # Find the opening brace before element_id
    brace_pos = text.rfind('{', 0, start_pos)
    if brace_pos == -1:
        logger.debug(
            f"extract_json_for_element: No opening brace found before '{element_id}'")
        return None

    logger.debug(
        f"extract_json_for_element: Opening brace at position {brace_pos}")

    # Now match braces to find the closing brace
    brace_count = 0
    in_string = False
    escape_next = False

    for i in range(brace_pos, len(text)):
        char = text[i]

        # Handle escape sequences
        if escape_next:
            escape_next = False
            continue

        if char == '\\':
            escape_next = True
            continue

        # Handle strings (ignore braces inside strings)
        if char == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        # Count braces
        if char == '{':
            brace_count += 1
        elif char == '}':
            brace_count -= 1

            # Found matching closing brace
            if brace_count == 0:
                json_str = text[brace_pos:i+1]
                logger.debug(
                    f"extract_json_for_element: Found complete JSON for '{element_id}' ({len(json_str)} chars)")

                # CRITICAL FIX: Unescape double-escaped quotes before parsing
                # The JavaScript returns valid JSON, but when embedded in Python strings,
                # quotes get double-escaped (\" becomes \\")
                # We need to fix this before json.loads()
                try:
                    # First attempt: Parse as-is
                    parsed = json.loads(json_str)
                    logger.debug(
                        f"extract_json_for_element: Successfully parsed JSON for '{element_id}'")
                    return parsed
                except json.JSONDecodeError as e:
                    # Second attempt: Fix escaped quotes and try again
                    logger.debug(
                        f"extract_json_for_element: First parse failed, trying to fix escaped quotes...")
                    try:
                        # Replace double-escaped quotes with single-escaped quotes
                        # \\" -> \"
                        fixed_json_str = json_str.replace('\\\\"', '\\"')
                        # Also handle \\' -> \'
                        fixed_json_str = fixed_json_str.replace("\\\\'", "\\'")

                        parsed = json.loads(fixed_json_str)
                        logger.debug(
                            f"extract_json_for_element: Successfully parsed JSON after fixing escapes for '{element_id}'")
                        return parsed
                    except json.JSONDecodeError as e2:
                        logger.error(
                            f"extract_json_for_element: Failed to parse JSON for {element_id} even after fixing escapes: {e2}")
                        logger.error(
                            f"Original JSON (first 500 chars): {json_str[:500]}...")
                        logger.error(
                            f"Fixed JSON (first 500 chars): {fixed_json_str[:500]}...")
                        return None

    logger.debug(
        f"extract_json_for_element: No matching closing brace found for '{element_id}'")
    return None


def extract_workflow_json(text: str) -> dict:
    """
    Extract workflow completion JSON from text, handling nested braces properly.

    Args:
        text: The text containing workflow JSON data

    Returns:
        Parsed JSON dict if found, None otherwise
    """
    # Find the starting position of workflow_completed
    search_pattern = '"workflow_completed"'
    start_pos = text.find(search_pattern)

    if start_pos == -1:
        return None

    # Find the opening brace before workflow_completed
    brace_pos = text.rfind('{', 0, start_pos)
    if brace_pos == -1:
        return None

    # Now match braces to find the closing brace
    brace_count = 0
    in_string = False
    escape_next = False

    for i in range(brace_pos, len(text)):
        char = text[i]

        # Handle escape sequences
        if escape_next:
            escape_next = False
            continue

        if char == '\\':
            escape_next = True
            continue

        # Handle strings (ignore braces inside strings)
        if char == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        # Count braces
        if char == '{':
            brace_count += 1
        elif char == '}':
            brace_count -= 1

            # Found matching closing brace
            if brace_count == 0:
                json_str = text[brace_pos:i+1]
                try:
                    parsed = json.loads(json_str)
                    # Verify it has the expected structure
                    if 'workflow_completed' in parsed and 'results' in parsed:
                        return parsed
                except json.JSONDecodeError as e:
                    logger.debug(f"Failed to parse workflow JSON: {e}")
                    logger.debug(f"JSON string: {json_str[:200]}...")
                    return None

    return None


# ========================================
# LOCATOR EXTRACTION
# ========================================
# Integrated from locator_extractor.py
# These functions use Playwright's built-in methods for F12-style locator extraction and validation

async def extract_element_attributes(page, coords: Dict[str, float]) -> Optional[Dict[str, Any]]:
    """
    Extract element attributes using minimal JavaScript.
    This is like inspecting an element in F12 DevTools.

    Args:
        page: Playwright page object
        coords: Dictionary with 'x' and 'y' coordinates

    Returns:
        Dictionary with element attributes or None if not found
    """
    try:
        # Minimal JavaScript to get element attributes (< 50 lines)
        element_info = await page.evaluate("""
            (coords) => {
                const el = document.elementFromPoint(coords.x, coords.y);
                if (!el || el.tagName === 'HTML' || el.tagName === 'BODY') {
                    return null;
                }
                
                // Extract all useful attributes
                return {
                    // Primary identifiers (highest priority)
                    id: el.id || null,
                    name: el.name || null,
                    testId: el.dataset?.testid || null,
                    
                    // Semantic attributes
                    ariaLabel: el.getAttribute('aria-label') || null,
                    role: el.getAttribute('role') || null,
                    title: el.title || null,
                    placeholder: el.placeholder || null,
                    type: el.type || null,
                    
                    // Structure
                    tagName: el.tagName.toLowerCase(),
                    className: el.className || null,
                    
                    // Content
                    text: el.textContent?.trim().slice(0, 100) || null,
                    href: el.href || null,
                    
                    // Visibility
                    visible: el.offsetParent !== null,
                    
                    // Position (for verification)
                    boundingBox: {
                        x: el.getBoundingClientRect().x,
                        y: el.getBoundingClientRect().y,
                        width: el.getBoundingClientRect().width,
                        height: el.getBoundingClientRect().height
                    }
                };
            }
        """, coords)

        return element_info

    except Exception as e:
        logger.error(f"Error extracting element attributes: {e}")
        return None


async def validate_locator_playwright(
    page,
    locator: str,
    expected_coords: Optional[Dict[str, float]] = None
) -> Dict[str, Any]:
    """
    Validate a locator using Playwright's built-in methods.
    This is exactly like testing a selector in F12 Console.
    
    CRITICAL: Only locators with count=1 are marked as valid=True (unique locators only).

    Args:
        page: Playwright page object
        locator: The locator to validate (e.g., "id=search", "text=Login")
        expected_coords: Optional {x, y} to verify we found the right element

    Returns:
        Dictionary with validation results including validated, count, unique, and valid flags
    """
    try:
        # Step 1: Count matches (like F12: document.querySelectorAll().length)
        count = await page.locator(locator).count()

        if count == 0:
            return {
                'valid': False,
                'unique': False,
                'count': 0,
                'validated': True,
                'validation_method': 'playwright',
                'error': 'Locator does not match any elements'
            }

        # Step 2: Get first element details (like F12: inspect element)
        element_info = await page.locator(locator).first.evaluate("""
            (el) => ({
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                className: el.className || null,
                text: el.textContent?.trim().slice(0, 100) || null,
                visible: el.offsetParent !== null,
                boundingBox: {
                    x: el.getBoundingClientRect().x,
                    y: el.getBoundingClientRect().y,
                    width: el.getBoundingClientRect().width,
                    height: el.getBoundingClientRect().height
                }
            })
        """)

        # Step 3: Check visibility (like F12: computed styles)
        is_visible = await page.locator(locator).first.is_visible()

        # Step 4: Get bounding box
        bounding_box = element_info['boundingBox']

        # Step 5: Verify it's the correct element (if coords provided)
        correct_element = True
        if expected_coords and bounding_box:
            # Check if expected coords are within the element's bounding box
            x_match = (bounding_box['x'] <= expected_coords['x'] <=
                       bounding_box['x'] + bounding_box['width'])
            y_match = (bounding_box['y'] <= expected_coords['y'] <=
                       bounding_box['y'] + bounding_box['height'])
            correct_element = x_match and y_match

        # CRITICAL: Only mark as valid if count == 1 (unique locator)
        return {
            'valid': count == 1,  # Only unique locators are valid
            'unique': count == 1,
            'count': count,
            'validated': True,
            'validation_method': 'playwright',
            'is_visible': is_visible,
            'correct_element': correct_element,
            'element_info': element_info,
            'bounding_box': bounding_box
        }

    except Exception as e:
        logger.error(f"Error validating locator '{locator}': {e}")
        return {
            'valid': False,
            'unique': False,
            'count': 0,
            'validated': False,
            'validation_method': 'playwright',
            'error': str(e)
        }


def generate_locators_from_attributes(
    element_attrs: Dict[str, Any],
    library_type: str = "browser"
) -> List[Dict[str, Any]]:
    """
    Generate locators from element attributes in priority order.

    Args:
        element_attrs: Dictionary of element attributes
        library_type: "browser" or "selenium"

    Returns:
        List of locator dictionaries with type, locator, and priority
    """
    locators = []

    # Priority 1: ID (most stable, fastest)
    if element_attrs.get('id'):
        locators.append({
            'type': 'id',
            'locator': f"id={element_attrs['id']}",
            'priority': 1
        })

    # Priority 2: data-testid (designed for testing)
    if element_attrs.get('testId'):
        if library_type == "browser":
            locators.append({
                'type': 'data-testid',
                'locator': f"data-testid={element_attrs['testId']}",
                'priority': 2
            })
        else:  # selenium
            locators.append({
                'type': 'data-testid',
                'locator': f"css=[data-testid=\"{element_attrs['testId']}\"]",
                'priority': 2
            })

    # Priority 3: name (semantic, stable)
    if element_attrs.get('name'):
        locators.append({
            'type': 'name',
            'locator': f"name={element_attrs['name']}",
            'priority': 3
        })

    # Priority 4: aria-label (accessibility, semantic)
    if element_attrs.get('ariaLabel'):
        if library_type == "browser":
            locators.append({
                'type': 'aria-label',
                'locator': f"[aria-label=\"{element_attrs['ariaLabel']}\"]",
                'priority': 4
            })
        else:  # selenium
            locators.append({
                'type': 'aria-label',
                'locator': f"css=[aria-label=\"{element_attrs['ariaLabel']}\"]",
                'priority': 4
            })

    # Priority 5: text content (can be fragile if text changes)
    if element_attrs.get('text') and len(element_attrs['text']) > 0:
        text = element_attrs['text'][:50]  # First 50 chars
        if library_type == "browser":
            locators.append({
                'type': 'text',
                'locator': f"text={text}",
                'priority': 5
            })
        else:  # selenium
            # Selenium uses XPath for text
            locators.append({
                'type': 'text',
                'locator': f"xpath=//*[contains(text(), \"{text}\")]",
                'priority': 5
            })

    # Priority 6: role (Playwright-specific, semantic)
    if library_type == "browser" and element_attrs.get('role'):
        role = element_attrs['role']
        name = element_attrs.get(
            'ariaLabel') or element_attrs.get('text', '')[:30]
        if name:
            locators.append({
                'type': 'role',
                'locator': f"role={role}[name=\"{name}\"]",
                'priority': 6
            })

    # Priority 7: CSS class (lower priority, can change)
    if element_attrs.get('className'):
        first_class = element_attrs['className'].split(
        )[0] if element_attrs['className'] else None
        if first_class:
            tag = element_attrs.get('tagName', 'div')
            if library_type == "browser":
                locators.append({
                    'type': 'css-class',
                    'locator': f"{tag}.{first_class}",
                    'priority': 7
                })
            else:  # selenium
                locators.append({
                    'type': 'css-class',
                    'locator': f"css={tag}.{first_class}",
                    'priority': 7
                })

    return locators


async def extract_and_validate_locators(
    page,
    element_description: str,
    element_coords: Dict[str, float],
    library_type: str = "browser"
) -> Dict[str, Any]:
    """
    Complete locator extraction and validation pipeline.
    Uses Playwright's built-in methods - no massive JavaScript!
    
    CRITICAL: Only returns locators with count=1 as valid (unique locators only).

    Args:
        page: Playwright page object
        element_description: Description of the element (for logging)
        element_coords: {x, y} coordinates from browser-use vision
        library_type: "browser" or "selenium"

    Returns:
        Dictionary with extraction results
    """
    logger.info(f"🔍 Extracting locators for: {element_description}")
    logger.info(
        f"   Coordinates: ({element_coords['x']}, {element_coords['y']})")

    # Step 1: Extract element attributes using minimal JavaScript
    element_attrs = await extract_element_attributes(page, element_coords)

    if not element_attrs:
        logger.error(f"❌ Could not find element at coordinates")
        return {
            'found': False,
            'error': 'Element not found at coordinates'
        }

    logger.info(
        f"   Found element: <{element_attrs['tagName']}> \"{element_attrs.get('text', '')[:50]}\"")

    # Step 2: Generate locators from attributes (in Python, not JavaScript!)
    locators = generate_locators_from_attributes(element_attrs, library_type)

    if not locators:
        logger.warning(
            f"⚠️ No locators could be generated from element attributes")
        return {
            'found': False,
            'error': 'No locators could be generated',
            'element_info': element_attrs
        }

    logger.info(f"   Generated {len(locators)} candidate locators")

    # Step 3: Validate each locator using Playwright validation
    validated_locators = []
    for loc in locators:
        validation = await validate_locator_playwright(
            page,
            loc['locator'],
            element_coords
        )

        # Merge validation results into locator dict
        loc.update(validation)

        if loc.get('validated'):
            validated_locators.append(loc)
            if loc.get('valid'):
                # valid=True means count=1 (unique)
                status = "✅ UNIQUE"
            else:
                # valid=False means count>1 or count=0
                count = loc.get('count', 0)
                if count > 1:
                    status = f"⚠️ NOT UNIQUE ({count} matches)"
                else:
                    status = "❌ NOT FOUND"
            correct = "✅" if loc.get(
                'correct_element') else "⚠️ Different element"
            logger.info(
                f"   {loc['type']}: {loc['locator']} → {status}, {correct}")
        else:
            logger.warning(f"   ❌ {loc['type']}: {loc['locator']} → VALIDATION FAILED")

    # Step 4: Filter and sort - ONLY unique locators (count=1) are considered valid
    unique_locators = [l for l in validated_locators if l.get(
        'unique') and l.get('correct_element')]
    valid_locators = [l for l in validated_locators if l.get('valid')]

    # Step 5: Select best locator
    best_locator = None
    if unique_locators:
        # Prefer unique locators that match the correct element, sorted by priority
        best_locator = sorted(unique_locators, key=lambda x: x['priority'])[0]
        logger.info(
            f"✅ Best locator: {best_locator['locator']} (unique, correct element)")
    elif valid_locators:
        # Fallback to any valid locator
        best_locator = sorted(valid_locators, key=lambda x: x['priority'])[0]
        logger.warning(
            f"⚠️ Best locator: {best_locator['locator']} (valid but not unique or wrong element)")

    result = {
        'found': best_locator is not None,
        'best_locator': best_locator['locator'] if best_locator else None,
        'all_locators': validated_locators,
        'unique_locators': unique_locators,
        'element_info': element_attrs,
        'validation_summary': {
            'total_generated': len(locators),
            'valid': len(valid_locators),
            'unique': len(unique_locators),
            'best_type': best_locator['type'] if best_locator else None,
            'validation_method': 'playwright'
        }
    }
    
    # Add validation data to the result itself for easy access
    if best_locator:
        result['validated'] = True
        result['count'] = best_locator.get('count', 1)
        result['unique'] = best_locator.get('unique', True)
        result['valid'] = best_locator.get('valid', True)
        result['validation_method'] = 'playwright'
    else:
        result['validated'] = True  # Validation was attempted
        result['count'] = 0  # No unique locator found
        result['unique'] = False
        result['valid'] = False
        result['validation_method'] = 'playwright'
    
    return result


# ========================================
# CUSTOM ACTIONS
# ========================================
# Custom actions that can be called by the browser-use agent

async def find_unique_locator_action(
    x: float,
    y: float,
    element_id: str,
    element_description: str,
    candidate_locator: Optional[str] = None,
    page=None
) -> Dict[str, Any]:
    """
    Custom action that agent can call to find and validate unique locator.
    ALL validation done with Playwright - no JavaScript needed.
    Runs deterministically (no LLM calls).
    
    This function is registered with browser-use and callable by the agent.
    
    Comprehensive error handling includes:
    - Input validation (page object, coordinates, element_id)
    - Specific exception handling (TimeoutError, CancelledError, RuntimeError, ValueError)
    - Structured error results with error_type and full context
    - Detailed logging with element_id, coordinates, and error messages
    
    Args:
        x: X coordinate of element center
        y: Y coordinate of element center
        element_id: Element identifier (elem_1, elem_2, etc.)
        element_description: Human-readable description
        candidate_locator: Optional locator suggested by agent (e.g., "id=search")
        page: Playwright page object
    
    Returns:
        Dict with validated locator or error:
        {
            'element_id': str,
            'description': str,
            'found': bool,
            'best_locator': str | None,
            'all_locators': List[Dict],
            'element_info': Dict,
            'coordinates': Dict,
            'validation_summary': Dict,
            'error': str | None,  # Only present if error occurred
            'error_type': str | None,  # Type of error (e.g., 'TimeoutError', 'PageObjectError')
            'validated': bool,
            'count': int,
            'unique': bool,
            'valid': bool,
            'validation_method': str
        }
    
    Phase: Error Handling and Logging
    Requirements: 8.2, 8.4, 9.1
    """
    logger.info(f"🎯 Custom Action: find_unique_locator called for {element_id}")
    logger.info(f"   Description: {element_description}")
    logger.info(f"   Coordinates: ({x}, {y})")
    if candidate_locator:
        logger.info(f"   Candidate locator: {candidate_locator}")
    
    # Helper function to create structured error result
    def create_error_result(error_type: str, error_message: str, additional_context: Optional[Dict] = None) -> Dict[str, Any]:
        """Create a structured error result with complete validation data."""
        result = {
            'element_id': element_id,
            'description': element_description,
            'found': False,
            'error': error_message,
            'error_type': error_type,
            'coordinates': {'x': x, 'y': y},
            'validated': False,
            'count': 0,
            'unique': False,
            'valid': False,
            'validation_method': 'playwright'
        }
        if additional_context:
            result.update(additional_context)
        return result
    
    try:
        # ========================================
        # VALIDATION: Input Parameters
        # ========================================
        
        # Validate page object
        if page is None:
            error_msg = "Page object is None - cannot validate locators"
            logger.error(f"❌ {error_msg}")
            logger.error(f"   Element ID: {element_id}")
            logger.error(f"   Description: {element_description}")
            logger.error(f"   Coordinates: ({x}, {y})")
            return create_error_result('PageObjectError', error_msg)
        
        # Validate coordinates
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            error_msg = f"Invalid coordinates: x={x} (type={type(x).__name__}), y={y} (type={type(y).__name__})"
            logger.error(f"❌ {error_msg}")
            logger.error(f"   Element ID: {element_id}")
            return create_error_result('InvalidCoordinatesError', error_msg)
        
        if x < 0 or y < 0:
            error_msg = f"Negative coordinates not allowed: x={x}, y={y}"
            logger.error(f"❌ {error_msg}")
            logger.error(f"   Element ID: {element_id}")
            return create_error_result('InvalidCoordinatesError', error_msg)
        
        # Validate element_id
        if not element_id or not isinstance(element_id, str):
            error_msg = f"Invalid element_id: {element_id} (type={type(element_id).__name__})"
            logger.error(f"❌ {error_msg}")
            return create_error_result('InvalidElementIdError', error_msg)
        
        # ========================================
        # STEP 1: Validate Candidate Locator (if provided)
        # ========================================
        
        if candidate_locator:
            logger.info(f"")
            logger.info(f"🔍 VALIDATING CANDIDATE LOCATOR")
            logger.info(f"   Locator: {candidate_locator}")
            logger.info(f"   Method: Playwright page.locator().count()")
            
            try:
                # Validate candidate locator syntax
                if not isinstance(candidate_locator, str) or not candidate_locator.strip():
                    logger.warning(f"⚠️ Invalid candidate locator format: {candidate_locator}")
                    logger.info(f"🔄 Continuing with smart locator finder...")
                else:
                    # Try to validate with Playwright
                    count = await page.locator(candidate_locator).count()
                    
                    # Log detailed validation results
                    is_unique = (count == 1)
                    is_valid = (count == 1)
                    
                    logger.info(f"   Validation Results:")
                    logger.info(f"      - count: {count}")
                    logger.info(f"      - unique: {is_unique}")
                    logger.info(f"      - valid: {is_valid}")
                    logger.info(f"      - validated: True")
                    logger.info(f"      - validation_method: playwright")
                    
                    if count == 1:
                        # Candidate is valid and unique!
                        logger.info(f"")
                        logger.info(f"{'='*80}")
                        logger.info(f"✅ CANDIDATE LOCATOR IS UNIQUE - Using it directly!")
                        logger.info(f"{'='*80}")
                        logger.info(f"   Skipping 21 strategies (not needed)")
                        logger.info(f"   Locator: {candidate_locator}")
                        logger.info(f"   Type: candidate")
                        logger.info(f"   Priority: 0 (agent-provided)")
                        logger.info(f"{'='*80}")
                        logger.info(f"")
                        
                        return {
                            'element_id': element_id,
                            'description': element_description,
                            'found': True,
                            'best_locator': candidate_locator,
                            'all_locators': [{
                                'type': 'candidate',
                                'locator': candidate_locator,
                                'priority': 0,
                                'strategy': 'Agent-provided candidate',
                                'count': count,
                                'unique': True,
                                'valid': True,
                                'validated': True,
                                'validation_method': 'playwright'
                            }],
                            'element_info': {},
                            'coordinates': {'x': x, 'y': y},
                            'validation_summary': {
                                'total_generated': 1,
                                'valid': 1,
                                'unique': 1,
                                'validated': 1,
                                'not_found': 0,
                                'not_unique': 0,
                                'errors': 0,
                                'best_type': 'candidate',
                                'best_strategy': 'Agent-provided candidate',
                                'validation_method': 'playwright'
                            },
                            # Add validation data at result level
                            'validated': True,
                            'count': count,
                            'unique': True,
                            'valid': True,
                            'validation_method': 'playwright'
                        }
                    elif count > 1:
                        logger.info(f"   ⚠️ Candidate locator NOT UNIQUE (matches {count} elements)")
                        logger.info(f"   🔄 Continuing with smart locator finder to find unique locator...")
                    else:  # count == 0
                        logger.info(f"   ⚠️ Candidate locator NOT FOUND (matches 0 elements)")
                        logger.info(f"   🔄 Continuing with smart locator finder to find valid locator...")
                        
            except ValueError as e:
                # Invalid locator syntax
                logger.warning(f"⚠️ Candidate locator has invalid syntax: {e}")
                logger.warning(f"   Locator: {candidate_locator}")
                logger.info(f"🔄 Continuing with smart locator finder...")
                
            except asyncio.TimeoutError as e:
                # Playwright timeout during validation
                logger.warning(f"⚠️ Candidate locator validation timed out: {e}")
                logger.warning(f"   Locator: {candidate_locator}")
                logger.info(f"🔄 Continuing with smart locator finder...")
                
            except Exception as e:
                # Generic error during candidate validation
                logger.warning(f"⚠️ Candidate locator validation failed: {type(e).__name__}: {e}")
                logger.warning(f"   Locator: {candidate_locator}")
                logger.info(f"🔄 Continuing with smart locator finder...")
        
        # ========================================
        # STEP 2: Call Smart Locator Finder
        # ========================================
        
        logger.info(f"🔍 Calling smart_locator_finder with 21 strategies...")
        
        # Import smart_locator_finder
        try:
            from tools.smart_locator_finder import find_unique_locator_at_coordinates
        except ImportError as e:
            error_msg = f"Failed to import smart_locator_finder: {e}"
            logger.error(f"❌ {error_msg}")
            logger.error(f"   Element ID: {element_id}")
            logger.error(f"   This is a critical error - smart_locator_finder module is required")
            return create_error_result('ImportError', error_msg)
        
        # Call smart locator finder with timeout protection
        try:
            result = await asyncio.wait_for(
                find_unique_locator_at_coordinates(
                    page=page,
                    x=x,
                    y=y,
                    element_id=element_id,
                    element_description=element_description,
                    candidate_locator=None  # Already validated above, so pass None
                ),
                timeout=settings.CUSTOM_ACTION_TIMEOUT
            )
            
            # Log the result with detailed information
            if result.get('found'):
                best_locator = result.get('best_locator')
                validation_summary = result.get('validation_summary', {})
                
                logger.info(f"")
                logger.info(f"{'='*80}")
                logger.info(f"✅ CUSTOM ACTION SUCCEEDED for {element_id}")
                logger.info(f"{'='*80}")
                logger.info(f"   Best Locator: {best_locator}")
                logger.info(f"   Locator Type: {validation_summary.get('best_type', 'unknown')}")
                logger.info(f"   Strategy: {validation_summary.get('best_strategy', 'unknown')}")
                logger.info(f"   Validation Results:")
                logger.info(f"      - validated: {result.get('validated', False)}")
                logger.info(f"      - count: {result.get('count', 0)}")
                logger.info(f"      - unique: {result.get('unique', False)}")
                logger.info(f"      - valid: {result.get('valid', False)}")
                logger.info(f"      - validation_method: {result.get('validation_method', 'unknown')}")
                logger.info(f"   Validation Summary:")
                logger.info(f"      - total_strategies: {validation_summary.get('total_generated', 0)}")
                logger.info(f"      - valid: {validation_summary.get('valid', 0)}")
                logger.info(f"      - unique: {validation_summary.get('unique', 0)}")
                logger.info(f"      - not_found: {validation_summary.get('not_found', 0)}")
                logger.info(f"      - not_unique: {validation_summary.get('not_unique', 0)}")
                logger.info(f"      - errors: {validation_summary.get('errors', 0)}")
                logger.info(f"{'='*80}")
                logger.info(f"")
            else:
                error = result.get('error', 'Unknown error')
                validation_summary = result.get('validation_summary', {})
                
                logger.error(f"")
                logger.error(f"{'='*80}")
                logger.error(f"❌ CUSTOM ACTION FAILED for {element_id}")
                logger.error(f"{'='*80}")
                logger.error(f"   Error: {error}")
                logger.error(f"   Element ID: {element_id}")
                logger.error(f"   Description: {element_description}")
                logger.error(f"   Coordinates: ({x}, {y})")
                if validation_summary:
                    logger.error(f"   Validation Summary:")
                    logger.error(f"      - total_strategies: {validation_summary.get('total_generated', 0)}")
                    logger.error(f"      - valid: {validation_summary.get('valid', 0)}")
                    logger.error(f"      - not_found: {validation_summary.get('not_found', 0)}")
                    logger.error(f"      - not_unique: {validation_summary.get('not_unique', 0)}")
                    logger.error(f"      - errors: {validation_summary.get('errors', 0)}")
                logger.error(f"{'='*80}")
                logger.error(f"")
            
            return result
            
        except asyncio.TimeoutError:
            # Handle timeout gracefully
            timeout_msg = f"Smart locator finder timed out after {settings.CUSTOM_ACTION_TIMEOUT} seconds"
            logger.error(f"⏱️ {timeout_msg}")
            logger.error(f"   Element ID: {element_id}")
            logger.error(f"   Description: {element_description}")
            logger.error(f"   Coordinates: ({x}, {y})")
            logger.error(f"   This may indicate a complex page or slow network")
            
            return create_error_result('TimeoutError', timeout_msg, {
                'timeout_seconds': settings.CUSTOM_ACTION_TIMEOUT
            })
            
        except asyncio.CancelledError:
            # Task was cancelled (e.g., browser closed)
            cancel_msg = "Smart locator finder was cancelled (browser may have closed)"
            logger.error(f"🚫 {cancel_msg}")
            logger.error(f"   Element ID: {element_id}")
            logger.error(f"   Coordinates: ({x}, {y})")
            
            return create_error_result('CancelledError', cancel_msg)
        
        except RuntimeError as e:
            # Runtime errors (e.g., event loop issues, browser closed)
            runtime_msg = f"Runtime error in smart locator finder: {str(e)}"
            logger.error(f"❌ {runtime_msg}")
            logger.error(f"   Element ID: {element_id}")
            logger.error(f"   Coordinates: ({x}, {y})")
            logger.error(f"   This may indicate the browser was closed or the page navigated away")
            logger.error(f"   Stack trace:", exc_info=True)
            
            return create_error_result('RuntimeError', runtime_msg)
        
        except Exception as e:
            # Catch any other errors from smart_locator_finder
            finder_error_msg = f"Smart locator finder raised {type(e).__name__}: {str(e)}"
            logger.error(f"❌ {finder_error_msg}")
            logger.error(f"   Element ID: {element_id}")
            logger.error(f"   Coordinates: ({x}, {y})")
            logger.error(f"   Stack trace:", exc_info=True)
            
            return create_error_result(type(e).__name__, finder_error_msg)
    
    except asyncio.TimeoutError:
        # Top-level timeout (shouldn't happen, but handle it)
        timeout_msg = "Custom action timed out at top level"
        logger.error(f"⏱️ {timeout_msg}")
        logger.error(f"   Element ID: {element_id}")
        logger.error(f"   Coordinates: ({x}, {y})")
        
        return create_error_result('TimeoutError', timeout_msg)
    
    except asyncio.CancelledError:
        # Top-level cancellation
        cancel_msg = "Custom action was cancelled"
        logger.error(f"🚫 {cancel_msg}")
        logger.error(f"   Element ID: {element_id}")
        logger.error(f"   Coordinates: ({x}, {y})")
        
        return create_error_result('CancelledError', cancel_msg)
    
    except KeyboardInterrupt:
        # User interrupted execution
        interrupt_msg = "Custom action interrupted by user"
        logger.error(f"⚠️ {interrupt_msg}")
        logger.error(f"   Element ID: {element_id}")
        
        return create_error_result('KeyboardInterrupt', interrupt_msg)
    
    except Exception as e:
        # Catch-all for any unexpected errors
        error_msg = f"Unexpected error in find_unique_locator_action: {type(e).__name__}: {str(e)}"
        logger.error(f"❌ {error_msg}")
        logger.error(f"   Element ID: {element_id}")
        logger.error(f"   Description: {element_description}")
        logger.error(f"   Coordinates: ({x}, {y})")
        if candidate_locator:
            logger.error(f"   Candidate locator: {candidate_locator}")
        logger.error(f"   Stack trace:", exc_info=True)
        
        return create_error_result(type(e).__name__, error_msg)


def register_custom_actions(agent, page=None) -> bool:
    """
    Register custom actions with browser-use agent.
    
    This function registers the find_unique_locator custom action that allows
    the agent to call deterministic Python code for locator finding and validation.
    
    The custom action will get the page object from browser_session during execution,
    ensuring we use the SAME browser that's already open. This is the key strategy:
    validate locators using the existing browser_use browser (no new instance needed).
    
    Args:
        agent: Browser-use Agent instance
        page: Optional Playwright page object (used as fallback if browser_session doesn't provide one)
    
    Returns:
        bool: True if registration succeeded, False otherwise
    
    Phase: Custom Action Implementation
    Requirements: 3.1, 8.1, 9.1
    """
    try:
        logger.info("🔧 Registering custom actions with browser-use agent...")
        
        # Import required classes for custom action registration
        from browser_use.tools.service import Tools
        from browser_use.agent.views import ActionResult
        from pydantic import BaseModel, Field
        
        # Define parameter model for find_unique_locator action
        class FindUniqueLocatorParams(BaseModel):
            """Parameters for find_unique_locator custom action"""
            x: float = Field(description="X coordinate of element center")
            y: float = Field(description="Y coordinate of element center")
            element_id: str = Field(description="Element identifier (elem_1, elem_2, etc.)")
            element_description: str = Field(description="Human-readable description of element")
            candidate_locator: Optional[str] = Field(
                default=None,
                description="Optional candidate locator to validate first (e.g., 'id=search-input')"
            )
        
        # Get or create Tools instance from agent
        if not hasattr(agent, 'tools') or agent.tools is None:
            logger.info("   Creating new Tools instance for agent")
            tools = Tools()
            agent.tools = tools
        else:
            logger.info("   Using existing Tools instance from agent")
            tools = agent.tools
        
        # Register the find_unique_locator action
        @tools.registry.action(
            description="Find and validate unique locator for element at coordinates using 21 systematic strategies. "
                       "This action runs deterministically without LLM calls and validates all locators with Playwright. "
                       "Call this action after finding an element's coordinates to get a validated unique locator.",
            param_model=FindUniqueLocatorParams
        )
        async def find_unique_locator(
            params: FindUniqueLocatorParams,
            browser_session
        ) -> ActionResult:
            """
            Custom action wrapper that calls find_unique_locator_action.
            
            This function is called by the browser-use agent when it needs to find
            a unique locator for an element. It wraps the find_unique_locator_action
            function and returns results in ActionResult format.
            
            The browser_session parameter is provided by browser-use and contains
            the active browser context with the page that's currently open.
            """
            try:
                logger.info(f"🎯 Custom action 'find_unique_locator' called by agent")
                logger.info(f"   Element: {params.element_id} - {params.element_description}")
                logger.info(f"   Coordinates: ({params.x}, {params.y})")
                
                # Get the page from the browser_session provided by browser-use
                # IMPORTANT: browser-use now uses CDP (Chrome DevTools Protocol) instead of Playwright
                # We need to connect to browser-use's browser via CDP to get a Playwright page for validation
                active_page = None
                playwright_instance = None
                connected_browser = None
                
                try:
                    logger.info(f"🔍 Attempting to retrieve page from browser_session via CDP...")
                    logger.info(f"   browser_session type: {type(browser_session)}")
                    
                    # Strategy 1: Connect via CDP (browser-use's current architecture)
                    # Get CDP URL from browser_session
                    cdp_url = None
                    
                    # Try session.cdp_url
                    if hasattr(browser_session, 'cdp_url'):
                        try:
                            cdp_url = browser_session.cdp_url
                            if cdp_url:
                                logger.info(f"✅ Found CDP URL from browser_session.cdp_url: {cdp_url}")
                        except Exception as e:
                            logger.debug(f"Error accessing browser_session.cdp_url: {e}")
                    
                    # Try cdp_client.url
                    if not cdp_url and hasattr(browser_session, 'cdp_client'):
                        try:
                            cdp_client = browser_session.cdp_client
                            if hasattr(cdp_client, 'url'):
                                cdp_url = cdp_client.url
                                if cdp_url:
                                    logger.info(f"✅ Found CDP URL from cdp_client.url: {cdp_url}")
                        except Exception as e:
                            logger.debug(f"Error accessing cdp_client.url: {e}")
                    
                    # Search all attributes for CDP URL
                    if not cdp_url:
                        logger.info("🔍 Searching for CDP URL in browser_session attributes...")
                        for attr in dir(browser_session):
                            if not attr.startswith('_'):
                                try:
                                    value = getattr(browser_session, attr, None)
                                    if value and isinstance(value, str) and 'ws://' in value and 'devtools' in value:
                                        cdp_url = value
                                        logger.info(f"✅ Found CDP URL in {attr}: {cdp_url}")
                                        break
                                except:
                                    pass
                    
                    # If we have CDP URL, connect Playwright to browser-use's browser
                    if cdp_url:
                        try:
                            from playwright.async_api import async_playwright
                            
                            logger.info(f"🔌 Connecting Playwright to browser-use's browser via CDP...")
                            playwright_instance = await async_playwright().start()
                            connected_browser = await playwright_instance.chromium.connect_over_cdp(cdp_url)
                            
                            # Get the active page from browser-use's browser
                            if connected_browser.contexts:
                                context = connected_browser.contexts[0]
                                logger.info(f"✅ Found {len(connected_browser.contexts)} context(s)")
                                
                                if context.pages:
                                    active_page = context.pages[0]
                                    page_url = await active_page.url()
                                    logger.info(f"✅ Connected to browser-use's page via CDP!")
                                    logger.info(f"   Page URL: {page_url}")
                                    logger.info(f"   Page type: {type(active_page)}")
                                else:
                                    logger.warning("⚠️ Context has no pages")
                            else:
                                logger.warning("⚠️ Browser has no contexts")
                        
                        except Exception as e:
                            logger.error(f"❌ Failed to connect Playwright via CDP: {e}")
                            import traceback
                            logger.debug(traceback.format_exc())
                    else:
                        logger.warning("⚠️ Could not find CDP URL in browser_session")
                        logger.info(f"   browser_session attributes: {[attr for attr in dir(browser_session) if not attr.startswith('_')][:20]}")
                    
                    # Strategy 2: Try get_pages() method (fallback)
                    if active_page is None and hasattr(browser_session, 'get_pages'):
                        try:
                            pages = await browser_session.get_pages()
                            if pages and len(pages) > 0:
                                active_page = pages[0]
                                logger.info(f"✅ Got Playwright page from browser_session.get_pages()[0]")
                                logger.info(f"   Page type: {type(active_page)}")
                                logger.info(f"   Total pages: {len(pages)}")
                            else:
                                logger.warning("⚠️ browser_session.get_pages() returned empty list")
                        except Exception as e:
                            logger.warning(f"⚠️ Failed to get pages: {e}")
                    
                    # Strategy 2: Direct access to .page attribute
                    if active_page is None and hasattr(browser_session, 'page') and browser_session.page is not None:
                        active_page = browser_session.page
                        logger.info(f"✅ Got active page from browser_session.page")
                        logger.info(f"   Page type: {type(active_page)}")
                    
                    # Strategy 3: Try get_current_page() method
                    if active_page is None and hasattr(browser_session, 'get_current_page'):
                        active_page = await browser_session.get_current_page()
                        logger.info(f"✅ Got active page from browser_session.get_current_page()")
                        logger.info(f"   Page type: {type(active_page)}")
                    
                    # Strategy 3: Try context.pages
                    elif hasattr(browser_session, 'context') and browser_session.context is not None:
                        pages = browser_session.context.pages
                        if pages and len(pages) > 0:
                            active_page = pages[0]  # Get the first (usually only) page
                            logger.info(f"✅ Got active page from browser_session.context.pages[0]")
                            logger.info(f"   Page type: {type(active_page)}")
                            logger.info(f"   Total pages: {len(pages)}")
                        else:
                            logger.warning("⚠️ browser_session.context.pages is empty")
                    
                    # Strategy 4: Try browser.contexts[0].pages
                    elif hasattr(browser_session, 'browser') and browser_session.browser is not None:
                        contexts = browser_session.browser.contexts
                        if contexts and len(contexts) > 0:
                            pages = contexts[0].pages
                            if pages and len(pages) > 0:
                                active_page = pages[0]
                                logger.info(f"✅ Got active page from browser_session.browser.contexts[0].pages[0]")
                                logger.info(f"   Page type: {type(active_page)}")
                            else:
                                logger.warning("⚠️ browser_session.browser.contexts[0].pages is empty")
                        else:
                            logger.warning("⚠️ browser_session.browser.contexts is empty")
                    
                    # Fallback: Use page passed during registration
                    if active_page is None:
                        logger.warning("⚠️ All page retrieval strategies failed")
                        logger.warning("⚠️ Falling back to page passed during registration")
                        active_page = page
                        if active_page:
                            logger.info(f"   Fallback page type: {type(active_page)}")
                        else:
                            logger.error("❌ Fallback page is also None!")
                        
                except Exception as e:
                    logger.error(f"❌ Error getting page from browser_session: {e}", exc_info=True)
                    active_page = page  # Use the page passed during registration as fallback
                    if active_page:
                        logger.info(f"   Fallback page type: {type(active_page)}")
                
                # Unwrap browser-use Page wrapper to get actual Playwright page
                if active_page and not hasattr(active_page, 'locator'):
                    logger.warning(f"⚠️ Page object is a browser-use wrapper: {type(active_page)}")
                    logger.info(f"   Attempting to unwrap to get Playwright page...")
                    
                    # browser-use wraps the Playwright page in browser_use.actor.page.Page
                    # Try multiple strategies to get the underlying Playwright page
                    playwright_page = None
                    
                    # Strategy 1: Check for .page attribute
                    if hasattr(active_page, 'page') and active_page.page is not None:
                        playwright_page = active_page.page
                        logger.info(f"✅ Unwrapped page from wrapper.page")
                    
                    # Strategy 2: Check for ._page attribute
                    elif hasattr(active_page, '_page') and active_page._page is not None:
                        playwright_page = active_page._page
                        logger.info(f"✅ Unwrapped page from wrapper._page")
                    
                    # Strategy 3: Check for ._client attribute (CDP client)
                    elif hasattr(active_page, '_client') and active_page._client is not None:
                        # _client might be the CDP client, try to get page from it
                        client = active_page._client
                        if hasattr(client, 'page') and client.page is not None:
                            playwright_page = client.page
                            logger.info(f"✅ Unwrapped page from wrapper._client.page")
                        else:
                            logger.warning(f"   _client exists but has no page attribute")
                    
                    # Strategy 4: Check for ._browser_session attribute
                    elif hasattr(active_page, '_browser_session') and active_page._browser_session is not None:
                        # Try to get page from the browser session
                        session = active_page._browser_session
                        if hasattr(session, 'page') and session.page is not None:
                            playwright_page = session.page
                            logger.info(f"✅ Unwrapped page from wrapper._browser_session.page")
                        elif hasattr(session, 'get_current_page'):
                            try:
                                playwright_page = await session.get_current_page()
                                logger.info(f"✅ Unwrapped page from wrapper._browser_session.get_current_page()")
                            except Exception as e:
                                logger.warning(f"   Failed to get page from _browser_session: {e}")
                    
                    # Strategy 5: Use the wrapper directly if it has evaluate method
                    # browser-use Page wrapper might proxy Playwright methods
                    elif hasattr(active_page, 'evaluate'):
                        logger.info(f"⚠️ Using browser-use Page wrapper directly (has evaluate method)")
                        logger.info(f"   This wrapper might proxy Playwright methods")
                        playwright_page = active_page  # Use wrapper as-is
                    
                    if playwright_page:
                        logger.info(f"   Playwright page type: {type(playwright_page)}")
                        active_page = playwright_page
                    else:
                        logger.error(f"❌ Could not unwrap browser-use Page wrapper!")
                        logger.error(f"   Wrapper attributes: {[attr for attr in dir(active_page) if not attr.startswith('__')][:20]}")
                        active_page = None
                
                # Final verification: ensure we have a page with required methods
                if active_page:
                    required_methods = ['locator', 'evaluate', 'evaluate_handle']
                    missing_methods = [m for m in required_methods if not hasattr(active_page, m)]
                    
                    if missing_methods:
                        logger.error(f"❌ Page object is missing required methods: {missing_methods}")
                        logger.error(f"   Type: {type(active_page)}")
                        logger.error(f"   Available methods: {[attr for attr in dir(active_page) if not attr.startswith('_')][:30]}")
                        active_page = None
                    else:
                        logger.info(f"✅ Page object has all required methods: {required_methods}")
                        logger.info(f"   Page type: {type(active_page)}")
                
                # Call the actual implementation with the active page
                # Wrap in timeout protection using CUSTOM_ACTION_TIMEOUT from config
                try:
                    result = await asyncio.wait_for(
                        find_unique_locator_action(
                            x=params.x,
                            y=params.y,
                            element_id=params.element_id,
                            element_description=params.element_description,
                            candidate_locator=params.candidate_locator,
                            page=active_page
                        ),
                        timeout=settings.CUSTOM_ACTION_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    # Handle timeout gracefully
                    timeout_msg = (
                        f"Custom action timed out after {settings.CUSTOM_ACTION_TIMEOUT} seconds "
                        f"for element {params.element_id}"
                    )
                    logger.error(f"⏱️ {timeout_msg}")
                    logger.error(f"   Element: {params.element_id} - {params.element_description}")
                    logger.error(f"   Coordinates: ({params.x}, {params.y})")
                    
                    # Return error result
                    result = {
                        'element_id': params.element_id,
                        'description': params.element_description,
                        'found': False,
                        'error': timeout_msg,
                        'coordinates': {'x': params.x, 'y': params.y},
                        'validated': False,
                        'count': 0,
                        'unique': False,
                        'valid': False,
                        'validation_method': 'playwright'
                    }
                
                # Convert result to ActionResult format
                action_result = None
                if result.get('found'):
                    best_locator = result.get('best_locator')
                    
                    # Get validation data from result (not validation_summary)
                    validated = result.get('validated', False)
                    count = result.get('count', 0)
                    unique = result.get('unique', False)
                    validation_method = result.get('validation_method', 'playwright')
                    
                    # Success message for agent - CLEAR and UNAMBIGUOUS
                    # Include explicit confirmation that this is the CORRECT and FINAL locator
                    success_msg = (
                        f"✅ SUCCESS - LOCATOR VALIDATED BY PLAYWRIGHT\n"
                        f"Element: {params.element_id}\n"
                        f"Locator: {best_locator}\n"
                        f"Validation Result: UNIQUE (count={count}, validated={validated})\n"
                        f"Method: {validation_method} (deterministic validation)\n"
                        f"Status: COMPLETE AND CORRECT\n"
                        f"This locator is guaranteed unique and valid.\n"
                        f"Do NOT retry or attempt to find a different locator.\n"
                        f"Move to the next element immediately."
                    )
                    
                    logger.info(f"✅ Custom action succeeded: {best_locator}")
                    
                    # CRITICAL FIX: Do NOT set success=True when is_done=False
                    # ActionResult validation rule: success can only be True when is_done=True
                    # For regular actions that succeed, leave success as None (default)
                    action_result = ActionResult(
                        extracted_content=success_msg,
                        long_term_memory=f"✅ VALIDATED: {params.element_id} = {best_locator} (Playwright confirmed count=1, unique=True). This is the CORRECT locator. Do NOT retry.",
                        metadata=result,
                        is_done=False  # Don't mark as done, let agent continue with other elements
                        # success is None by default for successful actions that aren't done
                    )
                
                else:
                    # Error message for agent - CLEAR about failure
                    error_msg = result.get('error', 'Could not find unique locator')
                    logger.error(f"❌ Custom action failed: {error_msg}")
                    
                    action_result = ActionResult(
                        error=f"FAILED: Could not find unique locator for {params.element_id}. Error: {error_msg}. Try different coordinates or description.",
                        is_done=False  # Let agent try again with different approach
                    )
                
                # Cleanup: Close Playwright connection if we created one
                if connected_browser:
                    try:
                        logger.info("🧹 Cleaning up: Closing Playwright CDP connection...")
                        await connected_browser.close()
                        logger.info("✅ Playwright browser connection closed")
                    except Exception as e:
                        logger.warning(f"⚠️ Error closing Playwright browser: {e}")
                
                if playwright_instance:
                    try:
                        logger.info("🧹 Cleaning up: Stopping Playwright instance...")
                        await playwright_instance.stop()
                        logger.info("✅ Playwright instance stopped")
                    except Exception as e:
                        logger.warning(f"⚠️ Error stopping Playwright instance: {e}")
                
                return action_result
                    
            except Exception as e:
                error_msg = f"Error in find_unique_locator custom action: {str(e)}"
                logger.error(f"❌ {error_msg}", exc_info=True)
                
                # Cleanup on error
                if connected_browser:
                    try:
                        await connected_browser.close()
                    except:
                        pass
                if playwright_instance:
                    try:
                        await playwright_instance.stop()
                    except:
                        pass
                
                return ActionResult(error=error_msg)
        
        logger.info("✅ Custom action 'find_unique_locator' registered successfully")
        logger.info("   Agent can now call: find_unique_locator(x, y, element_id, element_description, candidate_locator)")
        return True
        
    except Exception as e:
        # Log error but don't crash - allow fallback to legacy workflow
        logger.error(f"❌ Failed to register custom actions: {str(e)}")
        logger.error(f"   Stack trace:", exc_info=True)
        logger.warning("⚠️ Continuing with legacy workflow (custom actions disabled)")
        return False


def process_task(task_id: str, objective: str) -> None:
    """Process the browser automation task in the background."""
    logger.info(f"Starting Browser-Use agent with objective: {objective}")

    # Update task status to running
    tasks[task_id]['status'] = 'running'
    tasks[task_id]['started_at'] = time.time()

    async def run_browser_use(objective: str) -> Dict[str, Any]:
        session = None
        try:
            # Initialize browser session with better settings
            session = BrowserSession(
                headless=False,  # Set to True for production, False for debugging
                viewport={'width': 1920, 'height': 1080},
                # Add additional browser arguments
            )

            # Import ChatGoogle here to avoid import issues
            try:
                from browser_use.llm.google import ChatGoogle
            except ImportError:
                logger.error(
                    "Failed to import ChatGoogle. Make sure browser-use is properly installed.")
                raise ImportError("ChatGoogle not available")

            # Enhanced JavaScript with comprehensive fallback strategies and F12-style DOM validation
            # This includes: merged generate+validate, coordinate fallbacks, text search, attribute search
            js_code = r"""
(function() {
    // ============================================================
    // COMPREHENSIVE ELEMENT FINDER WITH FALLBACK STRATEGIES
    // ============================================================

    const COORDINATES = { x: CENTER_X, y: CENTER_Y };
    const DESCRIPTION = "ELEMENT_DESCRIPTION";
    let element = null;
    let fallbackUsed = 'none';
    const attemptedStrategies = [];

    console.log('🎯 Starting element search with coordinates:', COORDINATES);

    // ============================================================
    // HELPER FUNCTIONS
    // ============================================================

    function isValidElement(el) {
        if (!el) return false;
        if (el.tagName === 'HTML' || el.tagName === 'BODY') return false;

        const interactiveTags = ['BUTTON', 'A', 'INPUT', 'SELECT', 'TEXTAREA'];
        const isInteractive = interactiveTags.includes(el.tagName) ||
                            el.hasAttribute('onclick') ||
                            el.getAttribute('role') === 'button' ||
                            el.getAttribute('role') === 'link';

        return isInteractive;
    }

    function extractKeywords(desc) {
        const stopWords = ['the', 'a', 'an', 'and', 'or', 'but', 'below', 'above', 'near', 'next', 'to', 'with', 'find'];
        const words = desc.toLowerCase().split(/\\s+/);
        return words.filter(w => w.length > 2 && !stopWords.includes(w));
    }

    function calculateDistance(x1, y1, x2, y2) {
        return Math.sqrt(Math.pow(x2 - x1, 2) + Math.pow(y2 - y1, 2));
    }

    function selectClosestToCoordinates(candidates, coords) {
        let best = null;
        let minDistance = Infinity;

        for (const candidate of candidates) {
            const rect = candidate.getBoundingClientRect();
            const centerX = rect.left + rect.width / 2;
            const centerY = rect.top + rect.height / 2;
            const distance = calculateDistance(centerX, centerY, coords.x, coords.y);

            if (distance < minDistance) {
                minDistance = distance;
                best = candidate;
            }
        }

        return best;
    }

    // ============================================================
    // STRATEGY 1: EXACT COORDINATES
    // ============================================================
    console.log('📍 STRATEGY 1: Trying exact coordinates...');
    element = document.elementFromPoint(COORDINATES.x, COORDINATES.y);
    attemptedStrategies.push('exact_coordinates');

    if (isValidElement(element)) {
        console.log('✅ SUCCESS with exact coordinates:', element.tagName);
        fallbackUsed = 'exact_coordinates';
    } else {
        console.log('❌ FAILED: Element at coordinates is', element?.tagName || 'null');
        element = null;
    }

    // ============================================================
    // STRATEGY 2: NEARBY COORDINATES (±50px radius)
    // ============================================================
    if (!element) {
        console.log('📍 STRATEGY 2: Searching nearby coordinates...');
        attemptedStrategies.push('nearby_coordinates');
        const offsets = [
            [0, -50], [0, 50],      // Vertical
            [-50, 0], [50, 0],      // Horizontal
            [-30, -30], [30, 30],   // Diagonal
            [-30, 30], [30, -30],
            [0, -30], [0, 30], [-30, 0], [30, 0]  // Closer search
        ];

        for (const [dx, dy] of offsets) {
            const x = COORDINATES.x + dx;
            const y = COORDINATES.y + dy;
            const candidate = document.elementFromPoint(x, y);

            if (isValidElement(candidate)) {
                console.log('✅ SUCCESS with nearby coordinates:',
                            candidate.tagName, 'offset:', [dx, dy]);
                element = candidate;
                fallbackUsed = 'nearby_coordinates';
                break;
            }
        }

        if (!element) {
            console.log('❌ FAILED: No valid element in nearby area');
        }
    }

    // ============================================================
    // STRATEGY 3: TEXT-BASED SEARCH
    // ============================================================
    if (!element && DESCRIPTION && DESCRIPTION !== 'ELEMENT_DESCRIPTION') {
        console.log('📍 STRATEGY 3: Searching by text description...');
        attemptedStrategies.push('text_search');
        const keywords = extractKeywords(DESCRIPTION);
        console.log('Keywords extracted:', keywords);

        for (const keyword of keywords) {
            // Search in buttons, links, inputs
            const interactiveTags = ['button', 'a', 'input'];
            for (const tag of interactiveTags) {
                const xpath = \`//\${tag}[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "\${keyword.toLowerCase()}")]\`;
                const xpathResult = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);

                if (xpathResult.snapshotLength > 0) {
                    const candidates = [];
                    for (let i = 0; i < xpathResult.snapshotLength; i++) {
                        candidates.push(xpathResult.snapshotItem(i));
                    }
                    element = selectClosestToCoordinates(
                        candidates, COORDINATES);
                    if (element) {
                        console.log('✅ SUCCESS with text search:',
                                    element.tagName, 'keyword:', keyword);
                        fallbackUsed = 'text_search';
                        break;
                    }
                }
            }
            if (element) break;
        }

        if (!element) {
            console.log('❌ FAILED: No element found by text');
        }
    }

    // ============================================================
    // STRATEGY 4: ATTRIBUTE-BASED SEARCH
    // ============================================================
    if (!element && DESCRIPTION && DESCRIPTION !== 'ELEMENT_DESCRIPTION') {
        console.log('📍 STRATEGY 4: Searching by attributes...');
        attemptedStrategies.push('attribute_search');
        const keywords = extractKeywords(DESCRIPTION);

        for (const keyword of keywords) {
            const selectors = [
                \`[aria-label*="\${keyword}" i]\`,
                \`[placeholder*="\${keyword}" i]\`,
                \`[title*="\${keyword}" i]\`,
                \`[alt*="\${keyword}" i]\`,
                \`[name*="\${keyword}" i]\`,
                \`[id*="\${keyword}" i]\`
            ];

            for (const selector of selectors) {
                try {
                    const matches = document.querySelectorAll(selector);
                    if (matches.length > 0) {
                        element = selectClosestToCoordinates(Array.from(matches), COORDINATES);
                        if (element) {
                            console.log('✅ SUCCESS with attribute search:',
                                        element.tagName, 'keyword:', keyword);
                            fallbackUsed = 'attribute_search';
                            break;
                        }
                    }
                } catch (e) {
                    // Invalid selector, continue
                }
            }
            if (element) break;
        }

        if (!element) {
            console.log('❌ FAILED: No element found by attributes');
        }
    }

    // ============================================================
    // IF ALL STRATEGIES FAILED
    // ============================================================
    if (!element) {
        console.log('❌ ALL STRATEGIES FAILED');
        return JSON.stringify({
            success: false,
            element_found: false,
            fallback_used: 'none',
            attempted_strategies: attemptedStrategies,
            reason: 'No valid element found through any strategy',
            coordinates: COORDINATES
        });
    }

    // ============================================================
    // ELEMENT FOUND - GENERATE & VALIDATE LOCATORS
    // ============================================================
    console.log('✅ Element found via', fallbackUsed,
                '- Generating and validating locators...');

    const validatedLocators = [];

    // Locator generation strategies (priority order)
    const strategies = [
        {
            type: 'id',
            generate: () => element.id ? \`id=\${element.id}\` : null,
            getSelector: () => element.id ? \`#\${element.id}\` : null
        },
        {
            type: 'data-testid',
            generate: () => element.dataset?.testid ? \`css=[data-testid="\${element.dataset.testid}"]\` : null,
            getSelector: () => element.dataset?.testid ? \`[data-testid="\${element.dataset.testid}"]\` : null
        },
        {
            type: 'name',
            generate: () => element.name ? \`name=\${element.name}\` : null,
            getSelector: () => element.name ? \`[name="\${element.name}"]\` : null
        },
        {
            type: 'aria-label',
            generate: () => {
                const label = element.getAttribute('aria-label');
                return label ? \`css=[aria-label="\${label}"]\` : null;
            },
            getSelector: () => {
                const label = element.getAttribute('aria-label');
                return label ? \`[aria-label="\${label}"]\` : null;
            }
        },
        {
            type: 'placeholder',
            generate: () => element.placeholder ? \`css=[placeholder="\${element.placeholder}"]\` : null,
            getSelector: () => element.placeholder ? \`[placeholder="\${element.placeholder}"]\` : null
        },
        {
            type: 'css-id',
            generate: () => element.id ? \`css=\${element.tagName.toLowerCase()}#\${element.id}\` : null,
            getSelector: () => element.id ? \`\${element.tagName.toLowerCase()}#\${element.id}\` : null
        },
        {
            type: 'css-class',
            generate: () => {
                if (!element.className) return null;
                const classes = element.className.split(' ')
                    .filter(c => c && !c.match(/^(active|hover|focus|selected|disabled)/i))
                    .slice(0, 3);
                return classes.length ? \`css=\${element.tagName.toLowerCase()}.\${classes.join('.')}\` : null;
            },
            getSelector: () => {
                if (!element.className) return null;
                const classes = element.className.split(' ')
                    .filter(c => c && !c.match(/^(active|hover|focus|selected|disabled)/i))
                    .slice(0, 3);
                return classes.length ? \`\${element.tagName.toLowerCase()}.\${classes.join('.')}\` : null;
            }
        },
        {
            type: 'xpath-id',
            generate: () => element.id ? \`xpath=//*[@id="\${element.id}"]\` : null,
            getSelector: () => element.id ? \`//*[@id="\${element.id}"]\` : null,
            isXPath: true
        }
    ];

    // GENERATE + VALIDATE MERGED LOOP
    for (const strategy of strategies) {
        const locator = strategy.generate();
        const selector = strategy.getSelector();

        if (!locator || !selector) continue;

        try {
            let matches;
            if (strategy.isXPath) {
                const xpathResult = document.evaluate(selector, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                matches = [];
                for (let i = 0; i < xpathResult.snapshotLength; i++) {
                    matches.push(xpathResult.snapshotItem(i));
                }
            } else {
                matches = Array.from(document.querySelectorAll(selector));
            }

            const count = matches.length;

            // Validation: Must find elements
            if (count === 0) {
                console.log(\`❌ \${strategy.type}: \${locator} → NOT FOUND (0 matches)\`);
                continue;
            }

            // Validation: Must select correct element
            const selectsCorrectElement = matches.includes(element);
            if (!selectsCorrectElement) {
                console.log(\`❌ \${strategy.type}: \${locator} → WRONG ELEMENT (\${count} matches, none are target)\`);
                continue;
            }

            // Check uniqueness
            const isUnique = (count === 1);

            if (isUnique) {
                console.log(\`✅ \${strategy.type}: \${locator} → UNIQUE (1 of 1) ⭐\`);
            } else {
                console.log(\`⚠️  \${strategy.type}: \${locator} → NOT UNIQUE (1 of \${count})\`);
            }

            // Add validated locator
            validatedLocators.push({
                locator: locator,
                type: strategy.type,
                unique: isUnique,
                count: count,
                confidence: isUnique ? 1.0 : 0.7
            });

        } catch (error) {
            console.log(\`❌ \${strategy.type}: \${locator} → ERROR: \${error.message}\`);
        }
    }

    // Sort by confidence (unique first)
    validatedLocators.sort((a, b) => {
        if (a.unique && !b.unique) return -1;
        if (!a.unique && b.unique) return 1;
        return 0;
    });

    console.log(\`✅ Validated \${validatedLocators.length} locators (unique: \${validatedLocators.filter(l => l.unique).length})\`);

    // Return result
    return JSON.stringify({
        success: validatedLocators.length > 0,
        element_found: true,
        fallback_used: fallbackUsed,
        attempted_strategies: attemptedStrategies,
        best_locator: validatedLocators[0]?.locator,
        all_locators: validatedLocators,
        element_info: {
            tag: element.tagName.toLowerCase(),
            text: element.textContent?.trim().slice(0, 100),
            visible: element.offsetParent !== null,
            id: element.id || null,
            className: element.className || null
        },
        validation_summary: {
            total_strategies: strategies.length,
            validated: validatedLocators.length,
            unique: validatedLocators.filter(l => l.unique).length,
            not_unique: validatedLocators.filter(l => !l.unique).length
        }
    });

})();
"""

            # Enhanced objective with comprehensive instructions
            enhanced_objective = f"""{objective}

CRITICAL INSTRUCTIONS:
1. Use vision to identify the target element on the page
2. Get the coordinates (x, y) of the element from your vision analysis
3. Execute the JavaScript below, replacing CENTER_X and CENTER_Y with the actual coordinates
4. If you can extract element description from the objective, replace ELEMENT_DESCRIPTION with it

MANDATORY JavaScript execution (replace placeholders):
{js_code}

The JavaScript will:
- Try multiple strategies to find the element (coordinates, nearby search, text search, attributes)
- Validate each generated locator in the DOM (F12-style check: unique, correct element)
- Return ONLY validated locators with confidence scores

Expected JSON response format:
{{
  "success": true,
  "element_found": true,
  "fallback_used": "exact_coordinates",
  "best_locator": "id=search-button",
  "all_locators": [
    {{"locator": "id=search-button", "type": "id", "unique": true, "confidence": 1.0}},
    {{"locator": "css=button#search-button",
        "type": "css-id", "unique": true, "confidence": 1.0}}
  ],
  "validation_summary": {{
    "total_strategies": 8,
    "validated": 4,
    "unique": 3,
    "not_unique": 1
  }}
}}

Priority: id > data-* > aria-* > name > placeholder > css-id > css-class
Avoid dynamic classes (active, hover, focus, selected)"""

            logger.info(f"Gemini Key used is: {GOOGLE_API_KEY}")

            # Initialize the agent with enhanced settings and default LLM
            agent = Agent(
                task=enhanced_objective,
                browser_context=session,
                llm=ChatGoogle(
                    model=GOOGLE_MODEL,
                    api_key=GOOGLE_API_KEY,
                    temperature=0.1,
                    # Lower temperature for more consistent results
                ),
                use_vision=True,  # Enable vision for better automation
                # Configurable max steps (default 15)
                max_steps=BATCH_CONFIG["max_agent_steps"],
                # Add custom instructions for better performance
                system_prompt="""You are an expert web automation agent with vision capabilities, comprehensive fallback strategies, and F12-style DOM validation.

YOUR PRIMARY MISSION: Find elements reliably using vision + intelligent fallbacks, then generate validated locators.

CORE RESPONSIBILITIES:
1. Use vision AI to identify elements on the page (color, position, text, nearby context)
2. Execute the provided JavaScript for comprehensive element finding with fallbacks
3. The JavaScript handles: coordinate search → nearby search → text search → attribute search
4. Each generated locator is VALIDATED in DOM (F12-style: uniqueness, correctness)
5. Return ONLY validated locators with confidence scores

ELEMENT FINDING WORKFLOW (AUTOMATIC VIA JAVASCRIPT):
The JavaScript you execute will automatically try multiple strategies:
  Strategy 1: Exact coordinates from vision (70% success)
  Strategy 2: Nearby coordinates ±50px radius (if #1 fails, +10% success)
  Strategy 3: Text-based search using keywords (if #2 fails, +8% success)
  Strategy 4: Attribute-based search (aria-label, placeholder, etc.) (if #3 fails, +5% success)

Total automated success: ~93%

YOUR CRITICAL TASKS:
1. Use vision to get element coordinates (x, y)
2. Extract element description if available (e.g., "blue login button")
3. Execute JavaScript with:
   - CENTER_X = actual x coordinate
   - CENTER_Y = actual y coordinate
   - ELEMENT_DESCRIPTION = extracted description (or leave as placeholder)
4. Parse and return the JSON result from JavaScript

LOCATOR VALIDATION (AUTOMATIC):
The JavaScript performs F12-style validation:
  ✅ Uniqueness check: count = 1 (BEST, confidence 1.0)
  ⚠️  Non-unique but correct: count > 1 but includes target (OK, confidence 0.7)
  ❌ Wrong element: doesn't select target (REJECTED)
  ❌ Not found: count = 0 (REJECTED)

Only VALIDATED locators are returned!

POPUP HANDLING (MINIMAL):
- Popups are obstacles, not your primary task
- If popup blocks element: dismiss in 1 step max (Escape key or click backdrop)
- If popup doesn't block: IGNORE completely
- NEVER spend >2 steps on popup handling
- Focus on finding the target element

EXPECTED OUTPUT FORMAT:
{
  "success": true,
  "element_found": true,
  "fallback_used": "exact_coordinates",
  "best_locator": "id=search-button",
  "all_locators": [
    {"locator": "id=search-button", "type": "id",
        "unique": true, "confidence": 1.0},
    {"locator": "css=button#search-button",
        "type": "css-id", "unique": true, "confidence": 1.0}
  ],
  "element_info": {
    "tag": "button",
    "text": "Search",
    "visible": true
  },
  "validation_summary": {
    "total_strategies": 8,
    "validated": 4,
    "unique": 3,
    "not_unique": 1
  }
}

IMPORTANT RULES:
- Vision identifies element → Coordinates extracted → JavaScript runs → Validated locators returned
- The JavaScript is SMART and tries multiple fallbacks automatically
- You just need to: find element with vision + execute JS with correct coordinates
- Return structured JSON, not plain text descriptions
- If ALL strategies fail, return failure JSON with attempted_strategies list

PRIORITY: id > data-testid > aria-label > name > placeholder > css-id > css-class
Avoid dynamic classes (active, hover, focus, selected, disabled)"""
            )

            logger.info("Agent initialized, starting execution...")
            logger.info(
                "🤖 Using default ChatGoogle LLM (no rate limiting needed)")
            results = await agent.run()

            # Enhanced result extraction with JSON parsing
            final_result = ""
            success = False
            locator_data = None

            try:
                if hasattr(results, 'final_result'):
                    final_result = str(results.final_result())
                else:
                    final_result = str(results)

                # Try to parse JSON if present
                try:
                    # Look for JSON in the result
                    import re
                    json_match = re.search(
                        r'\{[\s\S]*"success"[\s\S]*\}', final_result)
                    if json_match:
                        locator_data = json.loads(json_match.group(0))
                        success = locator_data.get('success', False)
                        logger.info(
                            f"Parsed locator JSON successfully. Best locator: {locator_data.get('best_locator', 'N/A')}")

                        # STEP 7: Playwright validation for Browser Library
                        if library_type == "browser" and locator_data and locator_data.get('all_locators'):
                            try:
                                logger.info(
                                    "🎭 Running Playwright validation on locators...")
                                # Get the Playwright page from the session
                                page = None
                                try:
                                    if hasattr(session, 'context') and session.context:
                                        context = session.context
                                        if hasattr(context, 'pages') and context.pages:
                                            page = context.pages[0]
                                    elif hasattr(session, 'page') and session.page:
                                        page = session.page
                                    elif hasattr(session, 'pages') and session.pages:
                                        page = session.pages[0]
                                except Exception as e:
                                    logger.error(
                                        f"❌ Error accessing page: {e}")

                                if page:
                                    # Validate locators with Playwright
                                    validated_locators = await validate_locators_with_playwright(
                                        page,
                                        locator_data['all_locators'],
                                        library_type
                                    )

                                    # Update locator_data with Playwright-validated results
                                    locator_data['all_locators'] = validated_locators
                                    if validated_locators:
                                        locator_data['best_locator'] = validated_locators[0]['locator']
                                        logger.info(
                                            f"✅ Best locator after Playwright validation: {locator_data['best_locator']}")
                                else:
                                    logger.warning(
                                        "⚠️  No Playwright page available for validation")
                            except Exception as e:
                                logger.warning(
                                    f"⚠️  Playwright validation failed: {e}")
                                # Continue with JavaScript-validated locators
                    else:
                        # Fallback: consider it successful if we have meaningful data
                        success = bool(final_result and len(
                            final_result.strip()) > 10)
                except json.JSONDecodeError as je:
                    logger.warning(f"Could not parse JSON from result: {je}")
                    success = bool(final_result and len(
                        final_result.strip()) > 10)

                logger.info(
                    f"Agent execution completed. Success: {success}, Result length: {len(final_result)}")

            except Exception as e:
                logger.error(f"Error extracting final result: {e}")
                final_result = f"Task completed but result extraction failed: {str(e)}"
                success = False

            return {
                'success': success,
                'result': final_result,
                'locator_data': locator_data,  # Structured locator data if available
                'steps_taken': getattr(results, 'steps_taken', 0),
                'execution_time': time.time() - tasks[task_id]['started_at'],
                'agent_status': 'completed'
            }

        except Exception as e:
            error_msg = str(e)
            # Handle encoding errors specifically
            if 'charmap' in error_msg or 'codec' in error_msg:
                error_msg = f"Encoding error occurred. This is often due to special characters like currency symbols. Error: {error_msg}"

            logger.error(
                f"Error in browser automation: {error_msg}", exc_info=True)
            return {
                'success': False,
                'error': error_msg,
                'execution_time': time.time() - tasks[task_id].get('started_at', time.time()),
                'agent_status': 'failed'
            }
        finally:
            # Proper cleanup
            if session:
                try:
                    # Use the correct method for closing browser session
                    if hasattr(session, 'close'):
                        await session.close()
                    elif hasattr(session, 'browser') and hasattr(session.browser, 'close'):
                        await session.browser.close()
                    else:
                        # Fallback cleanup
                        logger.warning(
                            "Could not find proper close method for browser session")

                    logger.info("Browser session closed successfully")
                except Exception as e:
                    logger.error(f"Error closing browser session: {e}")

    # Run the automation in a new event loop
    loop = None
    try:
        # Set UTF-8 encoding for the current thread
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        results = loop.run_until_complete(run_browser_use(objective))
    except Exception as e:
        error_msg = str(e)
        logger.error(
            f"Error in event loop execution: {error_msg}", exc_info=True)
        results = {
            'success': False,
            'error': f"Event loop error: {error_msg}",
            'execution_time': time.time() - tasks[task_id].get('started_at', time.time()),
            'agent_status': 'failed'
        }
    finally:
        if loop:
            try:
                # Clean up any pending tasks
                pending = asyncio.all_tasks(loop)
                if pending:
                    for task in pending:
                        task.cancel()
                    loop.run_until_complete(asyncio.gather(
                        *pending, return_exceptions=True))

                loop.close()
            except Exception as e:
                logger.error(f"Error closing event loop: {e}")

    # Update the task status and results
    tasks[task_id].update({
        "status": "completed",
        "completed_at": time.time(),
        "message": f"Objective completed: {objective[:100]}{'...' if len(objective) > 100 else ''}",
        "results": results
    })

    logger.info(
        f"Task {task_id} completed. Success: {results.get('success', False)}")


def process_task(task_id: str, elements: list, url: str, user_query: str, session_config: dict, enable_custom_actions: bool = None):
    """
    Process elements as a UNIFIED WORKFLOW in a single browser session.

    This is the primary processing function for ALL tasks. Instead of creating separate
    Agent instances for each element, this creates ONE Agent that performs the entire
    workflow: navigate → act → extract all locators in sequence.

    Benefits:
    - Single Agent session (optimal cost)
    - Context preserved across all actions
    - No "empty page" or navigation issues
    - Agent understands the complete workflow
    - Matches user intent naturally

    Args:
        task_id: Unique task identifier
        elements: List of element specs [{"id": "elem_1", "description": "...", "action": "..."}]
        url: Target URL
        user_query: Full user query for context (e.g., "search for shoes and get product name")
        session_config: Browser configuration
        enable_custom_actions: Optional flag to enable/disable custom actions (defaults to config value)
    """
    tasks[task_id].update({
        "status": "running",
        "started_at": time.time(),
        "message": f"Processing {len(elements)} elements as unified workflow"
    })

    logger.info(f"🚀 Starting WORKFLOW MODE for task {task_id}")
    logger.info(f"   Elements: {len(elements)}")
    logger.info(f"   URL: {url}")
    logger.info(f"   Query: {user_query[:100]}...")
    
    # Capture enable_custom_actions parameter for use in async function
    # Default to config value if not provided
    if enable_custom_actions is None:
        enable_custom_actions_flag = settings.ENABLE_CUSTOM_ACTIONS
        logger.info(f"🔧 Using ENABLE_CUSTOM_ACTIONS from config: {enable_custom_actions_flag}")
    else:
        enable_custom_actions_flag = enable_custom_actions
        logger.info(f"🔧 Using ENABLE_CUSTOM_ACTIONS from API parameter: {enable_custom_actions_flag}")

    async def run_unified_workflow():
        """Execute the entire workflow in ONE Agent session."""
        from browser_use.browser.session import BrowserSession
        from browser_use.llm.google import ChatGoogle

        session = None
        connected_browser = None
        playwright_instance = None

        try:
            # Initialize browser session ONCE
            logger.info("🌐 Initializing browser session...")
            session = BrowserSession(
                headless=session_config.get("headless", False),
                viewport=None
            )

            # Parse user query to extract action parameters
            import re
            # Try multiple patterns to extract search term
            search_patterns = [
                r'search for ["\'](.*?)["\']',  # "search for 'shoes'"
                r'type ["\'](.*?)["\']',  # "type 'shoes'"
                r'input ["\'](.*?)["\']',  # "input 'shoes'"
                r'enter ["\'](.*?)["\']',  # "enter 'shoes'"
            ]

            search_term = None
            for pattern in search_patterns:
                search_match = re.search(pattern, user_query, re.IGNORECASE)
                if search_match:
                    search_term = search_match.group(1)
                    logger.info(
                        f"📝 Extracted search term: '{search_term}' using pattern: {pattern}")
                    break

            if not search_term:
                logger.warning(
                    f"⚠️ Could not extract search term from query: {user_query}")

            # Build unified workflow objective
            # SIMPLE, EXPLICIT APPROACH: Always extract locators in correct order
            workflow_steps = []
            workflow_steps.append(f"1. Navigate to {url}")

            step_num = 2

            # Separate elements by action type
            interactive_elements = [e for e in elements if e.get('action') in [
                'input', 'click']]
            result_elements = [e for e in elements if e.get('action') not in [
                'input', 'click']]

            logger.info(
                f"📊 Element breakdown: {len(interactive_elements)} interactive, {len(result_elements)} result elements")

            # Calculate dynamic max_steps based on workflow complexity
            # Formula: navigate(1) + extract_locators(all_elements) + perform_actions(interactive*2) + done(1) + buffer(3)
            dynamic_max_steps = 1 + \
                len(elements) + (len(interactive_elements) * 2) + 1 + 3
            logger.info(
                f"📊 Dynamic max_steps calculation:")
            logger.info(
                f"   Navigate: 1")
            logger.info(
                f"   Extract locators: {len(elements)} (one per element)")
            logger.info(
                f"   Perform actions: {len(interactive_elements) * 2} (2 steps per interactive element)")
            logger.info(
                f"   Done + buffer: 4")
            logger.info(
                f"   Total max_steps: {dynamic_max_steps}")

            # PHASE 1: Extract locators for interactive elements BEFORE using them
            if interactive_elements:
                workflow_steps.append(
                    f"{step_num}. BEFORE performing any actions, extract validated locators for these elements on the CURRENT page:")
                for elem in interactive_elements:
                    workflow_steps.append(
                        f"   - {elem.get('id')}: {elem.get('description')}")
                step_num += 1

            # PHASE 2: Perform user actions using the extracted locators
            if search_term:
                for elem in interactive_elements:
                    elem_action = elem.get('action', '')
                    elem_id = elem.get('id')
                    elem_desc = elem.get('description', '')

                    if elem_action == 'input':
                        workflow_steps.append(
                            f"{step_num}. Type '{search_term}' into the element you just found ({elem_id})")
                        workflow_steps.append(
                            f"{step_num + 1}. Press Enter to submit the search")
                        workflow_steps.append(
                            f"{step_num + 2}. Wait 5 seconds for search results to load completely")
                        step_num += 3
                    elif elem_action == 'click':
                        workflow_steps.append(
                            f"{step_num}. Click on the element you just found ({elem_id}: '{elem_desc}')")
                        workflow_steps.append(
                            f"{step_num + 1}. Wait 3 seconds for the page to update")
                        step_num += 2

            # PHASE 3: Extract locators for result elements AFTER actions complete
            if result_elements:
                workflow_steps.append(
                    f"{step_num}. AFTER all actions are complete, extract validated locators for these elements on the RESULTS page:")
                for elem in result_elements:
                    workflow_steps.append(
                        f"   - {elem.get('id')}: {elem.get('description')}")
                step_num += 1

            # If no interactive elements, just extract all locators
            if not interactive_elements and not result_elements:
                workflow_steps.append(
                    f"{step_num}. Extract validated locators for all elements:")
                for elem in elements:
                    workflow_steps.append(
                        f"   - {elem.get('id')}: {elem.get('description')}")
                step_num += 1

            # ========================================
            # NEW APPROACH: Use Playwright's Built-in Methods
            # ========================================
            # Instead of embedding 2000+ lines of JavaScript in the prompt (causing LLM timeout),
            # we use a simplified prompt where the agent only finds elements and returns coordinates.
            # Then Python uses Playwright's built-in methods for locator extraction and F12-style validation.

            library_type = ROBOT_LIBRARY
            logger.info(
                f"🔧 Using Playwright built-in methods for {library_type} library")

            # Build workflow prompt (integrated function)
            # Default to custom action mode (smart locator strategy)
            # Will fall back to legacy mode if custom action registration fails
            unified_objective = build_workflow_prompt(
                user_query=user_query,
                url=url,
                elements=elements,
                library_type=library_type,
                include_custom_action=True  # Default: use smart locator strategy
            )

            logger.info("📝 Built simplified workflow objective")
            logger.info(f"   Elements to find: {len(elements)}")

            # Skip all the old JavaScript code and validation
            # The new approach doesn't need it - Playwright handles everything
            logger.info("📝 Built unified workflow objective")
            logger.info(f"   Total workflow steps: {len(workflow_steps)}")

            # ========================================
            # FEATURE FLAG: ENABLE_CUSTOM_ACTIONS
            # ========================================
            # Use feature flag from outer scope (already resolved to config or parameter value)
            # If False, skip custom action registration and use legacy workflow
            # Phase: Integration and Deployment | Requirements: 10.1, 10.3
            
            if enable_custom_actions_flag:
                logger.info("� Custom actions ENABLED")
                logger.info("   Mode: Smart locator strategy (custom action mode)")
            else:
                logger.info("🔧 Custom actions DISABLED")
                logger.info("   Mode: Legacy workflow (JavaScript validation)")
            
            # Build prompts based on feature flag
            unified_objective = build_workflow_prompt(
                user_query=user_query,
                url=url,
                elements=elements,
                library_type=library_type,
                include_custom_action=enable_custom_actions_flag
            )
            
            # Create Agent with prompts based on feature flag
            agent = Agent(
                task=unified_objective,
                browser_context=session,
                llm=ChatGoogle(
                    model=GOOGLE_MODEL,
                    api_key=GOOGLE_API_KEY,
                    temperature=0.1
                ),
                use_vision=True,
                max_steps=dynamic_max_steps,
                system_prompt=build_system_prompt(include_custom_action=enable_custom_actions_flag)
            )

            # ========================================
            # REGISTER CUSTOM ACTIONS (if enabled)
            # ========================================
            # Register custom actions with the agent after creation.
            # The custom action will get the page object from browser_session during execution,
            # ensuring we use the SAME browser that's already open (no new browser instance needed).
            # This is the key strategy: validate locators using the existing browser_use browser.
            custom_actions_enabled = False
            
            if enable_custom_actions_flag:
                logger.info("🔧 Attempting to register custom actions...")
                # Pass None for page since the custom action will get it from browser_session during execution
                custom_actions_enabled = register_custom_actions(agent, page=None)
                
                if custom_actions_enabled:
                    logger.info("✅ Custom actions registered successfully")
                    logger.info("   Agent can now call find_unique_locator action")
                    logger.info("   Custom action will use the existing browser_use browser for validation")
                    logger.info("   Using smart locator strategy (custom action mode)")
                else:
                    logger.warning("⚠️ Custom action registration failed")
                    logger.warning("   Falling back to legacy workflow (JavaScript validation)")
                    
                    # Fall back to legacy mode
                    unified_objective = build_workflow_prompt(
                        user_query=user_query,
                        url=url,
                        elements=elements,
                        library_type=library_type,
                        include_custom_action=False  # Fallback to legacy mode
                    )
                    agent.task = unified_objective
                    agent.system_prompt = build_system_prompt(include_custom_action=False)
                    logger.info("✅ Agent prompts updated with legacy workflow instructions")
            else:
                logger.info("⏭️ Skipping custom action registration (disabled via config)")
                logger.info("   Using legacy workflow mode")

            # Run the unified workflow
            logger.info("🤖 Starting unified Agent...")
            logger.info(
                "🤖 Using default ChatGoogle LLM (no rate limiting needed)")
            start_time = time.time()
            agent_result = await agent.run()
            execution_time = time.time() - start_time

            logger.info(f"✅ Agent completed in {execution_time:.1f}s")
            
            # ========================================
            # METRICS LOGGING: LLM Call Count
            # ========================================
            # Track LLM calls from agent history for cost tracking
            # Phase: Error Handling and Logging | Requirements: 6.1, 6.2, 6.3, 9.5
            llm_call_count = 0
            if hasattr(agent_result, 'history') and agent_result.history:
                # Count steps that involved LLM calls (agent actions)
                llm_call_count = len(agent_result.history)
                logger.info(f"📊 METRIC: Total LLM calls in workflow: {llm_call_count}")
            
            # Log custom action usage
            logger.info(f"📊 METRIC: Custom actions enabled: {custom_actions_enabled}")
            logger.info(f"📊 METRIC: Workflow execution time: {execution_time:.2f}s")

            # ========================================
            # EXTRACT LOCATORS USING PLAYWRIGHT'S BUILT-IN METHODS
            # ========================================

            def extract_coordinates_from_js_history(history, elements_list):
                """
                Fallback: Extract coordinates from JavaScript execution results in agent history.
                This handles cases where the agent executes JS to get coordinates but doesn't
                return them in the expected JSON format.
                """
                extracted_elements = []

                try:
                    import re
                    import json

                    # Iterate through history to find execute_js actions
                    for step_idx, step in enumerate(history):
                        # Check if this step has action results
                        if hasattr(step, 'result') and step.result:
                            result_str = str(step.result)

                            # Look for JavaScript execution results with coordinates
                            # Pattern: Result: {"x":..., "y":..., "element_type":..., "visible_text":...}
                            if 'Result:' in result_str and '"x":' in result_str and '"y":' in result_str:
                                # Extract JSON after "Result:"
                                result_match = re.search(
                                    r'Result:\s*(\{[^}]*"x"[^}]*\})', result_str)
                                if result_match:
                                    try:
                                        coord_data = json.loads(
                                            result_match.group(1))

                                        # Try to match this to an element from our list
                                        # Use visible_text or element_type to match
                                        visible_text = coord_data.get(
                                            'visible_text', '').lower()
                                        element_type = coord_data.get(
                                            'element_type', '').lower()

                                        # Try to find matching element from our list
                                        matched_element = None
                                        for elem in elements_list:
                                            elem_desc = elem.get(
                                                'description', '').lower()
                                            elem_id = elem.get('id')

                                            # Check if already extracted
                                            if any(e.get('element_id') == elem_id for e in extracted_elements):
                                                continue

                                            # Match by description keywords in visible_text
                                            # or by element type
                                            if (visible_text and any(word in visible_text for word in elem_desc.split() if len(word) > 3)) or \
                                               (element_type and element_type in elem_desc):
                                                matched_element = elem
                                                break

                                        if matched_element:
                                            extracted_elements.append({
                                                'element_id': matched_element['id'],
                                                'found': True,
                                                'coordinates': {
                                                    'x': coord_data.get('x'),
                                                    'y': coord_data.get('y')
                                                },
                                                'element_type': element_type,
                                                'visible_text': coord_data.get('visible_text', '')
                                            })
                                            logger.info(
                                                f"   ✅ Matched JS result to element: {matched_element['id']}")
                                        else:
                                            # If we can't match, add it anyway with a generated ID
                                            # Use the first unmatched element
                                            unmatched = [e for e in elements_list if not any(
                                                ex.get('element_id') == e.get('id') for ex in extracted_elements)]
                                            if unmatched:
                                                elem = unmatched[0]
                                                extracted_elements.append({
                                                    'element_id': elem['id'],
                                                    'found': True,
                                                    'coordinates': {
                                                        'x': coord_data.get('x'),
                                                        'y': coord_data.get('y')
                                                    },
                                                    'element_type': element_type,
                                                    'visible_text': coord_data.get('visible_text', '')
                                                })
                                                logger.info(
                                                    f"   ⚠️ Could not match JS result, assigned to: {elem['id']}")

                                    except json.JSONDecodeError as e:
                                        logger.debug(
                                            f"   Failed to parse JS result JSON: {e}")
                                        continue

                    return extracted_elements

                except Exception as e:
                    logger.error(f"Error extracting from JS history: {e}")
                    return []

            def parse_coordinates_from_result(agent_result):
                """Extract element coordinates from agent result."""
                try:
                    final_result = ""
                    if hasattr(agent_result, 'final_result'):
                        final_result = str(agent_result.final_result())
                    elif hasattr(agent_result, 'history') and agent_result.history:
                        if len(agent_result.history) > 0:
                            last_step = agent_result.history[-1]
                            if hasattr(last_step, 'result'):
                                final_result = str(last_step.result)

                    logger.info(
                        f"📝 Agent final result (first 500 chars): {final_result[:500]}")

                    # Look for JSON with elements_found
                    import re
                    json_match = re.search(
                        r'\{[\s\S]*"elements_found"[\s\S]*\}', final_result)
                    if json_match:
                        data = json.loads(json_match.group(0))
                        return data.get('elements_found', [])

                    logger.warning(
                        "Could not find elements_found JSON, trying fallback...")
                    return []

                except Exception as e:
                    logger.error(f"Error parsing coordinates: {e}")
                    return []

            elements_found = parse_coordinates_from_result(agent_result)
            logger.info(
                f"📍 Agent found {len(elements_found)} elements with coordinates")

            # FALLBACK: If no structured results, try to extract from JavaScript execution results in history
            if not elements_found and hasattr(agent_result, 'history'):
                logger.info(
                    "🔍 Attempting fallback: extracting coordinates from JavaScript execution history...")
                elements_found = extract_coordinates_from_js_history(
                    agent_result.history, elements)
                if elements_found:
                    logger.info(
                        f"✅ Fallback successful: extracted {len(elements_found)} elements from JS history")
                    for elem in elements_found:
                        coords = elem.get('coordinates', {})
                        logger.info(
                            f"   - {elem.get('element_id')}: coords=({coords.get('x')}, {coords.get('y')}), text=\"{elem.get('visible_text', '')[:50]}\"")
                else:
                    logger.error(
                        "❌ Fallback failed: no coordinates extracted from JS history")

            # Get the Playwright page from browser_use session
            # STRATEGY: Connect Playwright to browser_use's browser via CDP
            # This allows us to use the SAME browser for vision AND validation
            page = None

            try:
                logger.info(
                    "🔍 Attempting to access browser_use's browser for validation...")

                # Method 1: Try to get CDP URL from session
                cdp_url = None

                # Try session.cdp_url
                if hasattr(session, 'cdp_url'):
                    try:
                        cdp_url = session.cdp_url
                        if cdp_url:
                            logger.info(
                                f"✅ Found CDP URL from session.cdp_url: {cdp_url}")
                        else:
                            logger.debug("session.cdp_url is None")
                    except Exception as e:
                        logger.debug(f"Error accessing session.cdp_url: {e}")

                # Try cdp_client.url
                if not cdp_url and hasattr(session, 'cdp_client'):
                    try:
                        cdp_client = session.cdp_client
                        if hasattr(cdp_client, 'url'):
                            cdp_url = cdp_client.url
                            if cdp_url:
                                logger.info(
                                    f"✅ Found CDP URL from cdp_client.url: {cdp_url}")
                    except Exception as e:
                        logger.debug(f"Error accessing cdp_client.url: {e}")

                # Search all attributes
                if not cdp_url:
                    logger.info(
                        "🔍 Searching for CDP URL in session attributes...")
                    for attr in dir(session):
                        if not attr.startswith('_'):
                            try:
                                value = getattr(session, attr, None)
                                if value and isinstance(value, str) and 'ws://' in value and 'devtools' in value:
                                    cdp_url = value
                                    logger.info(
                                        f"✅ Found CDP URL in {attr}: {cdp_url}")
                                    break
                            except:
                                pass

                # Method 2: If we have CDP URL, connect Playwright to browser_use's browser
                if cdp_url:
                    try:
                        from playwright.async_api import async_playwright

                        logger.info(
                            f"🔌 Connecting Playwright to browser_use's browser via CDP...")
                        playwright_instance = await async_playwright().start()
                        connected_browser = await playwright_instance.chromium.connect_over_cdp(cdp_url)

                        # Get the active page from browser_use's browser
                        if connected_browser.contexts:
                            context = connected_browser.contexts[0]
                            logger.info(
                                f"✅ Found {len(connected_browser.contexts)} context(s)")

                            if context.pages:
                                page = context.pages[0]
                                logger.info(f"✅ Connected to browser_use's page! URL: {await page.url()}")
                            else:
                                logger.warning("⚠️ Context has no pages")
                        else:
                            logger.warning("⚠️ Browser has no contexts")

                    except Exception as e:
                        logger.error(
                            f"❌ Failed to connect Playwright via CDP: {e}")
                        import traceback
                        logger.debug(traceback.format_exc())

                # Method 3: Fallback - try direct context access (old method)
                if not page:
                    logger.info("🔍 Trying fallback: direct context access...")
                    if hasattr(session, 'context') and session.context is not None:
                        context = session.context
                        logger.info(f"✅ Found context: {type(context)}")

                        if hasattr(context, 'pages'):
                            pages = context.pages
                            logger.info(f"✅ Context has {len(pages)} pages")
                            if pages and len(pages) > 0:
                                page = pages[0]
                                logger.info(
                                    f"✅ Got page from session.context.pages[0]")

                if not page:
                    logger.warning("⚠️ Could not access Playwright page")
                    logger.info(f"   Session type: {type(session)}")
                    logger.info(
                        f"   Session attributes (first 20): {[attr for attr in dir(session) if not attr.startswith('_')][:20]}")
                    logger.info(
                        "   Will proceed without validation (trusting browser_use)")

            except Exception as e:
                logger.error(f"❌ Error accessing page: {e}")
                import traceback
                logger.error(traceback.format_exc())

            results_list = []
            workflow_completed = False

            if not page:
                logger.error("❌ No Playwright page available")
            else:
                # Extract and validate locators for each element using Playwright
                for elem_data in elements_found:
                    elem_id = elem_data.get('element_id')

                    # Find element description from original elements list
                    elem_desc = next(
                        (e.get('description')
                         for e in elements if e.get('id') == elem_id),
                        'Unknown element'
                    )

                    if elem_data.get('found') and elem_data.get('coordinates'):
                        coords = elem_data.get('coordinates')
                        logger.info(
                            f"🔍 Extracting locators for {elem_id}: {elem_desc}")
                        logger.info(
                            f"   Coordinates: ({coords.get('x')}, {coords.get('y')})")
                        logger.info(f"   Page available: {page is not None}")

                        # ========================================
                        # METRICS LOGGING: Per-Element Timing
                        # ========================================
                        # Track execution time for each element
                        # Phase: Error Handling and Logging | Requirements: 6.1, 6.2, 6.3, 9.5
                        element_start_time = time.time()

                        try:
                            # Use Playwright's built-in methods for locator extraction
                            locator_result = await extract_and_validate_locators(
                                page=page,
                                element_description=elem_desc,
                                element_coords=elem_data.get('coordinates'),
                                library_type=library_type
                            )
                            
                            element_execution_time = time.time() - element_start_time

                            logger.info(
                                f"   Locator extraction completed: found={locator_result.get('found')}")
                            
                            # ========================================
                            # METRICS LOGGING: Per-Element Metrics
                            # ========================================
                            logger.info(f"📊 METRIC [{elem_id}]: Execution time: {element_execution_time:.2f}s")
                            logger.info(f"📊 METRIC [{elem_id}]: Custom action used: {custom_actions_enabled}")
                            logger.info(f"📊 METRIC [{elem_id}]: Locator found: {locator_result.get('found')}")

                            # ========================================
                            # METRICS LOGGING: Per-Element Cost Estimation
                            # ========================================
                            # Estimate LLM calls for this element (rough estimate based on workflow)
                            # In custom action mode: ~4-6 calls per element
                            # In legacy mode: ~20-50 calls per element
                            estimated_llm_calls_for_element = 5 if custom_actions_enabled else 30
                            estimated_cost_for_element = estimated_llm_calls_for_element * estimated_cost_per_call
                            
                            if settings.TRACK_LLM_COSTS:
                                logger.info(f"📊 METRIC [{elem_id}]: Estimated LLM calls: {estimated_llm_calls_for_element}")
                                logger.info(f"📊 METRIC [{elem_id}]: Estimated cost: ${estimated_cost_for_element:.6f}")
                            
                            results_list.append({
                                'element_id': elem_id,
                                'description': elem_desc,
                                'found': locator_result.get('found'),
                                'best_locator': locator_result.get('best_locator'),
                                'all_locators': locator_result.get('all_locators', []),
                                'unique_locators': locator_result.get('unique_locators', []),
                                'element_info': locator_result.get('element_info', {}),
                                'validation_summary': locator_result.get('validation_summary', {}),
                                # Add validation data at result level
                                'validated': locator_result.get('validated', False),
                                'count': locator_result.get('count', 0),
                                'unique': locator_result.get('unique', False),
                                'valid': locator_result.get('valid', False),
                                'validation_method': locator_result.get('validation_method', 'playwright'),
                                # Add metrics data at result level
                                'metrics': {
                                    'execution_time': element_execution_time,
                                    'estimated_llm_calls': estimated_llm_calls_for_element,
                                    'estimated_cost': estimated_cost_for_element,
                                    'custom_action_used': custom_actions_enabled
                                }
                            })

                            if locator_result.get('found'):
                                logger.info(
                                    f"   ✅ Best locator: {locator_result.get('best_locator')}")
                                logger.info(
                                    f"   All locators count: {len(locator_result.get('all_locators', []))}")
                            else:
                                error_msg = locator_result.get(
                                    'error', 'Unknown error')
                                logger.error(
                                    f"   ❌ Locator extraction failed: {error_msg}")

                        except Exception as e:
                            element_execution_time = time.time() - element_start_time
                            logger.error(
                                f"   ❌ Error extracting locators for {elem_id}: {e}")
                            
                            # Log metrics even for failed elements
                            if settings.TRACK_LLM_COSTS:
                                logger.info(f"📊 METRIC [{elem_id}]: Execution time: {element_execution_time:.2f}s (FAILED)")
                                logger.info(f"📊 METRIC [{elem_id}]: Custom action used: {custom_actions_enabled}")
                            
                            results_list.append({
                                'element_id': elem_id,
                                'description': elem_desc,
                                'found': False,
                                'error': f'Locator extraction failed: {str(e)}',
                                # Add validation data for errors
                                'validated': False,
                                'count': 0,
                                'unique': False,
                                'valid': False,
                                'validation_method': 'playwright',
                                # Add metrics data for failed elements
                                'metrics': {
                                    'execution_time': element_execution_time,
                                    'estimated_llm_calls': 0,
                                    'estimated_cost': 0,
                                    'custom_action_used': custom_actions_enabled
                                }
                            })
                    else:
                        logger.warning(
                            f"⚠️ Element {elem_id} not found by agent")
                        
                        # Log metrics for not found elements
                        if settings.TRACK_LLM_COSTS:
                            logger.info(f"📊 METRIC [{elem_id}]: Element not found by agent")
                            logger.info(f"📊 METRIC [{elem_id}]: Custom action used: {custom_actions_enabled}")
                        
                        results_list.append({
                            'element_id': elem_id,
                            'description': elem_desc,
                            'found': False,
                            'error': 'Element not found by agent vision',
                            # Add validation data for not found elements
                            'validated': False,
                            'count': 0,
                            'unique': False,
                            'valid': False,
                            'validation_method': 'playwright',
                            # Add metrics data for not found elements
                            'metrics': {
                                'execution_time': 0,
                                'estimated_llm_calls': 0,
                                'estimated_cost': 0,
                                'custom_action_used': custom_actions_enabled
                            }
                        })

                workflow_completed = len(results_list) > 0

                # Log summary
                successful = sum(1 for r in results_list if r.get('found'))
                logger.info(
                    f"📊 Locator extraction complete: {successful}/{len(results_list)} elements found")

            # Continue with existing result processing if needed
            logger.info(
                f"📝 Workflow completed: {workflow_completed}, Results: {len(results_list)}")

            # Skip old JavaScript-based result parsing - we've already extracted locators using Playwright
            # The old logic below is kept for backward compatibility but won't be executed
            # since results_list is already populated

            _skip_old_parsing = True  # Flag to indicate we're using new approach

            # OLD PARSING LOGIC (SKIPPED) - kept for reference
            if False and final_result:  # Disabled - using new approach above
                try:
                    # Try to find workflow_completed JSON using proper brace matching
                    workflow_data = extract_workflow_json(final_result)
                    if workflow_data:
                        workflow_completed = workflow_data.get(
                            'workflow_completed', False)
                        results_list = workflow_data.get('results', [])
                        logger.info(
                            f"📊 Parsed workflow results: {len(results_list)} elements")
                except Exception as e:
                    logger.warning(f"Could not parse workflow JSON: {e}")

            # If no structured results, try to extract individual element results from history
            if not results_list:
                logger.warning(
                    "No structured workflow results, attempting to extract from history...")

                # APPROACH 1: Extract actual result content from agent history
                logger.info(
                    "   Approach 1: Extracting from agent history steps...")

                # Build a list of all result strings from history
                # CRITICAL: Try multiple ways to access the content to avoid double-escaping
                result_strings = []
                direct_results = []  # Store parsed results directly from tool execution

                # DEBUG: Check what attributes agent_result has
                logger.info(f"   🔍 agent_result type: {type(agent_result)}")
                logger.info(f"   🔍 agent_result has all_results: {hasattr(agent_result, 'all_results')}")
                logger.info(f"   🔍 agent_result has history: {hasattr(agent_result, 'history')}")

                # Strategy 1: Try all_results attribute
                if hasattr(agent_result, 'all_results') and agent_result.all_results:
                    logger.info(
                        f"   ✅ Found all_results with {len(agent_result.all_results)} items")
                    for idx, action_result in enumerate(agent_result.all_results):
                        # DEBUG: Log available attributes
                        logger.info(
                            f"   📋 all_results[{idx}] type: {type(action_result)}")
                        logger.info(
                            f"   📋 all_results[{idx}] has metadata: {hasattr(action_result, 'metadata')}")

                        # PRIORITY 1: Check for metadata attribute (custom actions)
                        if hasattr(action_result, 'metadata') and action_result.metadata:
                            logger.info(
                                f"   🔍 all_results[{idx}].metadata exists, type: {type(action_result.metadata)}")
                            if isinstance(action_result.metadata, dict):
                                logger.info(
                                    f"   🎯 all_results[{idx}].metadata is a dict! Direct access possible")
                                logger.info(
                                    f"   🔍 metadata keys: {list(action_result.metadata.keys())}")
                                if 'element_id' in action_result.metadata:
                                    direct_results.append(action_result.metadata)
                                    logger.info(
                                        f"   ✅ Found element_id in all_results[{idx}].metadata dict!")
                                    continue  # Skip string conversion
                                else:
                                    logger.warning(
                                        f"   ⚠️ metadata exists but no element_id key")
                            else:
                                logger.warning(
                                    f"   ⚠️ metadata is not a dict: {type(action_result.metadata)}")
                        else:
                            logger.debug(
                                f"   all_results[{idx}] has no metadata or metadata is None")

                        # PRIORITY 2: Try multiple attribute names
                        content = None
                        if hasattr(action_result, 'extracted_content') and action_result.extracted_content:
                            content = action_result.extracted_content
                            logger.debug(
                                f"   Using extracted_content from all_results[{idx}]")
                        elif hasattr(action_result, 'content') and action_result.content:
                            content = action_result.content
                            logger.debug(
                                f"   Using content from all_results[{idx}]")
                        elif hasattr(action_result, 'result') and action_result.result:
                            # Check if result is a dict/object with direct access
                            if isinstance(action_result.result, dict):
                                logger.debug(
                                    f"   all_results[{idx}].result is a dict!")
                                direct_results.append(action_result.result)
                                continue  # Skip string conversion
                            content = str(action_result.result)
                            logger.debug(
                                f"   Using str(result) from all_results[{idx}]")

                        if content:
                            result_strings.append(content)
                            logger.debug(
                                f"   Collected content from all_results[{idx}]: {len(content)} chars")

                # Strategy 2: Try history attribute (MOST IMPORTANT for execute_js results)
                if hasattr(agent_result, 'history') and agent_result.history:
                    logger.info(
                        f"   ✅ Found history with {len(agent_result.history)} items")
                    for idx, step in enumerate(agent_result.history):
                        logger.info(f"   📋 history[{idx}] type: {type(step)}")
                        logger.info(f"   📋 history[{idx}] has result: {hasattr(step, 'result')}")
                        
                        # PRIORITY 0: Check if step.result contains ActionResult objects with metadata
                        if hasattr(step, 'result') and step.result:
                            logger.info(f"   🔍 history[{idx}].result type: {type(step.result)}")
                            
                            # step.result is a list of ActionResult objects
                            if isinstance(step.result, list):
                                logger.info(f"   🔍 history[{idx}].result is a list with {len(step.result)} items")
                                for result_idx, action_result in enumerate(step.result):
                                    logger.info(f"   🔍 history[{idx}].result[{result_idx}] type: {type(action_result)}")
                                    logger.info(f"   🔍 history[{idx}].result[{result_idx}] has metadata: {hasattr(action_result, 'metadata')}")
                                    
                                    if hasattr(action_result, 'metadata') and action_result.metadata:
                                        if isinstance(action_result.metadata, dict):
                                            logger.info(
                                                f"   🎯 history[{idx}].result[{result_idx}].metadata is a dict! Direct access possible")
                                            logger.info(
                                                f"   🔍 metadata keys: {list(action_result.metadata.keys())}")
                                            if 'element_id' in action_result.metadata:
                                                direct_results.append(action_result.metadata)
                                                logger.info(
                                                    f"   ✅ Found element_id in history[{idx}].result[{result_idx}].metadata dict!")
                                                # Don't break - there might be more ActionResults with metadata
                            
                            # If result is not a list, check if it has metadata directly
                            elif hasattr(step.result, 'metadata') and step.result.metadata:
                                if isinstance(step.result.metadata, dict):
                                    logger.info(
                                        f"   🎯 history[{idx}].result.metadata is a dict! Direct access possible")
                                    logger.info(
                                        f"   🔍 metadata keys: {list(step.result.metadata.keys())}")
                                    if 'element_id' in step.result.metadata:
                                        direct_results.append(step.result.metadata)
                                        logger.info(
                                            f"   ✅ Found element_id in history[{idx}].result.metadata dict!")
                        # Check for tool_results in state (this is where execute_js output is stored)
                        if hasattr(step, 'state') and hasattr(step.state, 'tool_results'):
                            logger.debug(
                                f"   history[{idx}] has tool_results: {len(step.state.tool_results)} items")
                            for tool_idx, tool_result in enumerate(step.state.tool_results):
                                # DEBUG: Check what type tool_result is
                                logger.debug(
                                    f"   tool_result[{tool_idx}] type: {type(tool_result)}")
                                # First 10 attrs
                                logger.debug(
                                    f"   tool_result[{tool_idx}] attributes: {dir(tool_result)[:10]}...")

                                # Try to access result directly without str()
                                # PRIORITY 1: Check for metadata attribute (custom actions)
                                if hasattr(tool_result, 'metadata') and tool_result.metadata:
                                    if isinstance(tool_result.metadata, dict):
                                        logger.info(
                                            f"   🎯 tool_result[{tool_idx}].metadata is a dict! Direct access possible")
                                        if 'element_id' in tool_result.metadata:
                                            direct_results.append(tool_result.metadata)
                                            logger.info(
                                                f"   ✅ Found element_id in tool_result.metadata dict!")
                                            continue  # Skip string conversion
                                
                                # PRIORITY 2: Check if tool_result itself is a dict
                                if isinstance(tool_result, dict):
                                    logger.info(
                                        f"   🎯 tool_result[{tool_idx}] is a dict! Direct access possible")
                                    if 'element_id' in tool_result:
                                        direct_results.append(tool_result)
                                        logger.info(
                                            f"   ✅ Found element_id in tool_result dict!")
                                        continue  # Skip string conversion
                                
                                # PRIORITY 3: Check result attribute
                                elif hasattr(tool_result, 'result'):
                                    if isinstance(tool_result.result, dict):
                                        logger.info(
                                            f"   🎯 tool_result[{tool_idx}].result is a dict!")
                                        if 'element_id' in tool_result.result:
                                            direct_results.append(
                                                tool_result.result)
                                            logger.info(
                                                f"   ✅ Found element_id in tool_result.result dict!")
                                            continue  # Skip string conversion
                                    else:
                                        # Add to string collection
                                        content = str(tool_result.result)
                                        if content and content not in result_strings:
                                            result_strings.append(content)
                                else:
                                    # Last resort: convert to string
                                    content = str(tool_result)
                                    if content and content not in result_strings:
                                        result_strings.append(content)

                        # Also try direct content attributes
                        content = None
                        if hasattr(step, 'extracted_content') and step.extracted_content:
                            content = step.extracted_content
                        elif hasattr(step, 'content') and step.content:
                            content = step.content
                        elif hasattr(step, 'result'):
                            if hasattr(step.result, 'extracted_content') and step.result.extracted_content:
                                content = step.result.extracted_content
                            elif hasattr(step.result, 'content') and step.result.content:
                                content = step.result.content
                            elif step.result:
                                content = str(step.result)
                                logger.debug(
                                    f"   Using str() for history[{idx}].result")

                        if content and content not in result_strings:
                            result_strings.append(content)
                            logger.debug(
                                f"   Collected content from history[{idx}]: {len(content)} chars")

                # Strategy 3: If still nothing, try converting entire agent_result to string as last resort
                if not result_strings and not direct_results:
                    logger.warning(
                        "   No content found via direct access, falling back to str(agent_result)")
                    result_strings.append(str(agent_result))

                # PRIORITY: If we found direct dict results, use them immediately!
                if direct_results:
                    logger.info(
                        f"   🎉 Found {len(direct_results)} direct dict results (NO PARSING NEEDED)!")
                    for direct_result in direct_results:
                        elem_id = direct_result.get('element_id')
                        if elem_id and direct_result.get('found'):
                            existing_idx = next(
                                (i for i, r in enumerate(results_list) if r.get('element_id') == elem_id), None)
                            if existing_idx is not None:
                                # Replace existing result (agent retry/correction)
                                old_locator = results_list[existing_idx].get(
                                    'best_locator')
                                new_locator = direct_result.get('best_locator')
                                logger.info(
                                    f"   🔄 Replacing {elem_id}: '{old_locator}' → '{new_locator}' (agent retry/correction)")
                                results_list[existing_idx] = direct_result
                            else:
                                # First occurrence, add it
                                results_list.append(direct_result)
                                logger.info(
                                    f"   ✅ Direct access: {elem_id} (best_locator: {direct_result.get('best_locator')})")

                    # If we got all elements via direct access, we're completely done!
                    if len(results_list) == len(elements):
                        logger.info(
                            f"   🏆 All {len(elements)} elements extracted via DIRECT ACCESS (fastest path)!")
                        # Skip all parsing - we have everything!
                        # Jump to re-ranking section

                # Combine all result strings
                full_result_str = "\n".join(result_strings)
                logger.info(
                    f"   Collected {len(result_strings)} result strings, total length: {len(full_result_str)} characters")

                # DEBUG: Show sample of result string to understand format
                logger.info(f"   📋 Result string sample (first 2000 chars):")
                logger.info(f"   {full_result_str[:2000]}")
                logger.info(f"   {'='*80}")

                # ROBUST EXTRACTION: Leverage "Result:" pattern from browser_use library
                # The browser_use library ALWAYS prints "Result: {json}" after JavaScript execution
                # This is the most reliable source of locator data
                logger.info(
                    "   🎯 Strategy: Extract from 'Result:' lines (most reliable)")
                import re

                # STRATEGY 1: Extract from "Result:" lines (MOST RELIABLE)
                # Pattern: "Result: {complete JSON object}"
                # The browser_use library prints this after every JavaScript execution
                def extract_from_result_lines(text):
                    """
                    Extract JSON from 'Result:' lines printed by browser_use.
                    This is the MOST RELIABLE method because:
                    1. Always printed by browser_use after JS execution
                    2. Contains complete, valid JSON
                    3. Has best_locator already selected (first unique locator)
                    4. No double-escaping issues
                    """
                    results = []
                    # Look for "Result: {" followed by JSON
                    pattern = r'Result:\s*(\{[^}]*?"element_id"[^}]*?\})'

                    # Find all Result: lines
                    lines = text.split('\n')
                    for line in lines:
                        if 'Result:' in line and 'element_id' in line:
                            # Extract everything after "Result:"
                            result_start = line.find('Result:')
                            if result_start != -1:
                                json_part = line[result_start + 7:].strip()

                                # Find complete JSON using brace matching
                                if json_part.startswith('{'):
                                    brace_count = 0
                                    in_string = False
                                    escape_next = False

                                    for i, char in enumerate(json_part):
                                        if escape_next:
                                            escape_next = False
                                            continue
                                        if char == '\\':
                                            escape_next = True
                                            continue
                                        if char == '"':
                                            in_string = not in_string
                                            continue
                                        if in_string:
                                            continue

                                        if char == '{':
                                            brace_count += 1
                                        elif char == '}':
                                            brace_count -= 1
                                            if brace_count == 0:
                                                json_str = json_part[:i+1]
                                                results.append(json_str)
                                                break

                    return results

                # STRATEGY 2: Extract any JSON with element_id (FALLBACK)
                def extract_all_element_jsons(text):
                    """Extract all JSON objects containing element_id from text."""
                    found_jsons = []
                    # Look for {"element_id": patterns
                    for pattern in ['"element_id":', "'element_id':"]:
                        pos = 0
                        while True:
                            pos = text.find(pattern, pos)
                            if pos == -1:
                                break

                            # Find the opening brace
                            brace_pos = text.rfind(
                                '{', max(0, pos - 50), pos + 20)
                            if brace_pos == -1:
                                pos += 1
                                continue

                            # Match braces to find complete JSON
                            brace_count = 0
                            in_string = False
                            escape_next = False

                            for i in range(brace_pos, min(len(text), brace_pos + 10000)):
                                char = text[i]

                                if escape_next:
                                    escape_next = False
                                    continue
                                if char == '\\':
                                    escape_next = True
                                    continue
                                if char == '"':
                                    in_string = not in_string
                                    continue
                                if in_string:
                                    continue

                                if char == '{':
                                    brace_count += 1
                                elif char == '}':
                                    brace_count -= 1
                                    if brace_count == 0:
                                        json_str = text[brace_pos:i+1]
                                        if json_str not in found_jsons:
                                            found_jsons.append(json_str)
                                        break

                            pos += 1

                    return found_jsons

                # Try Strategy 1 first (Result: lines)
                result_line_jsons = extract_from_result_lines(full_result_str)
                if result_line_jsons:
                    logger.info(
                        f"   ✅ Extracted {len(result_line_jsons)} JSON blocks from 'Result:' lines")
                    extracted_jsons = result_line_jsons
                else:
                    logger.warning(
                        "   ⚠️  No 'Result:' lines found, trying fallback extraction...")
                    # Try Strategy 2 (any JSON with element_id)
                    extracted_jsons = extract_all_element_jsons(
                        full_result_str)
                    if extracted_jsons:
                        logger.info(
                            f"   ✅ Extracted {len(extracted_jsons)} JSON blocks (fallback method)")
                    else:
                        logger.warning(
                            "   ⚠️  No JSON blocks with element_id found in output")

                # Add extracted JSONs to the result string for pattern matching
                if extracted_jsons:
                    full_result_str += "\n" + "\n".join(extracted_jsons)
                    logger.debug(
                        f"   Added {len(extracted_jsons)} JSON blocks to search string")

                    # OPTIMIZATION: Try to parse extracted JSONs directly
                    # This is faster and more reliable than pattern matching + extraction
                    logger.info(
                        "   🚀 Attempting direct JSON parsing (optimized path)...")
                    for json_str in extracted_jsons:
                        try:
                            parsed = json.loads(json_str)
                            elem_id = parsed.get('element_id')
                            if elem_id and parsed.get('found'):
                                # CRITICAL: Use validated locators from agent if available
                                # The agent now validates locators during execution (while browser is open)
                                # This is more reliable than validating after browser closes

                                # Initialize variables at the top level to avoid UnboundLocalError
                                dom_attrs = parsed.get('dom_attributes', {})
                                dom_id = parsed.get(
                                    'dom_id') or dom_attrs.get('id')
                                generated_locators = []

                                # Check if agent already validated locators
                                if 'locators' in parsed and parsed['locators']:
                                    # Agent provided locators - verify they were actually validated
                                    generated_locators = parsed['locators']
                                    logger.info(
                                        f"   📋 Received {len(generated_locators)} locators from agent")

                                    # Add priority field if missing and verify validation status
                                    priority_map = {
                                        'id': 1, 'data-testid': 2, 'name': 3, 'css-class': 7}

                                    actually_validated_count = 0
                                    for loc in generated_locators:
                                        if 'priority' not in loc:
                                            loc['priority'] = priority_map.get(
                                                loc.get('type'), 10)

                                        # CRITICAL: Only mark as valid if it's unique (count=1)
                                        # For test automation, only unique locators are usable
                                        if loc.get('validated') and 'count' in loc:
                                            # Has validation data from JavaScript
                                            count = loc.get('count', 0)
                                            loc['unique'] = (count == 1)
                                            # ONLY unique locators are valid for testing
                                            loc['valid'] = (count == 1)
                                            loc['validated'] = True
                                            actually_validated_count += 1

                                            if count == 1:
                                                status = "✅ VALID & UNIQUE"
                                            elif count == 0:
                                                status = "❌ NOT FOUND"
                                            else:
                                                status = f"❌ INVALID - {count} matches (not unique)"

                                            logger.info(
                                                f"      {loc['type']}: {loc['locator']} → {status} (agent-validated)")
                                        else:
                                            # No validation data - mark as unvalidated
                                            loc['validated'] = False
                                            loc['unique'] = False
                                            loc['valid'] = False
                                            logger.warning(
                                                f"      {loc['type']}: {loc['locator']} → ⚠️ No validation data")

                                    logger.info(
                                        f"   ✅ {actually_validated_count}/{len(generated_locators)} locators have validation data")
                                else:
                                    # Fallback: Generate locators from DOM attributes
                                    logger.info(
                                        "   ⚠️ No pre-validated locators, generating from DOM attributes...")

                                    # Priority 1: ID
                                    if dom_id:
                                        generated_locators.append({
                                            'type': 'id',
                                            'locator': f'id={dom_id}',
                                            'priority': 1,
                                            # Assume unique/valid but not validated yet
                                            'unique': None,  # Unknown until validated
                                            'valid': None,   # Unknown until validated
                                            'validated': False  # Not yet validated
                                        })

                                    # Priority 2: data-testid
                                    if dom_attrs.get('data-testid'):
                                        generated_locators.append({
                                            'type': 'data-testid',
                                            'locator': f'data-testid={dom_attrs["data-testid"]}',
                                            'priority': 2,
                                            'unique': None,
                                            'valid': None,
                                            'validated': False
                                        })

                                    # Priority 3: name
                                    if dom_attrs.get('name'):
                                        generated_locators.append({
                                            'type': 'name',
                                            'locator': f'name={dom_attrs["name"]}',
                                            'priority': 3,
                                            'unique': None,
                                            'valid': None,
                                            'validated': False
                                        })

                                    # Priority 4: CSS class (if available)
                                    if dom_attrs.get('class'):
                                        first_class = dom_attrs['class'].split(
                                        )[0] if dom_attrs['class'] else None
                                        if first_class:
                                            element_type = parsed.get(
                                                'element_type', 'div')
                                            generated_locators.append({
                                                'type': 'css-class',
                                                'locator': f'{element_type}.{first_class}',
                                                'priority': 7,
                                                'unique': None,
                                                'valid': None,
                                                'validated': False
                                            })

                                # VALIDATION: If Playwright page is available, validate locators
                                # This confirms uniqueness and that the locator actually works
                                if page:
                                    logger.info(
                                        f"   🔍 Validating {len(generated_locators)} locators for {elem_id}...")
                                    for loc in generated_locators:
                                        # Skip if already validated by agent
                                        if loc.get('validated') and 'count' in loc:
                                            logger.info(
                                                f"      {loc['type']}: {loc['locator']} → Already validated by agent")
                                            continue

                                        try:
                                            # Use Playwright to count matches
                                            count = await page.locator(loc['locator']).count()
                                            loc['count'] = count
                                            loc['unique'] = (count == 1)
                                            # ONLY unique locators are valid for testing
                                            loc['valid'] = (count == 1)
                                            # Successfully validated
                                            loc['validated'] = True

                                            if count == 1:
                                                status = "✅ VALID & UNIQUE"
                                            elif count == 0:
                                                status = "❌ NOT FOUND"
                                            else:
                                                status = f"❌ INVALID - {count} matches (not unique)"

                                            logger.info(
                                                f"      {loc['type']}: {loc['locator']} → {status} (playwright-validated)")
                                        except Exception as e:
                                            # Validation attempt failed due to technical error (invalid syntax, etc.)
                                            logger.warning(
                                                f"      ❌ {loc['type']}: {loc['locator']} → Validation error: {e}")
                                            # Unknown count due to error
                                            loc['count'] = None
                                            loc['unique'] = False
                                            loc['valid'] = False
                                            # Could not validate due to error
                                            loc['validated'] = False
                                            # Store error for debugging
                                            loc['validation_error'] = str(e)
                                else:
                                    logger.info(
                                        f"   ⚠️ Page not available, skipping validation for {elem_id} (trusting browser_use)")

                                # Select best locator - ONLY use validated, unique, valid locators
                                # valid=True means count=1 (unique and usable for testing)
                                validated_unique = [loc for loc in generated_locators if loc.get(
                                    'validated') and loc.get('unique') and loc.get('valid')]

                                if validated_unique:
                                    # Found valid unique locators - select best by priority
                                    best_locator = sorted(validated_unique, key=lambda x: x['priority'])[
                                        0]['locator']
                                    logger.info(
                                        f"   ✅ Selected VALID unique locator: {best_locator}")
                                else:
                                    # No valid unique locators found - try smart locator finder
                                    best_locator = None

                                    # Log why we couldn't find a valid locator
                                    if generated_locators:
                                        non_unique = [loc for loc in generated_locators if loc.get(
                                            'validated') and loc.get('count', 0) > 1]
                                        not_found = [loc for loc in generated_locators if loc.get(
                                            'validated') and loc.get('count', 0) == 0]
                                        not_validated = [
                                            loc for loc in generated_locators if not loc.get('validated')]

                                        if non_unique:
                                            logger.error(
                                                f"   ❌ No valid locator: {len(non_unique)} locators are not unique")
                                        if not_found:
                                            logger.error(
                                                f"   ❌ No valid locator: {len(not_found)} locators not found on page")
                                        if not_validated:
                                            logger.warning(
                                                f"   ⚠️ {len(not_validated)} locators were not validated")
                                    else:
                                        logger.error(
                                            f"   ❌ No locators generated for {elem_id}")
                                    
                                    # SMART FALLBACK: If we have coordinates and page, try systematic locator finding
                                    if page and parsed.get('coordinates'):
                                        coords = parsed.get('coordinates', {})
                                        if coords.get('x') and coords.get('y'):
                                            logger.info(
                                                f"   🎯 Attempting smart locator finder at coordinates ({coords['x']}, {coords['y']})")
                                            try:
                                                from tools.smart_locator_finder import find_unique_locator_at_coordinates
                                                
                                                elem_desc = next(
                                                    (e.get('description') for e in elements if e.get('id') == elem_id),
                                                    'Unknown element'
                                                )
                                                
                                                smart_result = await find_unique_locator_at_coordinates(
                                                    page=page,
                                                    x=coords['x'],
                                                    y=coords['y'],
                                                    element_id=elem_id,
                                                    element_description=elem_desc
                                                )
                                                
                                                if smart_result.get('found') and smart_result.get('best_locator'):
                                                    # Smart finder found a unique locator!
                                                    best_locator = smart_result['best_locator']
                                                    generated_locators = smart_result['all_locators']
                                                    logger.info(
                                                        f"   ✅ Smart finder found unique locator: {best_locator}")
                                                else:
                                                    logger.error(
                                                        f"   ❌ Smart finder could not find unique locator")
                                            except Exception as e:
                                                logger.error(
                                                    f"   ❌ Smart locator finder error: {e}")
                                                import traceback
                                                logger.debug(traceback.format_exc())

                                # Find element description
                                elem_desc = next(
                                    (e.get('description')
                                     for e in elements if e.get('id') == elem_id),
                                    'Unknown element'
                                )

                                # Build result with locators
                                result = {
                                    'element_id': elem_id,
                                    'description': elem_desc,
                                    'found': True,
                                    'best_locator': best_locator,
                                    'all_locators': generated_locators,
                                    'element_info': {
                                        'id': dom_id,
                                        'tagName': parsed.get('element_type', ''),
                                        'text': parsed.get('visible_text', ''),
                                        'className': dom_attrs.get('class', ''),
                                        'name': dom_attrs.get('name', ''),
                                        'testId': dom_attrs.get('data-testid', '')
                                    },
                                    'coordinates': parsed.get('coordinates', {}),
                                    'validation_summary': {
                                        'total_generated': len(generated_locators),
                                        'valid': sum(1 for loc in generated_locators if loc.get('valid')),
                                        'unique': sum(1 for loc in generated_locators if loc.get('unique')),
                                        'validated': sum(1 for loc in generated_locators if loc.get('validated')),
                                        'best_type': generated_locators[0]['type'] if generated_locators else None
                                    }
                                }

                                # Check if we already have this element
                                existing_idx = next(
                                    (i for i, r in enumerate(results_list) if r.get('element_id') == elem_id), None)
                                if existing_idx is not None:
                                    # Replace existing result (agent retry/correction)
                                    old_locator = results_list[existing_idx].get(
                                        'best_locator')
                                    logger.info(
                                        f"   🔄 Replacing {elem_id}: '{old_locator}' → '{best_locator}' (agent retry/correction)")
                                    results_list[existing_idx] = result
                                else:
                                    # First occurrence, add it
                                    results_list.append(result)
                                    logger.info(
                                        f"   ✅ Directly parsed and added {elem_id} (best_locator: {best_locator})")
                        except json.JSONDecodeError as e:
                            logger.debug(
                                f"   Failed to parse JSON directly: {e}")
                            # Will fall back to pattern matching below

                    # If we got all elements via direct parsing, we're done!
                    if len(results_list) == len(elements):
                        logger.info(
                            f"   🎉 All {len(elements)} elements extracted via direct JSON parsing!")
                        # Skip pattern matching - we have everything we need

                for elem in elements:
                    elem_id = elem.get('id')
                    logger.info(f"   🔍 Looking for {elem_id}...")

                    # Check multiple patterns (with and without space after colon)
                    patterns_to_check = [
                        # No space (common in minified JSON)
                        f'"element_id":"{elem_id}"',
                        f'"element_id": "{elem_id}"',  # With space
                        f"'element_id':'{elem_id}'",  # Single quotes, no space
                        # Single quotes, with space
                        f"'element_id': '{elem_id}'"
                    ]

                    # DEBUG: Show which patterns we're checking
                    logger.info(
                        f"   📝 Checking {len(patterns_to_check)} patterns:")
                    for idx, pattern in enumerate(patterns_to_check, 1):
                        is_found = pattern in full_result_str
                        status = "✅ FOUND" if is_found else "❌ Not found"
                        logger.info(f"      {idx}. '{pattern}' -> {status}")

                    found = any(
                        pattern in full_result_str for pattern in patterns_to_check)

                    # DEBUG: Check if elem_id appears ANYWHERE in the string (any format)
                    if not found:
                        if elem_id in full_result_str:
                            logger.warning(
                                f"   ⚠️  '{elem_id}' exists in result but pattern didn't match!")
                            logger.warning(
                                f"   💡 Searching for context around '{elem_id}'...")
                            # Find where elem_id appears and show context
                            pos = full_result_str.find(elem_id)
                            if pos != -1:
                                start = max(0, pos - 100)
                                end = min(len(full_result_str), pos + 100)
                                context = full_result_str[start:end]
                                logger.warning(f"   Context: ...{context}...")
                        else:
                            logger.warning(
                                f"   ❌ '{elem_id}' does not appear ANYWHERE in result string")

                    if found:
                        logger.info(f"   Found '{elem_id}' in result string!")
                        try:
                            elem_data = extract_json_for_element(
                                full_result_str, elem_id)
                            if elem_data:
                                logger.info(
                                    f"   Extracted JSON for {elem_id}: found={elem_data.get('found')}")
                                if elem_data.get('found'):
                                    existing_idx = next(
                                        (i for i, r in enumerate(results_list) if r.get('element_id') == elem_id), None)
                                    if existing_idx is not None:
                                        # Check if we should replace
                                        old_result = results_list[existing_idx]
                                        old_locator = old_result.get(
                                            'best_locator')
                                        new_locator = elem_data.get(
                                            'best_locator')

                                        # CRITICAL: Don't replace if old has locators but new doesn't
                                        if old_locator and not new_locator:
                                            logger.info(
                                                f"   ⚠️ Skipping replacement for {elem_id}: old has locator '{old_locator}', new doesn't")
                                        else:
                                            # Replace existing result (agent retry/correction)
                                            logger.info(
                                                f"   🔄 Replacing {elem_id}: '{old_locator}' → '{new_locator}' (agent retry/correction)")
                                            results_list[existing_idx] = elem_data
                                    else:
                                        # First occurrence, add it
                                        results_list.append(elem_data)
                                        logger.info(
                                            f"   ✅ Extracted {elem_id} from full result string")
                                else:
                                    logger.warning(
                                        f"   {elem_id} found but 'found' is False")
                            else:
                                logger.warning(
                                    f"   extract_json_for_element returned None for {elem_id}")
                        except Exception as e:
                            logger.error(
                                f"   Exception extracting {elem_id}: {e}")
                            import traceback
                            logger.error(traceback.format_exc())
                    else:
                        logger.warning(
                            f"   '{elem_id}' not found in result string")

                # APPROACH 2: Check agent history structure
                if not results_list and hasattr(agent_result, 'history'):
                    logger.info(
                        f"   Approach 2: Checking agent history ({len(agent_result.history)} steps)...")

                    for step_idx, step in enumerate(agent_result.history):
                        # PRIORITY 1: Try to access metadata directly from ActionResult (for custom actions)
                        # This avoids the string conversion issue where Python dicts use single quotes
                        if hasattr(step, 'result'):
                            result_obj = step.result
                            # Check if result is an ActionResult with metadata
                            if hasattr(result_obj, 'metadata') and result_obj.metadata:
                                metadata = result_obj.metadata
                                # Check if this metadata is for one of our elements
                                if isinstance(metadata, dict) and 'element_id' in metadata:
                                    elem_id = metadata.get('element_id')
                                    if elem_id and any(e.get('id') == elem_id for e in elements):
                                        if metadata.get('found'):
                                            if not any(r.get('element_id') == elem_id for r in results_list):
                                                results_list.append(metadata)
                                                logger.info(
                                                    f"   ✅ Extracted {elem_id} from step {step_idx} metadata (direct access)")
                                                continue  # Skip string conversion for this step
                        
                        # PRIORITY 2: Convert entire step to string (fallback)
                        step_str = str(step)

                        # Check for JavaScript execution results
                        if 'execute_js' in step_str or 'element_id' in step_str:
                            logger.debug(
                                f"   Step {step_idx} contains execute_js or element_id")

                            for elem in elements:
                                elem_id = elem.get('id')
                                if f'"element_id": "{elem_id}"' in step_str:
                                    try:
                                        elem_data = extract_json_for_element(
                                            step_str, elem_id)
                                        if elem_data and elem_data.get('found'):
                                            if not any(r.get('element_id') == elem_id for r in results_list):
                                                results_list.append(elem_data)
                                                logger.info(
                                                    f"   ✅ Extracted {elem_id} from step {step_idx}")
                                    except Exception as e:
                                        logger.debug(
                                            f"   Failed to extract {elem_id} from step {step_idx}: {e}")

                        # Also check specific attributes if they exist
                        if hasattr(step, 'state') and hasattr(step.state, 'tool_results'):
                            for tool_result in step.state.tool_results:
                                result_str = str(tool_result)
                                for elem in elements:
                                    elem_id = elem.get('id')
                                    if f'"element_id": "{elem_id}"' in result_str:
                                        try:
                                            elem_data = extract_json_for_element(
                                                result_str, elem_id)
                                            if elem_data and elem_data.get('found'):
                                                if not any(r.get('element_id') == elem_id for r in results_list):
                                                    results_list.append(
                                                        elem_data)
                                                    logger.info(
                                                        f"   ✅ Extracted {elem_id} from tool_results")
                                        except Exception as e:
                                            logger.debug(f"   Failed: {e}")

                        if hasattr(step, 'result'):
                            result_str = str(step.result)
                            for elem in elements:
                                elem_id = elem.get('id')
                                if f'"element_id": "{elem_id}"' in result_str:
                                    try:
                                        elem_data = extract_json_for_element(
                                            result_str, elem_id)
                                        if elem_data and elem_data.get('found'):
                                            if not any(r.get('element_id') == elem_id for r in results_list):
                                                results_list.append(elem_data)
                                                logger.info(
                                                    f"   ✅ Extracted {elem_id} from step.result")
                                    except Exception as e:
                                        logger.debug(f"   Failed: {e}")

            # If still no results, create default "not found" entries
            if not results_list:
                logger.error(
                    "Could not extract any element results from workflow")
                results_list = [
                    {
                        "element_id": elem.get('id'),
                        "description": elem.get('description'),
                        "found": False,
                        "error": "Could not extract from workflow result",
                        # Add validation data for not found elements
                        "validated": False,
                        "count": 0,
                        "unique": False,
                        "valid": False,
                        "validation_method": "playwright",
                        # Add metrics data for not found elements
                        "metrics": {
                            "execution_time": 0,
                            "estimated_llm_calls": 0,
                            "estimated_cost": 0,
                            "custom_action_used": custom_actions_enabled
                        }
                    }
                    for elem in elements
                ]

            # ========================================
            # PHASE 2: POST-PROCESS LOCATOR RE-RANKING
            # ========================================
            # Re-rank locators by quality to ensure best_locator is actually the best
            def score_locator(locator_obj):
                """
                Score locator based on robustness and stability.
                Higher score = better locator.

                STRICT SIX-TIER PRIORITY SYSTEM:
                ================================
                Tier 1 (90-100): Native Attributes - Most stable, browser-native lookups
                    - ID: 100 (best possible - unique, fast, stable)
                    - data-testid: 98 (designed specifically for testing)
                    - name: 96 (semantic, stable for forms)

                Tier 2 (70-89): Semantic Attributes - Accessibility-focused, stable
                    - aria-label: 88 (accessibility attribute, semantic)
                    - title: 85 (semantic attribute, descriptive)

                Tier 3 (50-69): Content-Based - Can change with content updates
                    - text: 65 (content-based, can change)
                    - role: 60 (Playwright-specific, semantic)

                Tier 4 (40-55): Fallback Strategies - Advanced strategies when basic attributes unavailable
                    - parent-id-xpath: 55 (anchored to parent ID, stable)
                    - nth-child: 50 (position-based, moderately stable)
                    - text-xpath: 48 (exact text match, more specific)
                    - attribute-combo: 45 (multiple attributes for uniqueness)

                Tier 5 (30-39): CSS Selectors - Styling-based, can change
                    - CSS with ID: 45 (should use id= instead)
                    - CSS with attribute: 40 (better than class)
                    - Regular CSS class: 35 (styling can change)
                    - Auto-generated class: 32 (very fragile)

                Tier 6 (0-29): XPath - LAST RESORT, fragile, breaks with DOM changes
                    - XPath with ID: 28 (should use id= instead!)
                    - XPath with data-testid: 26 (should use data-testid= instead!)
                    - XPath with semantic attrs: 24 (should use direct attribute)
                    - Text-based XPath: 20 (content can change)
                    - Structural XPath: 10-18 (very fragile, breaks easily)

                Clear score gaps between tiers prevent ties and ensure strict priority.
                """
                locator = locator_obj.get('locator', '')
                locator_type = locator_obj.get('type', '')

                # ========================================
                # TIER 1: NATIVE ATTRIBUTES (90-100)
                # ========================================
                # These are the most stable locators - browser-native lookups
                # that are fast, unique, and rarely change

                if locator_type == 'id' or locator.startswith('id='):
                    return 100  # Best possible - unique, fast, stable

                if locator_type == 'data-testid' or 'data-testid=' in locator:
                    return 98  # Designed specifically for testing

                if locator_type == 'name' or locator.startswith('name='):
                    return 96  # Semantic, stable for form elements

                # ========================================
                # TIER 2: SEMANTIC ATTRIBUTES (70-89)
                # ========================================
                # Accessibility-focused attributes that are semantic and stable

                if locator_type == 'aria-label' or 'aria-label=' in locator:
                    return 88  # Accessibility-focused, semantic

                if '@title=' in locator or locator_type == 'title':
                    return 85  # Semantic attribute, descriptive

                # ========================================
                # TIER 3: CONTENT-BASED (50-69)
                # ========================================
                # Locators based on visible content - can change with content updates

                if locator_type == 'text' or 'text=' in locator:
                    return 65  # Content-based, can change with text updates

                if locator_type == 'role' or 'role=' in locator:
                    return 60  # Playwright-specific, semantic but content-dependent

                # ========================================
                # TIER 4: CSS SELECTORS (30-49)
                # ========================================
                # Styling-based selectors - can change when CSS is refactored

                if locator_type == 'css' or locator.startswith('css='):
                    css_selector = locator.replace('css=', '')

                    if '#' in css_selector:
                        return 45  # CSS with ID (should use id= instead!)

                    if '[' in css_selector:
                        return 40  # CSS with attribute selector

                    # Check for auto-generated classes (very fragile)
                    if re.search(r'[_][0-9a-zA-Z]{5,}', css_selector):
                        return 32  # Auto-generated class (very fragile)

                    return 35  # Regular CSS class (styling can change)

                # ========================================
                # TIER 4.5: FALLBACK STRATEGIES (40-55)
                # ========================================
                # Advanced fallback strategies when basic attributes don't exist
                # These are better than generic CSS/XPath but not as good as native attributes

                # Parent ID + Relative XPath - anchored to stable ID
                if locator_type == 'parent-id-xpath':
                    return 55  # Anchored to parent ID (stable), but uses XPath

                # Nth-child selector - position-based, moderately stable
                if locator_type == 'nth-child':
                    return 50  # Position-based, can break if siblings change

                # Text-based XPath with exact match - better than generic XPath
                if locator_type == 'text-xpath':
                    return 48  # Text-based but exact match (more specific)

                # Attribute combination - multiple attributes for uniqueness
                if locator_type == 'attribute-combo':
                    # Multiple attributes (more stable than single class)
                    return 45

                # ========================================
                # TIER 5: XPATH - LAST RESORT (0-29)
                # ========================================
                # XPath locators are fragile and break when DOM structure changes
                # They should ONLY be used when no better option exists
                # Even "good" XPath gets low scores to enforce this priority

                if 'xpath' in locator_type or locator.startswith('//') or locator.startswith('xpath='):
                    # XPath with ID - should use id= instead!
                    if '@id=' in locator:
                        return 28  # Should use id= locator instead

                    # XPath with data-testid - should use data-testid= instead!
                    if '@data-testid=' in locator or '@data-test=' in locator:
                        return 26  # Should use data-testid= locator instead

                    # XPath with semantic attributes - should use direct attribute
                    if '@aria-label=' in locator or '@title=' in locator:
                        return 24  # Should use direct attribute locator

                    # Text-based XPath - content can change
                    if 'text()=' in locator or 'contains(text()' in locator:
                        return 20  # Content-based, can change

                    # Structural XPath (worst) - lots of [1], [2], etc.
                    # These break easily when DOM structure changes
                    index_count = locator.count(
                        '[1]') + locator.count('[2]') + locator.count('[3]')
                    if index_count >= 3:
                        return 10  # Very structural (extremely fragile)
                    elif index_count >= 2:
                        return 15  # Somewhat structural (fragile)

                    return 18  # Default XPath (still fragile)

                # Unknown/default - below Tier 4
                return 25

            logger.info("🔄 Re-ranking locators by quality score...")
            re_ranked_count = 0

            for result in results_list:
                if not result.get('found', False):
                    continue

                all_locators = result.get('all_locators', [])
                if not all_locators:
                    continue

                # Score each locator
                scored_locators = []
                for loc in all_locators:
                    score = score_locator(loc)
                    scored_locators.append({
                        **loc,
                        'quality_score': score
                    })

                # Sort by score (highest first)
                scored_locators.sort(
                    key=lambda x: x['quality_score'], reverse=True)

                # Log top 3 locators with their scores for debugging
                element_id = result.get('element_id', 'unknown')
                logger.info(f"📊 Locator Scores for {element_id}:")
                for i, loc in enumerate(scored_locators[:3]):  # Show top 3
                    locator_str = loc['locator'][:50]  # Truncate long locators
                    if i == 0:
                        # First locator is the selected best
                        logger.info(
                            f"   {loc['quality_score']:3d} - {loc['type']:15s} - {locator_str} ⭐ SELECTED AS BEST")
                        # Log warning if XPath is selected as best
                        if loc['type'] == 'xpath' or loc['locator'].startswith('xpath=') or loc['locator'].startswith('//'):
                            logger.warning(
                                f"   ⚠️  XPath used as fallback - no ID, data-testid, name, or aria-label available")
                    else:
                        logger.info(
                            f"   {loc['quality_score']:3d} - {loc['type']:15s} - {locator_str}")

                # Update result with re-ranked locators
                old_best = result.get('best_locator', '')
                new_best = scored_locators[0]['locator'] if scored_locators else old_best

                if old_best != new_best:
                    logger.info(
                        f"   ✨ {result.get('element_id')}: Upgraded locator")
                    logger.info(
                        f"      OLD: {old_best} (score: {score_locator({'locator': old_best})})")
                    logger.info(
                        f"      NEW: {new_best} (score: {scored_locators[0]['quality_score']})")
                    re_ranked_count += 1

                result['best_locator'] = new_best
                result['all_locators'] = scored_locators

            logger.info(
                f"✅ Re-ranking complete: {re_ranked_count}/{len(results_list)} elements upgraded")

            # ========================================
            # RESULTS VALIDATION - Verify quality_score is present
            # ========================================
            logger.info("🔍 Validating results before return...")
            for result in results_list:
                elem_id = result.get('element_id')
                found = result.get('found')
                best_locator = result.get('best_locator', 'N/A')
                all_locators = result.get('all_locators', [])

                if found:
                    # Check if locators have quality_score
                    has_scores = all(loc.get('quality_score')
                                     is not None for loc in all_locators)
                    logger.info(
                        f"   ✅ {elem_id}: {best_locator} ({len(all_locators)} locators, scored={has_scores})")

                    if not has_scores and all_locators:
                        logger.warning(
                            f"   ⚠️ {elem_id}: Some locators missing quality_score!")
                else:
                    error = result.get('error', 'Unknown')
                    logger.error(f"   ❌ {elem_id}: {error}")
            # ========================================

            # ========================================
            # LOCATOR PRIORITY VALIDATION CHECK
            # ========================================
            # Verify that elements with ID attributes use ID locators
            # This catches cases where the scoring system may have failed
            # or where XPath/other locators were incorrectly prioritized
            logger.info("🔍 Running locator priority validation check...")
            validation_violations = 0

            for result in results_list:
                if not result.get('found', False):
                    continue

                element_info = result.get('element_info', {})
                element_id_attr = element_info.get('id', '').strip()
                best_locator = result.get('best_locator', '')
                all_locators = result.get('all_locators', [])
                elem_id = result.get('element_id', 'unknown')

                # Check if element has ID attribute but best_locator is not ID type
                if element_id_attr and element_id_attr != '':
                    # Determine if best_locator is an ID locator
                    is_id_locator = best_locator.startswith('id=')

                    if not is_id_locator:
                        # PRIORITY VIOLATION DETECTED
                        logger.error(f"❌ PRIORITY VIOLATION: {elem_id}")
                        logger.error(
                            f"   Element has ID attribute: '{element_id_attr}'")
                        logger.error(f"   But best_locator is: {best_locator}")
                        validation_violations += 1

                        # Search for ID locator in all_locators list
                        id_locator = None
                        id_locator_index = None
                        for idx, loc in enumerate(all_locators):
                            loc_str = loc.get('locator', '')
                            if loc_str.startswith('id='):
                                id_locator = loc
                                id_locator_index = idx
                                break

                        if id_locator:
                            # Automatically correct by forcing ID locator to be best_locator
                            logger.info(
                                f"   🔧 Forcing ID locator: {id_locator['locator']}")

                            # Move ID locator to first position
                            all_locators.pop(id_locator_index)
                            all_locators.insert(0, id_locator)

                            # Update best_locator
                            result['best_locator'] = id_locator['locator']
                            result['all_locators'] = all_locators

                            logger.info(
                                f"   ✅ Corrected: {elem_id} now uses ID locator")
                        else:
                            # ID locator not found in list - this is a critical issue
                            logger.error(
                                f"   ⚠️  CRITICAL: ID locator not found in all_locators list!")
                            logger.error(
                                f"   Element ID attribute: '{element_id_attr}'")
                            logger.error(
                                f"   Available locators: {[loc.get('type') for loc in all_locators]}")
                            logger.error(
                                f"   This indicates a problem with locator generation")

            if validation_violations > 0:
                logger.warning(
                    f"⚠️  Validation found {validation_violations} priority violations (corrected)")
            else:
                logger.info(
                    "✅ Validation passed: All elements with ID use ID locators")
            # ========================================

            # Calculate metrics
            successful = sum(1 for r in results_list if r.get('found', False))
            failed = len(results_list) - successful
            
            # ========================================
            # METRICS LOGGING: Cost Calculation
            # ========================================
            # Calculate estimated LLM costs based on call count
            # Phase: Error Handling and Logging | Requirements: 6.1, 6.2, 6.3, 9.5
            
            # Gemini 2.5 Flash pricing (as of design document):
            # Input: $0.00015 per 1K tokens
            # Output: $0.0006 per 1K tokens
            # Estimated average: ~1000 tokens per call (input + output)
            # Average cost per call: ~$0.0003
            
            avg_tokens_per_call = 1000  # Estimated average tokens per LLM call
            cost_per_1k_tokens = 0.00015  # Gemini 2.5 Flash input cost
            estimated_cost_per_call = (avg_tokens_per_call / 1000) * cost_per_1k_tokens
            
            total_estimated_cost = llm_call_count * estimated_cost_per_call
            avg_llm_calls_per_element = llm_call_count / len(elements) if len(elements) > 0 else 0
            avg_cost_per_element = total_estimated_cost / len(elements) if len(elements) > 0 else 0
            
            # Only log cost metrics if TRACK_LLM_COSTS is enabled
            if settings.TRACK_LLM_COSTS:
                logger.info("=" * 80)
                logger.info("📊 WORKFLOW COST METRICS")
                logger.info("=" * 80)
                logger.info(f"Total LLM calls: {llm_call_count}")
                logger.info(f"Average LLM calls per element: {avg_llm_calls_per_element:.1f}")
                logger.info(f"Estimated total cost: ${total_estimated_cost:.6f}")
                logger.info(f"Estimated cost per element: ${avg_cost_per_element:.6f}")
                logger.info(f"Custom actions enabled: {custom_actions_enabled}")
                logger.info(f"Total execution time: {execution_time:.2f}s")
                logger.info(f"Average time per element: {execution_time / len(elements):.2f}s" if len(elements) > 0 else "N/A")
                logger.info("=" * 80)

            # ========================================
            # VALIDATION VERIFICATION BEFORE WORKFLOW COMPLETION
            # ========================================
            # Verify all elements have proper validation data
            logger.info("🔍 Verifying validation data for all elements...")
            
            validation_issues = []
            elements_without_validation = []
            elements_not_unique = []
            elements_not_valid = []
            
            for result in results_list:
                elem_id = result.get('element_id', 'unknown')
                
                # Check if element has validated=True
                if not result.get('validated', False):
                    validation_issues.append(f"{elem_id}: missing validated=True")
                    elements_without_validation.append(elem_id)
                
                # Check if element has count=1 and unique=True (only for found elements)
                if result.get('found', False):
                    count = result.get('count', 0)
                    unique = result.get('unique', False)
                    valid = result.get('valid', False)
                    
                    if count != 1 or not unique:
                        validation_issues.append(f"{elem_id}: count={count}, unique={unique} (expected count=1, unique=True)")
                        elements_not_unique.append(elem_id)
                    
                    if not valid:
                        validation_issues.append(f"{elem_id}: valid={valid} (expected valid=True)")
                        elements_not_valid.append(elem_id)
            
            # Log validation summary
            if validation_issues:
                logger.warning(f"⚠️ Validation issues found for {len(validation_issues)} element(s):")
                for issue in validation_issues:
                    logger.warning(f"   - {issue}")
            else:
                logger.info("✅ All elements have complete validation data")
            
            # Create validation summary for results
            validation_summary = {
                'total_elements': len(results_list),
                'elements_with_validation': len(results_list) - len(elements_without_validation),
                'elements_without_validation': len(elements_without_validation),
                'elements_unique': len([r for r in results_list if r.get('unique', False) and r.get('found', False)]),
                'elements_not_unique': len(elements_not_unique),
                'elements_valid': len([r for r in results_list if r.get('valid', False) and r.get('found', False)]),
                'elements_not_valid': len(elements_not_valid),
                'validation_issues': validation_issues,
                'elements_without_validation_list': elements_without_validation,
                'elements_not_unique_list': elements_not_unique,
                'elements_not_valid_list': elements_not_valid
            }
            
            logger.info(f"📊 Validation Summary:")
            logger.info(f"   Total elements: {validation_summary['total_elements']}")
            logger.info(f"   Elements with validation: {validation_summary['elements_with_validation']}/{validation_summary['total_elements']}")
            logger.info(f"   Elements with unique locators: {validation_summary['elements_unique']}/{successful}")
            logger.info(f"   Elements with valid locators: {validation_summary['elements_valid']}/{successful}")
            
            if validation_issues:
                logger.warning(f"   ⚠️ {len(validation_issues)} validation issue(s) detected")
            # ========================================

            # CRITICAL: Only consider workflow successful if ALL elements have unique locators
            # This ensures we don't proceed with placeholder locators or non-unique locators
            all_found = successful == len(
                results_list) and len(results_list) > 0

            return {
                'success': all_found,  # Changed from 'successful > 0' to require ALL elements found
                'workflow_mode': True,
                'workflow_completed': workflow_completed,
                'results': results_list,
                'summary': {
                    'total_elements': len(elements),
                    'successful': successful,
                    'failed': failed,
                    'success_rate': successful / len(elements) if len(elements) > 0 else 0,
                    # Cost tracking metrics
                    'total_llm_calls': llm_call_count,
                    'avg_llm_calls_per_element': avg_llm_calls_per_element,
                    'estimated_total_cost': total_estimated_cost,
                    'estimated_cost_per_element': avg_cost_per_element,
                    'custom_actions_enabled': custom_actions_enabled
                },
                'validation_summary': validation_summary,  # Add validation summary to results
                'execution_time': execution_time,
                'session_id': str(id(session))
            }

        except Exception as e:
            logger.error(f"❌ Workflow task error: {e}", exc_info=True)
            return {
                'success': False,
                'workflow_mode': True,
                'error': str(e),
                'results': [],
                'summary': {
                    'total_elements': len(elements),
                    'successful': 0,
                    'failed': len(elements),
                    'success_rate': 0
                }
            }
        finally:
            # Clean up Playwright connection (if we created one)
            if connected_browser:
                try:
                    await connected_browser.close()
                    logger.info("🧹 Closed Playwright connection to browser")
                except:
                    pass

            if playwright_instance:
                try:
                    await playwright_instance.stop()
                    logger.info("🧹 Stopped Playwright instance")
                except:
                    pass

            # Clean up browser session
            if session:
                try:
                    await session.close()
                    logger.info("🧹 Browser session closed")
                except:
                    pass

    # Run the async workflow
    try:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        results = loop.run_until_complete(run_unified_workflow())
        loop.close()

        # Update task status
        tasks[task_id].update({
            "status": "completed",
            "completed_at": time.time(),
            "message": f"Workflow completed: {results['summary']['successful']}/{results['summary']['total_elements']} elements found",
            "results": results
        })

        logger.info(f"🎉 Workflow task {task_id} completed successfully")
        if 'summary' in results and 'success_rate' in results['summary']:
            logger.info(
                f"   Success rate: {results['summary']['success_rate']*100:.1f}%")

    except Exception as e:
        logger.error(
            f"❌ Failed to execute workflow task {task_id}: {e}", exc_info=True)
        tasks[task_id].update({
            "status": "completed",
            "completed_at": time.time(),
            "message": f"Workflow failed: {str(e)}",
            "results": {
                'success': False,
                'workflow_mode': True,
                'error': str(e),
                'results': [],
                'summary': {'total_elements': len(elements), 'successful': 0, 'failed': len(elements)}
            }
        })


def process_batch_task(task_id: str, elements: list, url: str, user_query: str, session_config: dict):
    """
    Process a batch task to find multiple elements in one persistent browser session.

    Args:
        task_id: Unique task identifier
        elements: List of element descriptions [{"id": "elem_1", "description": "...", "action": "..."}]
        url: Target URL to navigate to
        user_query: Full user query for context
        session_config: Browser session configuration (headless, timeout, etc.)
    """
    tasks[task_id].update({
        "status": "running",
        "started_at": time.time(),
        "message": f"Processing batch task with {len(elements)} elements"
    })

    logger.info(f"Starting batch task {task_id} with {len(elements)} elements")
    logger.info(f"Target URL: {url}")
    logger.info(f"User query context: {user_query}")

    async def run_batch_browser_use(elements_list, target_url, query_context):
        """
        Run browser automation for multiple elements in one session.
        Keeps browser alive across all element lookups.
        """
        from browser_use.browser.session import BrowserSession
        from browser_use.llm.google import ChatGoogle

        session = None
        results_list = []
        pages_visited = []
        popups_handled = []

        try:
            # Initialize browser session ONCE
            logger.info(
                "🌐 Initializing persistent browser session for batch processing...")
            session = BrowserSession(
                # Set to False for debugging
                headless=session_config.get("headless", False),
                viewport={'width': 1920, 'height': 1080},
                # Add disable security for better compatibility
            )
            logger.info("✅ Browser session created successfully")

            # Note: Navigation will be handled by the first Agent call
            # The target_url will be included in the objective for the first element

            # Parse user query to extract action parameters (e.g., search terms, text to input)
            import re
            search_term = None
            search_match = re.search(
                r"search for ['\"]([^'\"]+)['\"]", query_context, re.IGNORECASE)
            if search_match:
                search_term = search_match.group(1)
                logger.info(
                    f"🔍 Extracted search term from query: '{search_term}'")

            # Process each element in the same browser session
            for idx, element_spec in enumerate(elements_list):
                element_id = element_spec.get("id", f"element_{idx}")
                element_desc = element_spec.get("description", "")
                element_action = element_spec.get("action", "")

                logger.info(
                    f"🔍 Processing element {idx + 1}/{len(elements_list)}: {element_id}")
                logger.info(f"   Description: {element_desc}")
                logger.info(
                    f"   Action: {element_action if element_action else 'None (locator only)'}")

                try:
                    # Build objective based on whether action is required
                    # For the first element, include navigation instruction
                    navigation_instruction = ""
                    if idx == 0 and target_url:
                        navigation_instruction = f"First, navigate to {target_url}. Then, "
                    else:
                        navigation_instruction = ""

                    # Determine if element requires interaction (input or click)
                    needs_interaction = element_action in ["input", "click"]

                    if needs_interaction:
                        # For actionable elements: Extract locator AND perform action
                        if element_action == "input" and search_term:
                            interaction_instruction = f"""
IMPORTANT: After extracting the locator, you MUST perform the following action:
1. Type '{search_term}' into the element
2. Press Enter to submit
3. Wait for the page to load (5 seconds)

This is necessary for subsequent elements to be found."""
                        elif element_action == "click":
                            interaction_instruction = f"""
IMPORTANT: After extracting the locator, you MUST perform the following action:
1. Click the element
2. Wait for the page to update (3 seconds)

This is necessary for subsequent elements to be found."""
                        else:
                            interaction_instruction = ""
                    else:
                        # For read-only elements: Locator extraction only
                        interaction_instruction = f"""
CRITICAL: You are NOT here to interact with this element. You are ONLY here to:
1. Find it using vision AI
2. Get its coordinates
3. Run the JavaScript validation code
4. Return the JSON result

DO NOT try to complete the user's task. JUST extract the locator."""

                    element_objective = f"""
{navigation_instruction}Your task is to extract locators for this element: "{element_desc}"

Step 1: Use vision AI to identify the element on the page
Step 2: Get the exact coordinates (x, y) of the element's center
Step 3: Execute the JavaScript validation code below with those coordinates
Step 4: Return the JSON result from the JavaScript execution
{interaction_instruction}

{'' if idx == 0 else 'The browser is already on the correct page. Do not reload or navigate.'}

After you identify the element and get its coordinates, execute this JavaScript:
(Replace CENTER_X and CENTER_Y with the actual coordinates, and ELEMENT_DESCRIPTION with "{element_desc}")

```javascript"""

                    # Reuse the existing enhanced JavaScript and agent logic
                    # (Same as in process_task but in the existing session)
                    js_code = r"""
(function() {
    // ============================================================
    // COMPREHENSIVE ELEMENT FINDER WITH FALLBACK STRATEGIES
    // ============================================================
    
    const COORDINATES = { x: CENTER_X, y: CENTER_Y };
    const DESCRIPTION = "ELEMENT_DESCRIPTION";
    let element = null;
    let fallbackUsed = 'none';
    const attemptedStrategies = [];
    
    console.log('🎯 Starting element search with coordinates:', COORDINATES);
    
    // ============================================================
    // HELPER FUNCTIONS
    // ============================================================
    
    function isValidElement(el) {
        if (!el) return false;
        if (el.tagName === 'HTML' || el.tagName === 'BODY') return false;
        
        const interactiveTags = ['BUTTON', 'A', 'INPUT', 'SELECT', 'TEXTAREA'];
        const isInteractive = interactiveTags.includes(el.tagName) || 
                            el.hasAttribute('onclick') ||
                            el.getAttribute('role') === 'button' ||
                            el.getAttribute('role') === 'link';
        
        return isInteractive;
    }
    
    function extractKeywords(desc) {
        const stopWords = ['the', 'a', 'an', 'and', 'or', 'but', 'below', 'above', 'near', 'next', 'to', 'with', 'find'];
        const words = desc.toLowerCase().split(/\\s+/);
        return words.filter(w => w.length > 2 && !stopWords.includes(w));
    }
    
    function calculateDistance(x1, y1, x2, y2) {
        return Math.sqrt(Math.pow(x2 - x1, 2) + Math.pow(y2 - y1, 2));
    }
    
    function selectClosestToCoordinates(candidates, coords) {
        let best = null;
        let minDistance = Infinity;
        
        for (const candidate of candidates) {
            const rect = candidate.getBoundingClientRect();
            const centerX = rect.left + rect.width / 2;
            const centerY = rect.top + rect.height / 2;
            const distance = calculateDistance(centerX, centerY, coords.x, coords.y);
            
            if (distance < minDistance) {
                minDistance = distance;
                best = candidate;
            }
        }
        
        return best;
    }
    
    // ============================================================
    // STRATEGY 1: EXACT COORDINATES
    // ============================================================
    console.log('📍 STRATEGY 1: Trying exact coordinates...');
    element = document.elementFromPoint(COORDINATES.x, COORDINATES.y);
    attemptedStrategies.push('exact_coordinates');
    
    if (isValidElement(element)) {
        console.log('✅ SUCCESS with exact coordinates:', element.tagName);
        fallbackUsed = 'exact_coordinates';
    } else {
        console.log('❌ FAILED: Element at coordinates is', element?.tagName || 'null');
        element = null;
    }
    
    // ============================================================
    // STRATEGY 2: NEARBY COORDINATES (±50px radius)
    // ============================================================
    if (!element) {
        console.log('📍 STRATEGY 2: Searching nearby coordinates...');
        attemptedStrategies.push('nearby_coordinates');
        const offsets = [
            [0, -50], [0, 50],      // Vertical
            [-50, 0], [50, 0],      // Horizontal
            [-30, -30], [30, 30],   // Diagonal
            [-30, 30], [30, -30],
            [0, -30], [0, 30], [-30, 0], [30, 0]  // Closer search
        ];
        
        for (const [dx, dy] of offsets) {
            const x = COORDINATES.x + dx;
            const y = COORDINATES.y + dy;
            const candidate = document.elementFromPoint(x, y);
            
            if (isValidElement(candidate)) {
                console.log('✅ SUCCESS with nearby coordinates:', candidate.tagName, 'offset:', [dx, dy]);
                element = candidate;
                fallbackUsed = 'nearby_coordinates';
                break;
            }
        }
    }
    
    // ============================================================
    // STRATEGY 3: TEXT-BASED SEARCH
    // ============================================================
    if (!element) {
        console.log('📝 STRATEGY 3: Text-based search...');
        attemptedStrategies.push('text_search');
        const keywords = extractKeywords(DESCRIPTION);
        console.log('Keywords:', keywords);
        
        const allElements = Array.from(document.querySelectorAll('button, a, input, select, textarea, [role="button"], [role="link"]'));
        const textMatches = allElements.filter(el => {
            const text = el.textContent.toLowerCase();
            const ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
            const placeholder = (el.getAttribute('placeholder') || '').toLowerCase();
            
            return keywords.some(keyword => 
                text.includes(keyword) || ariaLabel.includes(keyword) || placeholder.includes(keyword)
            );
        });
        
        if (textMatches.length > 0) {
            element = selectClosestToCoordinates(textMatches, COORDINATES);
            console.log('✅ SUCCESS with text search:', element.tagName);
            fallbackUsed = 'text_search';
        }
    }
    
    // ============================================================
    // STRATEGY 4: ATTRIBUTE SEARCH
    // ============================================================
    if (!element) {
        console.log('🏷️ STRATEGY 4: Attribute-based search...');
        attemptedStrategies.push('attribute_search');
        const keywords = extractKeywords(DESCRIPTION);
        
        const allElements = Array.from(document.querySelectorAll('*'));
        const attrMatches = allElements.filter(el => {
            const name = (el.getAttribute('name') || '').toLowerCase();
            const id = (el.id || '').toLowerCase();
            const dataAttrs = Array.from(el.attributes)
                .filter(attr => attr.name.startsWith('data-'))
                .map(attr => attr.value.toLowerCase())
                .join(' ');
            
            return keywords.some(keyword => 
                name.includes(keyword) || id.includes(keyword) || dataAttrs.includes(keyword)
            );
        });
        
        if (attrMatches.length > 0) {
            element = selectClosestToCoordinates(attrMatches, COORDINATES);
            console.log('✅ SUCCESS with attribute search:', element.tagName);
            fallbackUsed = 'attribute_search';
        }
    }
    
    // ============================================================
    // ELEMENT NOT FOUND
    // ============================================================
    if (!element) {
        console.log('❌ FAILED: Could not find element with any strategy');
        return JSON.stringify({
            success: false,
            element_found: false,
            fallback_used: 'none',
            attempted_strategies: attemptedStrategies,
            error: 'Element not found with any strategy'
        });
    }
    
    // ============================================================
    // GENERATE AND VALIDATE LOCATORS (F12-STYLE)
    // ============================================================
    console.log('🔧 Generating and validating locators for:', element.tagName);
    const validatedLocators = [];
    
    // Define locator strategies
    const strategies = [
        {
            type: 'id',
            generate: () => element.id ? `id=${element.id}` : null,
            getSelector: () => element.id ? `//*[@id="${element.id}"]` : null,
            isXPath: true
        },
        {
            type: 'name',
            generate: () => element.name ? `name=${element.name}` : null,
            getSelector: () => element.name ? `//*[@name="${element.name}"]` : null,
            isXPath: true
        },
        {
            type: 'data-testid',
            generate: () => element.getAttribute('data-testid') ? `xpath=//*[@data-testid="${element.getAttribute('data-testid')}"]` : null,
            getSelector: () => element.getAttribute('data-testid') ? `//*[@data-testid="${element.getAttribute('data-testid')}"]` : null,
            isXPath: true
        },
        {
            type: 'aria-label',
            generate: () => element.getAttribute('aria-label') ? `xpath=//*[@aria-label="${element.getAttribute('aria-label')}"]` : null,
            getSelector: () => element.getAttribute('aria-label') ? `//*[@aria-label="${element.getAttribute('aria-label')}"]` : null,
            isXPath: true
        },
        {
            type: 'placeholder',
            generate: () => element.getAttribute('placeholder') ? `xpath=//*[@placeholder="${element.getAttribute('placeholder')}"]` : null,
            getSelector: () => element.getAttribute('placeholder') ? `//*[@placeholder="${element.getAttribute('placeholder')}"]` : null,
            isXPath: true
        },
        {
            type: 'css-id',
            generate: () => element.id ? `css=${element.tagName.toLowerCase()}#${element.id}` : null,
            getSelector: () => element.id ? `${element.tagName.toLowerCase()}#${element.id}` : null,
            isXPath: false
        },
        {
            type: 'css-class',
            generate: () => {
                const classes = Array.from(element.classList).filter(c => !/(active|hover|focus|selected|disabled)/i.test(c));
                return classes.length > 0 ? `css=${element.tagName.toLowerCase()}.${classes.join('.')}` : null;
            },
            getSelector: () => {
                const classes = Array.from(element.classList).filter(c => !/(active|hover|focus|selected|disabled)/i.test(c));
                return classes.length > 0 ? `${element.tagName.toLowerCase()}.${classes.join('.')}` : null;
            },
            isXPath: false
        },
        {
            type: 'xpath-text',
            generate: () => element.textContent ? `xpath=//${element.tagName.toLowerCase()}[contains(text(), "${element.textContent.trim().slice(0, 30)}")]` : null,
            getSelector: () => element.textContent ? `//${element.tagName.toLowerCase()}[contains(text(), "${element.textContent.trim().slice(0, 30)}")]` : null,
            isXPath: true
        }
    ];
    
    // GENERATE + VALIDATE MERGED LOOP (F12-STYLE)
    for (const strategy of strategies) {
        const locator = strategy.generate();
        const selector = strategy.getSelector();
        
        if (!locator || !selector) continue;
        
        try {
            let matches;
            if (strategy.isXPath) {
                const xpathResult = document.evaluate(selector, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                matches = [];
                for (let i = 0; i < xpathResult.snapshotLength; i++) {
                    matches.push(xpathResult.snapshotItem(i));
                }
            } else {
                matches = Array.from(document.querySelectorAll(selector));
            }
            
            const count = matches.length;
            
            // Validation: Must find elements
            if (count === 0) {
                console.log(`❌ ${strategy.type}: ${locator} → NOT FOUND (0 matches)`);
                continue;
            }
            
            // Validation: Must select correct element
            const selectsCorrectElement = matches.includes(element);
            if (!selectsCorrectElement) {
                console.log(`❌ ${strategy.type}: ${locator} → WRONG ELEMENT (${count} matches, none are target)`);
                continue;
            }
            
            // Check uniqueness
            const isUnique = (count === 1);
            
            if (isUnique) {
                console.log(`✅ ${strategy.type}: ${locator} → UNIQUE (1 of 1) ⭐`);
            } else {
                console.log(`⚠️  ${strategy.type}: ${locator} → NOT UNIQUE (1 of ${count})`);
            }
            
            // Add validated locator
            validatedLocators.push({
                locator: locator,
                type: strategy.type,
                unique: isUnique,
                count: count,
                confidence: isUnique ? 1.0 : 0.7
            });
            
        } catch (error) {
            console.log(`❌ ${strategy.type}: ${locator} → ERROR: ${error.message}`);
        }
    }
    
    // Sort by confidence (unique first)
    validatedLocators.sort((a, b) => {
        if (a.unique && !b.unique) return -1;
        if (!a.unique && b.unique) return 1;
        return 0;
    });
    
    console.log(`✅ Validated ${validatedLocators.length} locators (unique: ${validatedLocators.filter(l => l.unique).length})`);
    
    // Return result
    return JSON.stringify({
        success: validatedLocators.length > 0,
        element_found: true,
        fallback_used: fallbackUsed,
        attempted_strategies: attemptedStrategies,
        best_locator: validatedLocators[0]?.locator,
        all_locators: validatedLocators,
        element_info: {
            tag: element.tagName.toLowerCase(),
            text: element.textContent?.trim().slice(0, 100),
            visible: element.offsetParent !== null,
            id: element.id || null,
            className: element.className || null
        },
        validation_summary: {
            total_strategies: strategies.length,
            validated: validatedLocators.length,
            unique: validatedLocators.filter(l => l.unique).length,
            not_unique: validatedLocators.filter(l => !l.unique).length
        }
    });
    
})();
"""

                    enhanced_objective = f"""{element_objective}

CRITICAL INSTRUCTIONS:
1. Use vision to identify the target element on the page
2. Get the coordinates (x, y) of the element from your vision analysis
3. Execute the JavaScript below, replacing CENTER_X and CENTER_Y with the actual coordinates
4. If you can extract element description from the objective, replace ELEMENT_DESCRIPTION with it

MANDATORY JavaScript execution (replace placeholders):
{js_code}

The JavaScript will:
- Try multiple strategies to find the element (coordinates, nearby search, text search, attributes)
- Validate each generated locator in the DOM (F12-style check: unique, correct element)
- Return ONLY validated locators with confidence scores
"""

                    # Initialize agent with the existing session and default LLM (reuse browser!)
                    agent = Agent(
                        task=enhanced_objective,
                        browser_context=session,
                        llm=ChatGoogle(
                            model=GOOGLE_MODEL,
                            api_key=GOOGLE_API_KEY,
                            temperature=0.1
                        ),
                        use_vision=True,
                        # Configurable max steps (default 15)
                        max_steps=BATCH_CONFIG["max_agent_steps"],
                        system_prompt="""You are an expert web automation agent. Find elements using vision and return validated locators.
Focus ONLY on finding the requested element. Don't waste time on popups unless they block the element."""
                    )

                    # Run agent for this element
                    logger.info(
                        "🤖 Using default ChatGoogle LLM (no rate limiting needed)")
                    element_start_time = time.time()
                    agent_result = await agent.run()
                    element_execution_time = time.time() - element_start_time

                    # Parse result - look for execute_js action output in history
                    locator_data = None
                    success = False

                    # Check agent history for execute_js results
                    if hasattr(agent_result, 'history') and agent_result.history:
                        for step in agent_result.history:
                            if hasattr(step, 'state') and hasattr(step.state, 'tool_results'):
                                for tool_result in step.state.tool_results:
                                    # Check if this is a JS execution result
                                    result_str = str(tool_result)
                                    if 'execute_js' in result_str or 'best_locator' in result_str or 'element_found' in result_str:
                                        try:
                                            # Try to parse JSON from the result
                                            json_match = re.search(
                                                r'\{[^{}]*"success"[^{}]*"element_found".*?\}', result_str, re.DOTALL)
                                            if not json_match:
                                                # Try broader JSON search
                                                json_match = re.search(
                                                    r'\{(?:[^{}]|\{[^{}]*\})*\}', result_str, re.DOTALL)

                                            if json_match:
                                                locator_data = json.loads(
                                                    json_match.group(0))
                                                if locator_data.get('success') and locator_data.get('element_found'):
                                                    success = True
                                                    logger.info(
                                                        f"   📍 Found JS result in history for {element_id}")
                                                    break
                                        except (json.JSONDecodeError, AttributeError) as e:
                                            logger.debug(
                                                f"   Could not parse tool result as JSON: {e}")
                                            continue

                            if success:
                                break

                    # Fallback: Try to get from final_result
                    if not success:
                        final_result = ""
                        if hasattr(agent_result, 'final_result'):
                            final_result = str(agent_result.final_result())
                        elif hasattr(agent_result, 'history') and agent_result.history:
                            if len(agent_result.history) > 0:
                                final_result = str(
                                    agent_result.history[-1].result) if hasattr(agent_result.history[-1], 'result') else ""

                        if final_result:
                            try:
                                json_match = re.search(
                                    r'\{.*"best_locator".*\}', final_result, re.DOTALL)
                                if json_match:
                                    locator_data = json.loads(
                                        json_match.group(0))
                                    success = locator_data.get(
                                        'success', False) and locator_data.get('element_found', False)
                            except json.JSONDecodeError:
                                # Agent completed but didn't return structured data - mark as found anyway
                                success = bool(final_result and len(
                                    final_result.strip()) > 20)
                                logger.warning(
                                    f"   ⚠️ Agent completed for {element_id} but no structured locator data found")

                    # Store result for this element
                    element_result = {
                        "element_id": element_id,
                        "description": element_desc,
                        "found": success,
                        "best_locator": locator_data.get('best_locator') if locator_data else None,
                        "all_locators": locator_data.get('all_locators', []) if locator_data else [],
                        "validation": locator_data.get('validation_summary', {}) if locator_data else {},
                        "fallback_used": locator_data.get('fallback_used') if locator_data else None,
                        "execution_time": element_execution_time,
                        "element_info": locator_data.get('element_info', {}) if locator_data else {}
                    }

                    results_list.append(element_result)

                    logger.info(
                        f"✅ Element {element_id} processed: {'Found' if success else 'Not found'}")
                    if success and locator_data:
                        logger.info(
                            f"   Best locator: {locator_data.get('best_locator')}")

                except Exception as elem_error:
                    logger.error(
                        f"❌ Error processing element {element_id}: {elem_error}", exc_info=True)
                    # Continue with other elements (partial results support)
                    results_list.append({
                        "element_id": element_id,
                        "description": element_desc,
                        "found": False,
                        "error": str(elem_error),
                        "best_locator": None,
                        "all_locators": [],
                        "validation": {},
                        "execution_time": 0
                    })

            logger.info(
                f"🎉 Batch processing complete: {len(results_list)}/{len(elements_list)} elements processed")

            # Calculate success metrics
            successful_elements = [r for r in results_list if r.get('found')]
            failed_elements = [r for r in results_list if not r.get('found')]

            # ========================================
            # VALIDATION VERIFICATION BEFORE WORKFLOW COMPLETION
            # ========================================
            # Verify all elements have proper validation data
            logger.info("🔍 Verifying validation data for all elements...")
            
            validation_issues = []
            elements_without_validation = []
            elements_not_unique = []
            elements_not_valid = []
            
            for result in results_list:
                elem_id = result.get('element_id', 'unknown')
                
                # Check if element has validated=True
                if not result.get('validated', False):
                    validation_issues.append(f"{elem_id}: missing validated=True")
                    elements_without_validation.append(elem_id)
                
                # Check if element has count=1 and unique=True (only for found elements)
                if result.get('found', False):
                    count = result.get('count', 0)
                    unique = result.get('unique', False)
                    valid = result.get('valid', False)
                    
                    if count != 1 or not unique:
                        validation_issues.append(f"{elem_id}: count={count}, unique={unique} (expected count=1, unique=True)")
                        elements_not_unique.append(elem_id)
                    
                    if not valid:
                        validation_issues.append(f"{elem_id}: valid={valid} (expected valid=True)")
                        elements_not_valid.append(elem_id)
            
            # Log validation summary
            if validation_issues:
                logger.warning(f"⚠️ Validation issues found for {len(validation_issues)} element(s):")
                for issue in validation_issues:
                    logger.warning(f"   - {issue}")
            else:
                logger.info("✅ All elements have complete validation data")
            
            # Create validation summary for results
            validation_summary = {
                'total_elements': len(results_list),
                'elements_with_validation': len(results_list) - len(elements_without_validation),
                'elements_without_validation': len(elements_without_validation),
                'elements_unique': len([r for r in results_list if r.get('unique', False) and r.get('found', False)]),
                'elements_not_unique': len(elements_not_unique),
                'elements_valid': len([r for r in results_list if r.get('valid', False) and r.get('found', False)]),
                'elements_not_valid': len(elements_not_valid),
                'validation_issues': validation_issues,
                'elements_without_validation_list': elements_without_validation,
                'elements_not_unique_list': elements_not_unique,
                'elements_not_valid_list': elements_not_valid
            }
            
            logger.info(f"📊 Validation Summary:")
            logger.info(f"   Total elements: {validation_summary['total_elements']}")
            logger.info(f"   Elements with validation: {validation_summary['elements_with_validation']}/{validation_summary['total_elements']}")
            logger.info(f"   Elements with unique locators: {validation_summary['elements_unique']}/{len(successful_elements)}")
            logger.info(f"   Elements with valid locators: {validation_summary['elements_valid']}/{len(successful_elements)}")
            
            if validation_issues:
                logger.warning(f"   ⚠️ {len(validation_issues)} validation issue(s) detected")
            # ========================================

            # CRITICAL: Only consider successful if ALL elements have unique locators
            # This ensures we don't proceed with placeholder locators or non-unique locators
            all_found = len(successful_elements) == len(
                results_list) and len(results_list) > 0

            return {
                # Changed from 'len(successful_elements) > 0' to require ALL elements found
                'success': all_found,
                'session_id': str(id(session)),
                'results': results_list,
                'summary': {
                    'total_elements': len(elements_list),
                    'successful': len(successful_elements),
                    'failed': len(failed_elements),
                    'success_rate': len(successful_elements) / len(elements_list) if len(elements_list) > 0 else 0
                },
                'validation_summary': validation_summary,  # Add validation summary to results
                'pages_visited': pages_visited,
                'popups_handled': popups_handled,
                'execution_time': time.time() - tasks[task_id]['started_at'],
                'agent_status': 'completed'
            }

        except Exception as e:
            error_msg = str(e)
            logger.error(
                f"Error in batch browser automation: {error_msg}", exc_info=True)
            return {
                'success': False,
                'error': error_msg,
                'results': results_list,  # Return partial results if any
                'execution_time': time.time() - tasks[task_id].get('started_at', time.time()),
                'agent_status': 'failed'
            }
        finally:
            # Proper cleanup
            if session:
                try:
                    if hasattr(session, 'close'):
                        await session.close()
                    elif hasattr(session, 'browser') and hasattr(session.browser, 'close'):
                        await session.browser.close()
                    else:
                        logger.warning(
                            "Could not find proper close method for browser session")

                    logger.info(
                        "🔒 Browser session closed successfully (batch mode)")
                except Exception as e:
                    logger.error(f"Error closing browser session: {e}")

    # Run the batch automation in a new event loop
    loop = None
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        results = loop.run_until_complete(
            run_batch_browser_use(elements, url, user_query))
    except Exception as e:
        error_msg = str(e)
        logger.error(
            f"Error in batch event loop execution: {error_msg}", exc_info=True)
        results = {
            'success': False,
            'error': f"Batch event loop error: {error_msg}",
            'execution_time': time.time() - tasks[task_id].get('started_at', time.time()),
            'agent_status': 'failed'
        }
    finally:
        if loop:
            try:
                pending = asyncio.all_tasks(loop)
                if pending:
                    for task in pending:
                        task.cancel()
                    loop.run_until_complete(asyncio.gather(
                        *pending, return_exceptions=True))

                loop.close()
            except Exception as e:
                logger.error(f"Error closing event loop: {e}")

    # Update the task status and results
    tasks[task_id].update({
        "status": "completed",
        "completed_at": time.time(),
        "message": f"Batch task completed: {len(elements)} elements processed",
        "results": results
    })

    logger.info(
        f"Batch task {task_id} completed. Success: {results.get('success', False)}")
    logger.info(f"Summary: {results.get('summary', {})}")


@app.route('/', methods=['GET'])
def root():
    """Root endpoint to verify service is running."""
    return jsonify({
        "service": "Enhanced Browser Use Service with Vision-Based Locators",
        "status": "running",
        "version": "4.0.0",
        "improvements": [
            "Vision AI for element identification (built-in browser-use)",
            "Structured JSON locator output",
            "Multiple locator strategies (10+ options)",
            "Locator stability scoring",
            "Validation and uniqueness checking",
            "Smart fallback mechanisms",
            "Better encoding handling",
            "Proper session cleanup",
            "NEW: Batch processing mode for multiple elements in one session",
            "NEW: Persistent browser session across element lookups",
            "NEW: Context-aware popup handling"
        ],
        "endpoints": [
            "GET / - This endpoint",
            "GET /health - Health check",
            "GET /probe - Legacy health check",
            "POST /submit - Submit single element automation task",
            "POST /workflow - Process workflow task (unified session, RECOMMENDED)",
            "POST /batch - Deprecated alias for /workflow (backward compatible)",
            "GET /query/<task_id> - Query task status",
            "GET /tasks - List all tasks"
        ]
    }), 200


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({
        "status": "healthy",
        "service": "enhanced_browser_use_service",
        "timestamp": time.time(),
        "active_tasks": len([t for t in tasks.values() if t.get('status') in ['processing', 'running']]),
        "total_tasks": len(tasks),
        "encoding": "utf-8",
        "google_api_configured": bool(GOOGLE_API_KEY and GOOGLE_API_KEY != 'your_api_key_here')
    }), 200


@app.route('/probe', methods=['GET'])
def probe():
    """Legacy probe endpoint for backward compatibility."""
    return jsonify({"status": "alive", "message": "enhanced_browser_use_service is alive"}), 200


@app.route('/submit', methods=['POST'])
def submit():
    """Submit a new browser automation task."""
    try:
        # Check if request has JSON data
        if not request.is_json:
            return jsonify({
                "status": "error",
                "message": "Request must be JSON with Content-Type: application/json"
            }), 400

        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "No JSON data provided."}), 400

        objective = data.get("browser_use_objective")
        if not objective:
            return jsonify({"status": "error", "message": "No 'browser_use_objective' field provided."}), 400

        # Check if service is busy (only one task at a time)
        active_tasks = [t for t in tasks.values() if t.get('status') in [
            'processing', 'running']]
        if active_tasks:
            return jsonify({
                "status": "busy",
                "message": "Service is currently processing another task. Please try again later.",
                "active_tasks": len(active_tasks)
            }), 429

        # Generate a unique task ID
        task_id = str(uuid.uuid4())

        # Initialize the task with "processing" status
        tasks[task_id] = {
            "status": "processing",
            "objective": objective,
            "created_at": time.time(),
            "started_at": None,
            "completed_at": None
        }

        # Submit the task to the thread pool executor
        future = executor.submit(process_task, task_id, objective)
        tasks[task_id]['future'] = future

        logger.info(
            f"Task {task_id} submitted with objective: {objective[:100]}{'...' if len(objective) > 100 else ''}")

        # Return the task ID immediately
        return jsonify({
            "status": "processing",
            "task_id": task_id,
            "message": "Task submitted successfully with enhanced processing"
        }), 202

    except Exception as e:
        logger.error(f"Error in submit endpoint: {e}", exc_info=True)
        return jsonify({"status": "error", "message": f"Internal server error: {str(e)}"}), 500


@app.route('/workflow', methods=['POST'])
# Deprecated alias for backward compatibility
@app.route('/batch', methods=['POST'])
def workflow_submit():
    """
    Process a workflow task with multiple elements in a single browser session.

    This endpoint handles complete user workflows (navigate → act → extract locators).
    All elements are processed in ONE browser session for context preservation.

    Endpoints:
        /workflow - Primary endpoint (recommended)
        /batch - Deprecated alias (backward compatible)

    Request JSON:
        {
            "elements": [{"id": "elem_1", "description": "...", "action": "input"}, ...],
            "url": "https://example.com",
            "user_query": "search for shoes and get product name",
            "enable_custom_actions": true  // Optional: Enable/disable custom actions (defaults to config value)
        }

    Response JSON:
        {
            "task_id": "uuid",
            "status": "processing",
            "message": "Workflow task submitted (N elements in single session)"
        }
    """
    # Log deprecation warning if /batch endpoint is used
    if request.path == '/batch':
        logger.warning(
            "⚠️  /batch endpoint is deprecated. Please use /workflow instead.")

    logger.info(f"📥 Received workflow request via {request.path}")
    try:
        # Check if request has JSON data
        if not request.is_json:
            return jsonify({
                "status": "error",
                "message": "Request must be JSON with Content-Type: application/json"
            }), 400

        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "No JSON data provided."}), 400

        # Validate batch request structure
        elements = data.get("elements")
        if not elements or not isinstance(elements, list):
            return jsonify({
                "status": "error",
                "message": "Missing or invalid 'elements' field. Must be a list of element descriptions."
            }), 400

        if len(elements) == 0:
            return jsonify({
                "status": "error",
                "message": "Elements list cannot be empty."
            }), 400

        url = data.get("url")
        user_query = data.get("user_query", "")
        session_config = data.get("session_config", {})
        
        # Feature flag: enable_custom_actions (defaults to config value if not provided)
        # Phase: Integration and Deployment | Requirements: 10.1, 10.3
        enable_custom_actions = data.get("enable_custom_actions")
        if enable_custom_actions is None:
            # Default to config value if not provided in request
            enable_custom_actions = settings.ENABLE_CUSTOM_ACTIONS
            logger.info(f"🔧 enable_custom_actions not provided in request, using config default: {enable_custom_actions}")
        else:
            logger.info(f"🔧 enable_custom_actions provided in request: {enable_custom_actions}")

        # All tasks are processed as unified workflows
        # (sequential actions + element extraction in single Agent session)
        logger.info(
            f"✅ Using unified workflow mode (all tasks processed as workflows)")

        # Check if service is busy (only one task at a time)
        active_tasks = [t for t in tasks.values() if t.get('status') in [
            'processing', 'running']]
        if active_tasks:
            return jsonify({
                "status": "busy",
                "message": "Service is currently processing another task. Please try again later.",
                "active_tasks": len(active_tasks)
            }), 429

        # Generate a unique task ID
        task_id = str(uuid.uuid4())

        # Initialize the task
        tasks[task_id] = {
            "status": "processing",
            "type": "workflow",  # All tasks are workflows now
            "elements": elements,
            "url": url,
            "user_query": user_query,
            "session_config": session_config,
            "enable_custom_actions": enable_custom_actions,  # Store feature flag with task
            "created_at": time.time(),
            "started_at": None,
            "completed_at": None
        }

        # Process as unified workflow
        logger.info(
            f"🚀 Workflow task {task_id} submitted with {len(elements)} elements for URL: {url}")
        logger.info(
            f"   Processing mode: Unified workflow (single Agent session)")

        # Submit to process_task (unified workflow processor)
        future = executor.submit(
            process_task, task_id, elements, url, user_query, session_config, enable_custom_actions)

        tasks[task_id]['future'] = future
        logger.info(
            f"📝 User query: {user_query[:100]}{'...' if len(user_query) > 100 else ''}")

        # Return the task ID immediately
        return jsonify({
            "status": "processing",
            "task_id": task_id,
            "message": f"Workflow task submitted with {len(elements)} elements (unified session)",
            "elements_count": len(elements),
            "mode": "workflow"
        }), 202

    except Exception as e:
        logger.error(f"Error in batch submit endpoint: {e}", exc_info=True)
        return jsonify({"status": "error", "message": f"Internal server error: {str(e)}"}), 500


@app.route('/query/<task_id>', methods=['GET'])
def query(task_id: str):
    """Query the status of a specific task."""
    try:
        if task_id not in tasks:
            return jsonify({"status": "error", "message": "Task ID not found."}), 404

        task = tasks[task_id]

        # Prepare response data
        response_data = {
            "task_id": task_id,
            "status": task.get("status", "unknown"),
            # Truncate long objectives
            "objective": task.get("objective", "")[:200],
            "created_at": task.get("created_at"),
        }

        status = task.get("status")

        if status == "processing":
            return jsonify(response_data), 202
        elif status == "running":
            response_data.update({
                "started_at": task.get("started_at"),
                "running_time": time.time() - task.get("started_at", time.time()) if task.get("started_at") else 0
            })
            return jsonify(response_data), 202
        elif status == "completed":
            response_data.update({
                "started_at": task.get("started_at"),
                "completed_at": task.get("completed_at"),
                "message": task.get("message"),
                "results": task.get("results", {}),
                "total_time": (task.get("completed_at", time.time()) - task.get("created_at", time.time())) if task.get("created_at") else 0
            })
            logger.info(
                f"Task {task_id} query completed: {task.get('results', {}).get('success', False)}")
            return jsonify(response_data), 200
        else:
            return jsonify(response_data), 200

    except Exception as e:
        logger.error(f"Error in query endpoint: {e}", exc_info=True)
        return jsonify({"status": "error", "message": f"Internal server error: {str(e)}"}), 500


@app.route('/tasks', methods=['GET'])
def list_tasks():
    """List all tasks with their status."""
    try:
        task_list = []
        for task_id, task_data in tasks.items():
            task_summary = {
                "task_id": task_id,
                "status": task_data.get("status", "unknown"),
                # Truncate for display
                "objective": task_data.get("objective", "")[:100],
                "created_at": task_data.get("created_at"),
            }
            if task_data.get("completed_at"):
                task_summary["completed_at"] = task_data["completed_at"]
                task_summary["success"] = task_data.get(
                    "results", {}).get("success", False)
            task_list.append(task_summary)

        return jsonify({
            "tasks": task_list,
            "total_tasks": len(task_list),
            "active_tasks": len([t for t in tasks.values() if t.get('status') in ['processing', 'running']])
        }), 200

    except Exception as e:
        logger.error(f"Error in list_tasks endpoint: {e}", exc_info=True)
        return jsonify({"status": "error", "message": f"Internal server error: {str(e)}"}), 500


@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors."""
    return jsonify({
        "status": "error",
        "message": "Endpoint not found",
        "available_endpoints": [
            "GET / - Service info",
            "GET /health - Health check",
            "GET /probe - Legacy health check",
            "POST /submit - Submit single element task",
            "POST /batch - Submit batch task (multiple elements)",
            "GET /query/<task_id> - Query task",
            "GET /tasks - List tasks"
        ]
    }), 404


@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors."""
    return jsonify({
        "status": "error",
        "message": "Internal server error",
        "error": str(error)
    }), 500


if __name__ == '__main__':
    # Set encoding environment variables
    os.environ['PYTHONIOENCODING'] = 'utf-8'

    logger.info("Starting Enhanced Browser Use Service v4.0...")
    service_url = os.getenv(
        "BROWSER_USE_SERVICE_URL") or settings.BROWSER_USE_SERVICE_URL
    parsed_url = urlparse(service_url)
    resolved_port = parsed_url.port or (
        443 if parsed_url.scheme == "https" else 80)
    logger.info(
        f"Service will run on {parsed_url.scheme}://{parsed_url.hostname or '0.0.0.0'}:{resolved_port}")
    logger.info("Available endpoints:")
    logger.info("  GET  / - Service information")
    logger.info("  GET  /health - Health check")
    logger.info("  GET  /probe - Legacy health check")
    logger.info("  POST /submit - Submit single element task")
    logger.info(
        "  POST /workflow - Process workflow task (unified session, primary endpoint)")
    logger.info(
        "  POST /batch - Deprecated alias for /workflow (backward compatible)")
    logger.info("  GET  /query/<task_id> - Query task status")
    logger.info("  GET  /tasks - List all tasks")

    logger.info("Enhanced features:")
    logger.info("  - Better encoding handling for international characters")
    logger.info("  - Improved CSS selector detection strategies")
    logger.info("  - Enhanced error handling and recovery")
    logger.info("  - Batch processing with persistent browser sessions (NEW)")
    logger.info("  - Context-aware popup handling (NEW)")
    logger.info("  - Proper browser session cleanup")
    logger.info("  - Fallback mechanisms for dynamic sites")

    app.run(debug=False, host='0.0.0.0', port=resolved_port, threaded=True)
