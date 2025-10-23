# ========================================
# UNICODE FIX - MUST BE FIRST
# ========================================
# Force UTF-8 encoding on Windows BEFORE any other imports
import sys
import os
import io

if sys.platform.startswith('win'):
    # Set environment variables
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    os.environ['PYTHONUTF8'] = '1'

    # Reconfigure stdout/stderr with UTF-8
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer, encoding='utf-8', errors='replace')

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
# STANDARD LIBRARY & THIRD-PARTY IMPORTS
# ========================================
from browser_use.browser.session import BrowserSession
from browser_use import Agent, Browser
from dotenv import load_dotenv
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
from typing import Dict, Any

# ========================================
# LOCAL IMPORTS
# ========================================
# These imports work because:
# 1. tools/__init__.py sets up path (when imported as module)
# 2. Fallback above sets up path (when run directly)
from src.backend.core.config import settings

# ========================================
# LOGGING SETUP
# ========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ],
    encoding="utf-8"
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv("src/backend/.env")

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
                console.log('✅ SUCCESS with nearby coordinates:', candidate.tagName, 'offset:', [dx, dy]);
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
                    element = selectClosestToCoordinates(candidates, COORDINATES);
                    if (element) {
                        console.log('✅ SUCCESS with text search:', element.tagName, 'keyword:', keyword);
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
                            console.log('✅ SUCCESS with attribute search:', element.tagName, 'keyword:', keyword);
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
    console.log('✅ Element found via', fallbackUsed, '- Generating and validating locators...');
    
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
    {{"locator": "css=button#search-button", "type": "css-id", "unique": true, "confidence": 1.0}}
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
    {"locator": "id=search-button", "type": "id", "unique": true, "confidence": 1.0},
    {"locator": "css=button#search-button", "type": "css-id", "unique": true, "confidence": 1.0}
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


def process_task(task_id: str, elements: list, url: str, user_query: str, session_config: dict):
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

    async def run_unified_workflow():
        """Execute the entire workflow in ONE Agent session."""
        from browser_use.browser.session import BrowserSession
        from browser_use.llm.google import ChatGoogle

        session = None

        try:
            # Initialize browser session ONCE
            logger.info("🌐 Initializing browser session...")
            session = BrowserSession(
                headless=session_config.get("headless", False),
                viewport={'width': 1920, 'height': 1080}
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

            # JavaScript validation code for locator extraction with CONTENT-BASED SEARCH
            # Config values passed from Python
            content_retries = LOCATOR_EXTRACTION_CONFIG["content_based_retries"]
            coord_retries = LOCATOR_EXTRACTION_CONFIG["coordinate_based_retries"]
            coord_offsets = LOCATOR_EXTRACTION_CONFIG["coordinate_offsets"]

            js_validation_code = r"""
(function() {{
    const ELEMENT_ID = "ELEMENT_ID_PLACEHOLDER";
    const CONTENT_HINT = "CONTENT_HINT_PLACEHOLDER";
    const ELEMENT_TYPE_HINT = "ELEMENT_TYPE_HINT_PLACEHOLDER";
    const COORDINATES = {{ x: CENTER_X, y: CENTER_Y }};
    
    console.log(`\n🎯 === LOCATOR EXTRACTION FOR ${{ELEMENT_ID}} ===`);
    console.log(`   Content hint: "${{CONTENT_HINT}}"`);
    console.log(`   Type hint: ${{ELEMENT_TYPE_HINT}}`);
    console.log(`   Coordinates: (${{COORDINATES.x}}, ${{COORDINATES.y}})`);
    
    // ========================================
    // HELPER: Extract current price from element
    // ========================================
    function extractCurrentPrice(element) {{
        console.log(`   💰 Extracting price from element...`);
        
        // Strategy 1: Find dedicated current/special price element
        const priceSelectors = [
            '[class*="current"]',
            '[class*="special"]',
            '[class*="sale"]',
            '[class*="offer"]',
            '[class*="deal"]',
            '[class*="price"]:not([class*="original"]):not([class*="was"])'
        ];
        
        for (const selector of priceSelectors) {{
            const priceElem = element.querySelector(selector);
            if (priceElem && priceElem.offsetParent !== null) {{
                const priceText = priceElem.textContent.trim();
                const match = priceText.match(/[₹$€£¥]?[\d,]+(?:\.\d{{2}})?/);
                if (match) {{
                    console.log(`   ✅ Found price via selector "${{selector}}": ${{match[0]}}`);
                    return match[0];
                }}
            }}
        }}
        
        // Strategy 2: Extract first price pattern from full text
        const fullText = element.textContent || '';
        const priceMatch = fullText.match(/[₹$€£¥][\d,]+(?:\.\d{{2}})?/);
        if (priceMatch) {{
            console.log(`   ✅ Found price via regex: ${{priceMatch[0]}}`);
            return priceMatch[0];
        }}
        
        // Strategy 3: Extract first number sequence
        const numberMatch = fullText.match(/[\d,]+(?:\.\d{{2}})?/);
        if (numberMatch) {{
            console.log(`   ✅ Found number: ${{numberMatch[0]}}`);
            return numberMatch[0];
        }}
        
        console.log(`   ⚠️  Could not extract price, returning full text`);
        return fullText.slice(0, 50);
    }}
    
    // ========================================
    // HELPER: Extract main text (exclude ratings, labels)
    // ========================================
    function extractMainText(element) {{
        console.log(`   📝 Extracting main text from element...`);
        
        // Strategy 1: Find specific name/title child
        const nameSelectors = ['[class*="name"]', '[class*="title"]', 'a[title]', 'h1', 'h2', 'h3'];
        for (const selector of nameSelectors) {{
            const nameElem = element.querySelector(selector);
            if (nameElem && nameElem.offsetParent !== null) {{
                const text = nameElem.textContent.trim();
                if (text.length > 5) {{
                    console.log(`   ✅ Found main text via selector "${{selector}}"`);
                    return text;
                }}
            }}
        }}
        
        // Strategy 2: Get longest text node (usually main content)
        const textNodes = [];
        const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT, null, false);
        let node;
        while (node = walker.nextNode()) {{
            const text = node.textContent.trim();
            if (text.length > 10) {{
                textNodes.push(text);
            }}
        }}
        
        if (textNodes.length > 0) {{
            textNodes.sort((a, b) => b.length - a.length);
            console.log(`   ✅ Found main text via longest text node`);
            return textNodes[0];
        }}
        
        // Fallback: Return element's direct text
        console.log(`   ⚠️  Using element's direct text`);
        return element.textContent.trim();
    }}
    
    // ========================================
    // STRATEGY 1: CONTENT-BASED SEARCH (PRIMARY)
    // ========================================
    function findElementByContent(contentHint, elementTypeHint) {{
        if (!contentHint || contentHint.trim() === '' || contentHint === 'CONTENT_HINT_PLACEHOLDER') {{
            console.log(`   ⚠️  No content hint provided, skipping content-based search`);
            return null;
        }}
        
        console.log(`\n🔍 STRATEGY 1: Content-based search`);
        console.log(`   Searching for: "${{contentHint}}"`);
        
        // Escape special characters for XPath
        const escapeXPath = (str) => {{
            // Simple approach: replace double quotes with single quotes for XPath
            return `"${{str.replace(/"/g, "'")}}"`; 
        }};
        
        const escapedContent = escapeXPath(contentHint);
        
        // Attempt 1: Exact text match
        try {{
            const xpathExact = `//*[normalize-space(text())=${{escapedContent}}]`;
            console.log(`   Attempt 1: Exact text match`);
            const result = document.evaluate(xpathExact, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            
            for (let i = 0; i < result.snapshotLength; i++) {{
                const candidate = result.snapshotItem(i);
                if (candidate.offsetParent !== null) {{
                    if (!elementTypeHint || elementTypeHint === 'ELEMENT_TYPE_HINT_PLACEHOLDER' ||
                        candidate.tagName.toLowerCase() === elementTypeHint.toLowerCase()) {{
                        console.log(`   ✅ Found by exact text: <${{candidate.tagName}}>`);
                        return candidate;
                    }}
                }}
            }}
        }} catch(e) {{
            console.log(`   ❌ Exact match error: ${{e.message}}`);
        }}
        
        // Attempt 2: Partial match (first 30 chars)
        if (contentHint.length > 30) {{
            try {{
                const partialText = contentHint.slice(0, 30);
                const escapedPartial = escapeXPath(partialText);
                const xpathPartial = `//*[contains(normalize-space(text()), ${{escapedPartial}})]`;
                console.log(`   Attempt 2: Partial match (first 30 chars)`);
                const result = document.evaluate(xpathPartial, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                
                for (let i = 0; i < result.snapshotLength; i++) {{
                    const candidate = result.snapshotItem(i);
                    if (candidate.offsetParent !== null) {{
                        if (!elementTypeHint || elementTypeHint === 'ELEMENT_TYPE_HINT_PLACEHOLDER' ||
                            candidate.tagName.toLowerCase() === elementTypeHint.toLowerCase()) {{
                            console.log(`   ✅ Found by partial text: <${{candidate.tagName}}>`);
                            return candidate;
                        }}
                    }}
                }}
            }} catch(e) {{
                console.log(`   ❌ Partial match error: ${{e.message}}`);
            }}
        }}
        
        // Attempt 3: Search in child elements
        console.log(`   Attempt 3: Searching in child text nodes`);
        const allElements = document.querySelectorAll('*');
        for (const el of allElements) {{
            if (el.offsetParent === null) continue;
            
            const textContent = el.textContent?.trim() || '';
            if (textContent.includes(contentHint)) {{
                if (!elementTypeHint || elementTypeHint === 'ELEMENT_TYPE_HINT_PLACEHOLDER' ||
                    el.tagName.toLowerCase() === elementTypeHint.toLowerCase()) {{
                    console.log(`   ✅ Found by child text search: <${{el.tagName}}>`);
                    return el;
                }}
            }}
        }}
        
        console.log(`   ❌ Content-based search failed`);
        return null;
    }}
    
    // ========================================
    // STRATEGY 2: COORDINATE-BASED SEARCH (FALLBACK)
    // ========================================
    function findElementByCoordinates(coords, contentHint, offsetsToTry) {{
        console.log(`\n🎯 STRATEGY 2: Coordinate-based search`);
        console.log(`   Base coordinates: (${{coords.x}}, ${{coords.y}})`);
        
        // Attempt 1: Original coordinates
        let element = document.elementFromPoint(coords.x, coords.y);
        if (element && element.tagName !== 'HTML' && element.tagName !== 'BODY') {{
            console.log(`   Attempt 1: Original coordinates → <${{element.tagName}}>`);
            
            // Validate against content hint if provided
            if (contentHint && contentHint !== 'CONTENT_HINT_PLACEHOLDER') {{
                const matches = element.textContent.includes(contentHint);
                if (matches) {{
                    console.log(`   ✅ Content validation passed`);
                    return element;
                }} else {{
                    console.log(`   ⚠️  Content mismatch: "${{element.textContent.slice(0, 50)}}..."`);
                    console.log(`   Expected: "${{contentHint}}"`);
                }}
            }} else {{
                console.log(`   ✅ Found element (no content validation)`);
                return element;
            }}
        }}
        
        // Attempts 2-N: Try coordinate offsets
        for (let i = 0; i < offsetsToTry.length; i++) {{
            const offset = offsetsToTry[i];
            const newX = coords.x + offset.x;
            const newY = coords.y + offset.y;
            
            console.log(`   Attempt ${{i + 2}}: Offset (${{offset.x}}, ${{offset.y}}) - ${{offset.reason}}`);
            element = document.elementFromPoint(newX, newY);
            
            if (element && element.tagName !== 'HTML' && element.tagName !== 'BODY') {{
                console.log(`     → <${{element.tagName}}>`);
                
                // Validate against content hint
                if (contentHint && contentHint !== 'CONTENT_HINT_PLACEHOLDER') {{
                    if (element.textContent.includes(contentHint)) {{
                        console.log(`   ✅ Found with offset + content validation`);
                        return element;
                    }}
                }} else {{
                    console.log(`   ✅ Found with offset (no content validation)`);
                    return element;
                }}
            }}
        }}
        
        console.log(`   ❌ Coordinate-based search failed`);
        return null;
    }}
    
    // ========================================
    // STRATEGY 3: ELEMENT TYPE FALLBACK (LAST RESORT)
    // ========================================
    function findElementByType(elementTypeHint) {{
        if (!elementTypeHint || elementTypeHint === 'ELEMENT_TYPE_HINT_PLACEHOLDER') {{
            console.log(`   ⚠️  No element type hint, skipping type-based search`);
            return null;
        }}
        
        console.log(`\n🔧 STRATEGY 3: Element type fallback`);
        console.log(`   Searching for first visible <${{elementTypeHint}}>`);
        
        const candidates = document.querySelectorAll(elementTypeHint);
        for (const candidate of candidates) {{
            if (candidate.offsetParent !== null) {{
                console.log(`   ✅ Found first visible <${{elementTypeHint}}>`);
                return candidate;
            }}
        }}
        
        console.log(`   ❌ Type-based search failed`);
        return null;
    }}
    
    // ========================================
    // MAIN: FIND ELEMENT USING ALL STRATEGIES
    // ========================================
    let element = null;
    let strategyUsed = 'none';
    
    // Define coordinate offsets to try (passed from Python config)
    const coordinateOffsets = {coord_offsets_json};
    
    // Try Strategy 1: Content-based
    element = findElementByContent(CONTENT_HINT, ELEMENT_TYPE_HINT);
    if (element) {{
        strategyUsed = 'content-based';
    }}
    
    // Try Strategy 2: Coordinate-based (with higher retry count)
    if (!element) {{
        element = findElementByCoordinates(COORDINATES, CONTENT_HINT, coordinateOffsets);
        if (element) {{
            strategyUsed = 'coordinate-based';
        }}
    }}
    
    // Try Strategy 3: Element type fallback
    if (!element) {{
        element = findElementByType(ELEMENT_TYPE_HINT);
        if (element) {{
            strategyUsed = 'element-type';
        }}
    }}
    
    // If all strategies failed
    if (!element) {{
        console.log(`\n❌ ALL STRATEGIES FAILED for ${{ELEMENT_ID}}`);
        return JSON.stringify({{
            element_id: ELEMENT_ID,
            found: false,
            error: "Could not find element with any strategy",
            attempted_strategies: ['content-based', 'coordinate-based', 'element-type']
        }});
    }}
    
    console.log(`\n✅ Element found via: ${{strategyUsed}}`);
    console.log(`   Tag: <${{element.tagName}}>`);
    console.log(`   Text: "${{element.textContent?.slice(0, 100)}}..."`);
    
    // ========================================
    // GENERATE AND VALIDATE LOCATORS
    // ========================================
    console.log(`\n🔍 Generating and validating locators...`);
    const validatedLocators = [];
    const strategies = [
        {{ type: 'id', gen: () => element.id ? `id=${{element.id}}` : null, sel: () => element.id ? `#${{element.id}}` : null }},
        {{ type: 'name', gen: () => element.name ? `name=${{element.name}}` : null, sel: () => element.name ? `[name="${{element.name}}"]` : null }},
        {{ type: 'data-testid', gen: () => element.dataset?.testid ? `css=[data-testid="${{element.dataset.testid}}"]` : null, sel: () => element.dataset?.testid ? `[data-testid="${{element.dataset.testid}}"]` : null }},
        {{ type: 'aria-label', gen: () => element.getAttribute('aria-label') ? `css=[aria-label="${{element.getAttribute('aria-label')}}"]` : null, sel: () => element.getAttribute('aria-label') ? `[aria-label="${{element.getAttribute('aria-label')}}"]` : null }},
        {{ type: 'title', gen: () => element.getAttribute('title') ? `xpath=//*[@title="${{element.getAttribute('title')}}"]` : null, sel: () => element.getAttribute('title') ? `//*[@title="${{element.getAttribute('title')}}"]` : null, isXPath: true }},
        {{ type: 'text', gen: () => element.textContent?.trim() ? `xpath=//*[text()="${{element.textContent.trim().slice(0,50)}}"]` : null, sel: () => element.textContent?.trim() ? `//*[text()="${{element.textContent.trim().slice(0,50)}}"]` : null, isXPath: true }},
        {{ type: 'css-class', gen: () => element.className ? `css=${{element.tagName.toLowerCase()}}.${{element.className.split(' ')[0]}}` : null, sel: () => element.className ? `${{element.tagName.toLowerCase()}}.${{element.className.split(' ')[0]}}` : null }}
    ];
    
    for (const s of strategies) {{
        const locator = s.gen();
        const selector = s.sel();
        if (!locator || !selector) continue;
        
        try {{
            let matches;
            if (s.isXPath) {{
                const result = document.evaluate(selector, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                matches = [];
                for (let i = 0; i < result.snapshotLength; i++) matches.push(result.snapshotItem(i));
            }} else {{
                matches = Array.from(document.querySelectorAll(selector));
            }}
            
            if (matches.length > 0 && matches.includes(element)) {{
                validatedLocators.push({{
                    type: s.type,
                    locator: locator,
                    unique: matches.length === 1,
                    count: matches.length
                }});
                console.log(`   ✅ ${{s.type}}: ${{locator}} → ${{matches.length === 1 ? 'UNIQUE ⭐' : `NOT UNIQUE (${{matches.length}})`}}`);
            }}
        }} catch(e) {{ 
            console.log(`   ❌ ${{s.type}}: ERROR - ${{e.message}}`); 
        }}
    }}
    
    // Sort: unique locators first
    validatedLocators.sort((a, b) => (a.unique && !b.unique) ? -1 : ((!a.unique && b.unique) ? 1 : 0));
    
    // CRITICAL: Only mark as found if we have at least ONE UNIQUE locator
    const uniqueLocators = validatedLocators.filter(loc => loc.unique);
    const hasUniqueLocator = uniqueLocators.length > 0;
    const bestLocator = hasUniqueLocator ? uniqueLocators[0] : validatedLocators[0];
    
    console.log(`\n✅ === EXTRACTION COMPLETE FOR ${{ELEMENT_ID}} ===`);
    console.log(`   Strategy: ${{strategyUsed}}`);
    console.log(`   Total locators: ${{validatedLocators.length}}`);
    console.log(`   Unique locators: ${{uniqueLocators.length}}`);
    console.log(`   Best locator: ${{bestLocator?.locator || 'none'}}`);
    console.log(`   Status: ${{hasUniqueLocator ? 'FOUND (unique)' : 'NOT FOUND (no unique locator)'}}`);
    
    return JSON.stringify({{
        element_id: ELEMENT_ID,
        found: hasUniqueLocator,  // ONLY true if we have a UNIQUE locator
        best_locator: bestLocator?.locator,
        all_locators: validatedLocators,
        unique_locators: uniqueLocators,
        strategy_used: strategyUsed,
        element_info: {{
            tag: element.tagName,
            text: element.textContent?.slice(0, 100),
            visible: element.offsetParent !== null
        }},
        reason: hasUniqueLocator ? 'Found unique locator' : `Found ${{validatedLocators.length}} non-unique locators`
    }});
}})();
"""

            # Replace coordinate offsets placeholder with actual JSON
            coord_offsets_json = json.dumps(coord_offsets)
            js_validation_code = js_validation_code.replace(
                '{coord_offsets_json}', coord_offsets_json)

            # Validate JavaScript code integrity
            logger.debug(f"📊 JavaScript validation code stats:")
            logger.debug(
                f"   Total length: {len(js_validation_code)} characters")
            logger.debug(
                f"   Contains 'const strategies': {'const strategies = [' in js_validation_code}")
            logger.debug(
                f"   Contains 'for (const s of strategies)': {'for (const s of strategies)' in js_validation_code}")

            # Critical validation: ensure strategies array is defined
            if 'for (const s of strategies)' in js_validation_code and 'const strategies = [' not in js_validation_code:
                logger.error(
                    "❌ JavaScript validation FAILED: 'strategies' used but not defined!")
                logger.error("   This will cause 'Uncaught at line 324' error")
                raise ValueError(
                    "JavaScript template error: strategies array not properly defined")

            # Build the unified objective
            unified_objective = f"""
You are completing a web automation workflow. Follow these steps EXACTLY:

USER'S GOAL: {user_query}

WORKFLOW STEPS:
{chr(10).join(workflow_steps)}

LOCATOR EXTRACTION - CONTENT-BASED + JAVASCRIPT VALIDATION METHOD:
For EACH element in the list above, you MUST:

Step 1: Use your vision to find the element on the page

Step 2: CAPTURE the element's visible content text (READ what you see):
   - For PRICE elements (elem with "price" in description):
     * Capture ONLY the current/selling price (e.g., "₹1,799" or "$29.99")
     * IGNORE original price, discount percentage, strikethrough text
     * Example: If you see "₹1,799 ₹3,695 51% off", capture ONLY "₹1,799"
   
   - For PRODUCT NAME / TEXT elements:
     * Capture the FULL visible text of the main element
     * EXCLUDE ratings (e.g., "(4.5 stars)"), labels, badges
     * Example: "Lite Sports Running Shoes For Men" (not "PUMA Lite Sports...")
   
   - For INPUT / SEARCH BOX elements:
     * Use EMPTY STRING "" (no visible text to capture)
   
   - For BUTTON elements:
     * Capture button text (e.g., "Add to Cart", "Search")

Step 3: IDENTIFY the element type (HTML tag name):
   - Examples: "a" (link), "div", "span", "button", "input", "select"
   - Look at the element's HTML tag, not its appearance

Step 4: GET the coordinates (x, y) of the element center

Step 5: Execute the JavaScript code below, replacing:
   - CENTER_X with the actual x coordinate
   - CENTER_Y with the actual y coordinate
   - ELEMENT_ID_PLACEHOLDER with the actual element_id (e.g., "elem_1", "elem_2")
   - CONTENT_HINT_PLACEHOLDER with the captured text from Step 2 (use EXACT text)
   - ELEMENT_TYPE_HINT_PLACEHOLDER with the tag name from Step 3 (e.g., "a", "div", "input")

JavaScript to execute for EACH element:
{js_validation_code}

EXAMPLE EXECUTION:
For elem_2 (product name):
1. Find element with vision → See text: "Lite Sports Running Shoes For Men"
2. Capture content → "Lite Sports Running Shoes For Men" (exclude rating if visible)
3. Identify type → "a" (it's a link)
4. Get coordinates → x=450, y=320
5. Execute JS with replacements:
   - CENTER_X = 450
   - CENTER_Y = 320
   - ELEMENT_ID_PLACEHOLDER = "elem_2"
   - CONTENT_HINT_PLACEHOLDER = "Lite Sports Running Shoes For Men"
   - ELEMENT_TYPE_HINT_PLACEHOLDER = "a"

The JavaScript will:
✅ Try Strategy 1: Content-based search (searches DOM for your captured text)
✅ Try Strategy 2: Coordinate-based search with offsets (if content search fails)
✅ Try Strategy 3: Element type fallback (if coordinates also fail)
✅ Validate each locator works (tests if it finds the element)
✅ Check uniqueness (count === 1 means UNIQUE, best)
✅ Return ONLY working locators
✅ Automatically prioritize: id > name > data-* > aria-* > title > text > css-class

CRITICAL: After running JavaScript for ALL elements, return this EXACT JSON structure:
{{
  "workflow_completed": true,
  "results": [
    {{"element_id": "elem_1", "found": true, "best_locator": "...", "all_locators": [...]}},
    {{"element_id": "elem_2", "found": true, "best_locator": "...", "all_locators": [...]}},
    {{"element_id": "elem_3", "found": true, "best_locator": "...", "all_locators": [...]}}
  ]
}}

DO NOT use extract_structured_data. You MUST execute the JavaScript code for proper validation.
DO NOT deviate from these steps. Complete the FULL workflow before finishing.

CRITICAL COMPLETION RULES:
1. Execute JavaScript for EACH element in the list (elem_1, elem_2, etc.) - ONE TIME ONLY
2. Collect ALL results from JavaScript executions
3. Return the JSON structure above with "workflow_completed": true
4. STOP IMMEDIATELY - Do NOT retry, do NOT re-execute, do NOT validate again
5. The task is COMPLETE once you return the JSON - NO FURTHER ACTIONS NEEDED

IMPORTANT REMINDERS:
- Always capture content text BEFORE executing JavaScript (the JS needs it for content-based search)
- For prices: Capture ONLY first price number (current price), not full combined text
- For product names: Capture full name, but exclude ratings/badges
- Content hints help the JavaScript find elements more reliably than coordinates alone
"""

            logger.info("📝 Built unified workflow objective")
            logger.info(f"   Total workflow steps: {len(workflow_steps)}")

            # Create ONE Agent for entire workflow with default LLM
            agent = Agent(
                task=unified_objective,
                browser_context=session,
                llm=ChatGoogle(
                    model=GOOGLE_MODEL,
                    api_key=GOOGLE_API_KEY,
                    temperature=0.1
                ),
                use_vision=True,
                # Dynamic max_steps based on workflow complexity
                max_steps=dynamic_max_steps,
                system_prompt="""You are a web automation agent specialized in locator extraction.

YOUR WORKFLOW:
1. Execute JavaScript for EACH element to get locators
2. The JavaScript will return JSON like: {"element_id": "elem_1", "found": true, "best_locator": "name=q", ...}
3. After extracting ALL locators, call done() to complete the task

IMPORTANT:
- Execute the JavaScript validation code for EACH element
- The JavaScript results are automatically captured
- Call done(text="Successfully extracted locators for all elements", success=True) when finished
- Do NOT retry or re-execute - once you have all results, you're done"""
            )

            # Run the unified workflow
            logger.info("🤖 Starting unified Agent...")
            logger.info(
                "🤖 Using default ChatGoogle LLM (no rate limiting needed)")
            start_time = time.time()
            agent_result = await agent.run()
            execution_time = time.time() - start_time

            logger.info(f"✅ Agent completed in {execution_time:.1f}s")

            # Parse results from agent
            results_list = []
            workflow_completed = False

            # Try to extract JSON from agent result
            final_result = ""
            if hasattr(agent_result, 'final_result'):
                final_result = str(agent_result.final_result())
            elif hasattr(agent_result, 'history') and agent_result.history:
                if len(agent_result.history) > 0:
                    final_result = str(
                        agent_result.history[-1].result) if hasattr(agent_result.history[-1], 'result') else ""

            logger.info(
                f"📝 Agent final result (first 500 chars): {final_result[:500]}")

            # Look for workflow completion JSON
            if final_result:
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

                # Strategy 1: Try all_results attribute
                if hasattr(agent_result, 'all_results') and agent_result.all_results:
                    logger.debug(
                        f"   Found all_results with {len(agent_result.all_results)} items")
                    for idx, action_result in enumerate(agent_result.all_results):
                        # DEBUG: Log available attributes
                        logger.debug(
                            f"   all_results[{idx}] attributes: {dir(action_result)}")

                        # Try multiple attribute names
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
                            content = str(action_result.result)
                            logger.debug(
                                f"   Using str(result) from all_results[{idx}]")

                        if content:
                            result_strings.append(content)
                            logger.debug(
                                f"   Collected content from all_results[{idx}]: {len(content)} chars")

                # Strategy 2: Try history attribute (MOST IMPORTANT for execute_js results)
                if hasattr(agent_result, 'history') and agent_result.history:
                    logger.debug(
                        f"   Found history with {len(agent_result.history)} items")
                    for idx, step in enumerate(agent_result.history):
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
                                if isinstance(tool_result, dict):
                                    logger.info(
                                        f"   🎯 tool_result[{tool_idx}] is a dict! Direct access possible")
                                    if 'element_id' in tool_result:
                                        direct_results.append(tool_result)
                                        logger.info(
                                            f"   ✅ Found element_id in tool_result dict!")
                                elif hasattr(tool_result, 'result'):
                                    if isinstance(tool_result.result, dict):
                                        logger.info(
                                            f"   🎯 tool_result[{tool_idx}].result is a dict!")
                                        if 'element_id' in tool_result.result:
                                            direct_results.append(
                                                tool_result.result)
                                            logger.info(
                                                f"   ✅ Found element_id in tool_result.result dict!")
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
                                # Check if we already have this element
                                existing_idx = next(
                                    (i for i, r in enumerate(results_list) if r.get('element_id') == elem_id), None)
                                if existing_idx is not None:
                                    # Replace existing result (agent retry/correction)
                                    old_locator = results_list[existing_idx].get(
                                        'best_locator')
                                    new_locator = parsed.get('best_locator')
                                    logger.info(
                                        f"   🔄 Replacing {elem_id}: '{old_locator}' → '{new_locator}' (agent retry/correction)")
                                    results_list[existing_idx] = parsed
                                else:
                                    # First occurrence, add it
                                    results_list.append(parsed)
                                    logger.info(
                                        f"   ✅ Directly parsed and added {elem_id} (best_locator: {parsed.get('best_locator')})")
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
                                        # Replace existing result (agent retry/correction)
                                        old_locator = results_list[existing_idx].get(
                                            'best_locator')
                                        new_locator = elem_data.get(
                                            'best_locator')
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
                        # Convert entire step to string
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
                        "error": "Could not extract from workflow result"
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
                """
                locator = locator_obj.get('locator', '')
                locator_type = locator_obj.get('type', '')

                # Priority 1: ID (best)
                if locator_type == 'id' or locator.startswith('id='):
                    return 100

                # Priority 2: Name attribute
                if locator_type == 'name' or locator.startswith('name='):
                    return 95

                # Priority 3: Attribute-based XPath (semantic attributes)
                if 'xpath' in locator_type or locator.startswith('//') or locator.startswith('xpath='):
                    # Check for semantic attributes
                    if '@title=' in locator:
                        return 90  # Title is very stable
                    if '@aria-label=' in locator:
                        return 88
                    if '@data-testid=' in locator or '@data-test=' in locator:
                        return 85
                    if '@placeholder=' in locator:
                        return 82

                    # Text-based XPath (good for static content)
                    if 'text()=' in locator or 'contains(text()' in locator:
                        return 75

                    # Check if it's a simple attribute XPath (not structural)
                    attribute_count = locator.count('@')
                    if attribute_count >= 1:
                        # Has attributes, check if it's not overly structural
                        index_count = locator.count(
                            '[1]') + locator.count('[2]') + locator.count('[3]')
                        if index_count == 0:
                            return 70  # Simple attribute XPath
                        elif index_count <= 2:
                            return 50  # Some structural elements

                    # Structural XPath (worst) - lots of [1], [2], etc.
                    index_count = locator.count(
                        '[1]') + locator.count('[2]') + locator.count('[3]')
                    if index_count >= 3:
                        return 20  # Very structural (fragile)
                    elif index_count >= 2:
                        return 30  # Somewhat structural

                    return 40  # Default XPath

                # Priority 4: CSS selectors
                if locator_type == 'css' or locator.startswith('css='):
                    css_selector = locator.replace('css=', '')

                    # Check for stable classes (avoid auto-generated)
                    if re.search(r'[_][0-9a-zA-Z]{5,}', css_selector):
                        return 35  # Auto-generated class (fragile)

                    if '#' in css_selector:
                        return 80  # ID in CSS

                    if '[' in css_selector:
                        return 65  # Attribute selector in CSS

                    return 45  # Regular CSS class

                return 30  # Unknown/default

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

            # Calculate metrics
            successful = sum(1 for r in results_list if r.get('found', False))
            failed = len(results_list) - successful

            return {
                'success': successful > 0,
                'workflow_mode': True,
                'workflow_completed': workflow_completed,
                'results': results_list,
                'summary': {
                    'total_elements': len(elements),
                    'successful': successful,
                    'failed': failed,
                    'success_rate': successful / len(elements) if len(elements) > 0 else 0
                },
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

            return {
                # Success if at least one element found
                'success': len(successful_elements) > 0,
                'session_id': str(id(session)),
                'results': results_list,
                'summary': {
                    'total_elements': len(elements_list),
                    'successful': len(successful_elements),
                    'failed': len(failed_elements),
                    'success_rate': len(successful_elements) / len(elements_list) if len(elements_list) > 0 else 0
                },
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
            "user_query": "search for shoes and get product name"
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
            process_task, task_id, elements, url, user_query, session_config)

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
