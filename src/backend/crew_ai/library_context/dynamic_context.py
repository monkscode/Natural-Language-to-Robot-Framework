"""
Dynamic library context generator using Robot Framework's libdoc.

This module automatically extracts keyword documentation from installed
Robot Framework libraries, ensuring the context is always up-to-date
with the installed library version.

Uses in-memory caching to avoid repeated libdoc calls during server runtime.
"""

import json
import logging
import tempfile
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Global cache for library documentation (persists during server runtime)
_LIBRARY_DOC_CACHE: Dict[str, Dict] = {}


class DynamicLibraryDocumentation:
    """
    Extracts and formats library documentation dynamically using libdoc.
    
    This ensures that the AI agents always have access to the latest
    keywords and documentation from the installed library version.
    """
    
    def __init__(self, library_name: str):
        """
        Initialize dynamic documentation extractor.
        
        Args:
            library_name: Name of the Robot Framework library
                         (e.g., 'SeleniumLibrary', 'Browser')
        """
        self.library_name = library_name
    
    def get_library_documentation(self) -> Dict:
        """
        Extract library documentation using Robot Framework's libdoc.
        Uses global cache to avoid repeated extraction during server runtime.
        
        Returns:
            Dictionary containing library metadata and keywords
            
        Raises:
            ImportError: If library is not installed
            Exception: If libdoc extraction fails
        """
        # Check global cache first
        if self.library_name in _LIBRARY_DOC_CACHE:
            logger.debug(f"Using cached documentation for {self.library_name}")
            return _LIBRARY_DOC_CACHE[self.library_name]
        
        try:
            from robot.libdoc import libdoc
            
            # Create temporary file for JSON output
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as temp_file:
                temp_path = temp_file.name
            
            logger.info(f"Extracting documentation for {self.library_name} using libdoc...")
            
            # Extract documentation in JSON format
            libdoc(self.library_name, temp_path, format='JSON')
            
            # Read the generated JSON
            with open(temp_path, 'r', encoding='utf-8') as f:
                doc_data = json.load(f)
            
            # Clean up temp file
            Path(temp_path).unlink(missing_ok=True)
            
            logger.info(f"Successfully extracted {len(doc_data.get('keywords', []))} keywords from {self.library_name}")
            
            # Store in global cache
            _LIBRARY_DOC_CACHE[self.library_name] = doc_data
            return doc_data
            
        except ImportError as e:
            logger.error(f"Library {self.library_name} is not installed: {e}")
            raise ImportError(f"Library {self.library_name} not found. Please install it first.")
        
        except Exception as e:
            logger.error(f"Failed to extract documentation for {self.library_name}: {e}")
            raise
    
    def get_keywords_summary(self, max_keywords: int = 25) -> str:
        """
        Get a formatted summary of available keywords.
        Optimized to show the most useful keywords for test automation.
        
        Args:
            max_keywords: Maximum number of keywords to include
            
        Returns:
            Formatted string with keyword documentation
        """
        try:
            doc_data = self.get_library_documentation()
            keywords = doc_data.get('keywords', [])
            
            # Filter out internal/deprecated keywords
            public_keywords = [
                kw for kw in keywords 
                if not kw['name'].startswith('_') and 'deprecated' not in kw.get('doc', '').lower()
            ]
            
            # Prioritize common automation keywords
            priority_keywords = [
                'open browser', 'new browser', 'new page', 'close browser',
                'click', 'click element', 'input text', 'fill text',
                'get text', 'press keys', 'keyboard key',
                'wait until element is visible', 'wait for elements state',
                'select from list', 'select options by',
                'should be true', 'should contain'
            ]
            
            # Sort: priority keywords first, then by name length
            def sort_key(kw):
                name_lower = kw['name'].lower()
                if name_lower in priority_keywords:
                    return (0, priority_keywords.index(name_lower))
                return (1, len(kw['name']), kw['name'])
            
            public_keywords.sort(key=sort_key)
            
            # Take top N keywords
            top_keywords = public_keywords[:max_keywords]
            
            summary = f"--- {self.library_name.upper()} KEYWORDS (Auto-generated from v{doc_data.get('version', 'Unknown')}) ---\n\n"
            summary += f"Available Keywords: {len(public_keywords)} total\n\n"
            summary += "**MOST COMMONLY USED KEYWORDS:**\n\n"
            
            for kw in top_keywords:
                name = kw['name']
                args = kw.get('args', [])
                doc = kw.get('doc', '')
                
                # Format arguments (simplified)
                if args:
                    # Remove default values and type hints for brevity
                    simple_args = []
                    for arg in args:
                        # Handle both string and dict formats
                        if isinstance(arg, dict):
                            arg_name = arg.get('name', '')
                        else:
                            arg_name = str(arg).split('=')[0].split(':')[0].strip()
                        
                        if arg_name and arg_name not in ['self', 'cls']:
                            simple_args.append(f"<{arg_name}>")
                    arg_str = '    '.join(simple_args)
                else:
                    arg_str = ""
                
                # Get first sentence of documentation
                doc_first_sentence = doc.split('.')[0] if doc else ""
                if len(doc_first_sentence) > 80:
                    doc_first_sentence = doc_first_sentence[:77] + "..."
                
                summary += f"• {name}    {arg_str}\n"
                if doc_first_sentence:
                    summary += f"  → {doc_first_sentence}\n"
                summary += "\n"
            
            summary += f"\n**Note:** Use the most appropriate keyword for each action. "
            summary += f"All {len(public_keywords)} keywords are available.\n"
            
            return summary
            
        except Exception as e:
            logger.warning(f"Could not generate keywords summary: {e}")
            return f"--- {self.library_name.upper()} KEYWORDS ---\n\nCould not load dynamic documentation.\n"
    
    def get_keyword_details(self, keyword_name: str) -> Optional[Dict]:
        """
        Get detailed information about a specific keyword.
        
        Args:
            keyword_name: Name of the keyword
            
        Returns:
            Dictionary with keyword details or None if not found
        """
        try:
            doc_data = self.get_library_documentation()
            keywords = doc_data.get('keywords', [])
            
            for kw in keywords:
                if kw['name'].lower() == keyword_name.lower():
                    return kw
            
            return None
            
        except Exception as e:
            logger.warning(f"Could not get keyword details: {e}")
            return None
    
    def get_locator_format_guide(self) -> str:
        """
        Extract locator format information from library documentation.
        
        Returns:
            Formatted string with locator format guide
        """
        try:
            doc_data = self.get_library_documentation()
            intro = doc_data.get('doc', '')
            
            # Look for locator-related documentation
            if 'locator' in intro.lower() or 'selector' in intro.lower():
                # Extract relevant sections
                lines = intro.split('\n')
                locator_lines = []
                in_locator_section = False
                
                for line in lines:
                    if 'locator' in line.lower() or 'selector' in line.lower():
                        in_locator_section = True
                    
                    if in_locator_section:
                        locator_lines.append(line)
                        
                        # Stop after a reasonable amount
                        if len(locator_lines) > 20:
                            break
                
                if locator_lines:
                    return '\n'.join(locator_lines)
            
            # Fallback: generic guide
            return self._get_generic_locator_guide()
            
        except Exception as e:
            logger.warning(f"Could not extract locator guide: {e}")
            return self._get_generic_locator_guide()
    
    def _get_generic_locator_guide(self) -> str:
        """Fallback locator guide if extraction fails."""
        if self.library_name == 'Browser':
            return """
**BROWSER LIBRARY LOCATOR FORMATS:**
- id=<value>          → Find by ID
- text=<value>        → Find by visible text
- role=<role>[name="<name>"]  → Find by ARIA role
- data-testid=<value> → Find by test ID
- <css_selector>      → CSS selector (no prefix)
- xpath=<expression>  → XPath (no prefix)
"""
        else:  # SeleniumLibrary
            return """
**SELENIUMLIBRARY LOCATOR FORMATS:**
- id=<value>          → Find by ID
- name=<value>        → Find by name
- xpath=<expression>  → Find by XPath
- css=<selector>      → Find by CSS
"""


def get_dynamic_keywords(library_name: str, max_keywords: int = 20) -> str:
    """
    Convenience function to get keyword summary for a library.
    
    Args:
        library_name: Name of the Robot Framework library
        max_keywords: Maximum number of keywords to include
        
    Returns:
        Formatted keyword documentation string
    """
    try:
        doc_extractor = DynamicLibraryDocumentation(library_name)
        return doc_extractor.get_keywords_summary(max_keywords)
    except Exception as e:
        logger.error(f"Failed to get dynamic keywords for {library_name}: {e}")
        return f"--- {library_name.upper()} KEYWORDS ---\n\nFailed to load documentation: {str(e)}\n"


def get_all_keywords_list(library_name: str) -> str:
    """
    Get a complete list of all available keywords (names only).
    This is lightweight and can be included in context to show LLM what's available.
    
    Args:
        library_name: Name of the Robot Framework library
        
    Returns:
        Formatted string with all keyword names
    """
    try:
        doc_extractor = DynamicLibraryDocumentation(library_name)
        doc_data = doc_extractor.get_library_documentation()
        keywords = doc_data.get('keywords', [])
        
        # Filter public keywords
        public_keywords = [
            kw['name'] for kw in keywords 
            if not kw['name'].startswith('_') and 'deprecated' not in kw.get('doc', '').lower()
        ]
        
        # Sort alphabetically
        public_keywords.sort()
        
        # Format as compact list
        result = f"\n**ALL AVAILABLE KEYWORDS ({len(public_keywords)} total):**\n"
        result += ", ".join(public_keywords)
        result += "\n\n**Note:** If you need details about any keyword not shown above, "
        result += "you can use it - all keywords are available in the library.\n"
        
        return result
        
    except Exception as e:
        logger.warning(f"Could not get all keywords list: {e}")
        return ""
