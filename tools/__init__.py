"""
Tools package for Natural Language to Robot Framework.

This package contains:
- browser_use_service.py: Service for browser automation with BrowserUse
- browser_use_tool.py: Tool interface for CrewAI integration

The __init__.py automatically sets up the Python path to allow
imports from src.backend without manual sys.path manipulation.

This eliminates the need for manual sys.path.insert() in each file
and ensures consistent import behavior across all tools.
"""
import sys
from pathlib import Path

# Add project root to path for src.backend imports
# This allows: from src.backend.core.config import settings
_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# Package metadata
__version__ = "1.0.0"
__author__ = "Natural Language to Robot Framework Team"

# Note: With this __init__.py in place, you can now import from src.backend
# in any file within the tools package without manual path setup!
