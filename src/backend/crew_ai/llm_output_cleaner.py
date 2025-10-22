"""
LLM Output Cleaner - Robust parsing for malformed agent responses.

This module provides a wrapper around LLM instances that cleans malformed
output before CrewAI parses it. This handles the common issue where LLMs
(especially Gemini) add extra text to Action lines, breaking CrewAI's parser.

Root Cause:
-----------
LLMs naturally want to explain their actions, leading to output like:
    "Action: batch_browser_automation` call with the collected elements..."
    
Instead of the required format:
    "Action: batch_browser_automation"
    "Action Input: {...}"

Solution:
---------
We intercept LLM responses and clean them using regex patterns before
CrewAI's parser sees them. This is similar to how we handled duplicate
entries - we create robust parsing that handles real-world LLM behavior.
"""

import re
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class LLMOutputCleaner:
    """
    Cleans malformed LLM output to ensure proper Action/Action Input format.
    
    This class provides static methods to detect and fix common formatting
    issues in LLM responses that would otherwise break CrewAI's parser.
    """
    
    # Patterns for detecting malformed Action/Using Tool lines
    ACTION_PATTERNS = [
        # Match: "Action: batch_browser_automation` call with..."
        r'(Action:\s*batch_browser_automation)[`\']?\s+[a-zA-Z].*',
        # Match: "Action: batch_browser_automation` and..."
        r'(Action:\s*batch_browser_automation)[`\']?\s+and\s+.*',
        # Match: "Action: vision_browser_automation` using..."
        r'(Action:\s*vision_browser_automation)[`\']?\s+[a-zA-Z].*',
        # Match: "Using Tool: batch_browser_automation` and..."
        r'(Using Tool:\s*batch_browser_automation)[`\']?\s+[a-zA-Z].*',
        # Match: "Using Tool: vision_browser_automation` with..."
        r'(Using Tool:\s*vision_browser_automation)[`\']?\s+[a-zA-Z].*',
        # Generic: "Action: <tool_name>` <extra text>"
        r'(Action:\s*[a-zA-Z_][a-zA-Z0-9_]*)[`\']?\s+[a-zA-Z].*',
        # Generic: "Using Tool: <tool_name>` <extra text>"
        r'(Using Tool:\s*[a-zA-Z_][a-zA-Z0-9_]*)[`\']?\s+[a-zA-Z].*',
    ]
    
    @staticmethod
    def clean_action_lines(text: str) -> str:
        """
        Clean malformed Action lines by removing extra text.
        
        Args:
            text: Raw LLM output that may contain malformed Action lines
            
        Returns:
            Cleaned text with proper Action line formatting
            
        Examples:
            Input:  "Action: batch_browser_automation` call with the elements..."
            Output: "Action: batch_browser_automation"
            
            Input:  "Action: vision_browser_automation` and Action Input using..."
            Output: "Action: vision_browser_automation"
        """
        if not isinstance(text, str):
            return text
        
        original_text = text
        lines = text.split('\n')
        cleaned_lines = []
        changes_made = False
        
        for i, line in enumerate(lines):
            stripped = line.strip()
            
            # Check if this line starts with "Action:" or "Using Tool:"
            if stripped.startswith('Action:') or stripped.startswith('Using Tool:'):
                # Try to extract clean action name
                cleaned_line = LLMOutputCleaner._clean_single_action_line(line)
                
                if cleaned_line != line:
                    changes_made = True
                    logger.debug(f"🧹 Cleaned Action/Tool line {i+1}:")
                    logger.debug(f"   Before: {line[:100]}...")
                    logger.debug(f"   After:  {cleaned_line}")
                
                cleaned_lines.append(cleaned_line)
            else:
                cleaned_lines.append(line)
        
        cleaned_text = '\n'.join(cleaned_lines)
        
        if changes_made:
            logger.info(f"✅ Cleaned LLM output: removed extra text from Action lines")
            logger.debug(f"   Original length: {len(original_text)} chars")
            logger.debug(f"   Cleaned length:  {len(cleaned_text)} chars")
        
        return cleaned_text
    
    @staticmethod
    def _clean_single_action_line(line: str) -> str:
        """
        Clean a single Action/Using Tool line by extracting just the action name.
        
        Args:
            line: A line that starts with "Action:" or "Using Tool:"
            
        Returns:
            Cleaned line with format "Action: <action_name>" or "Using Tool: <tool_name>"
        """
        # Try each pattern
        for pattern in LLMOutputCleaner.ACTION_PATTERNS:
            match = re.match(pattern, line.strip())
            if match:
                # Extract the clean "Action: <name>" or "Using Tool: <name>" part
                clean_action = match.group(1)
                # Remove any trailing backticks or quotes
                clean_action = re.sub(r'[`\'"]$', '', clean_action)
                return clean_action
        
        # If no pattern matched, try simple extractions
        # Match "Action: <word>" and stop at first non-word character
        simple_match = re.match(r'(Action:\s*[a-zA-Z_][a-zA-Z0-9_]*)', line.strip())
        if simple_match:
            return simple_match.group(1)
        
        # Match "Using Tool: <word>" and stop at first non-word character
        tool_match = re.match(r'(Using Tool:\s*[a-zA-Z_][a-zA-Z0-9_]*)', line.strip())
        if tool_match:
            return tool_match.group(1)
        
        # If all else fails, return original line
        return line
    
    @staticmethod
    def clean_action_input_lines(text: str) -> str:
        """
        Clean malformed Action Input lines by ensuring proper JSON formatting.
        
        Args:
            text: Raw LLM output that may contain malformed Action Input lines
            
        Returns:
            Cleaned text with proper Action Input formatting
        """
        if not isinstance(text, str):
            return text
        
        lines = text.split('\n')
        cleaned_lines = []
        
        for line in lines:
            stripped = line.strip()
            
            # Check if this line starts with "Action Input:"
            if stripped.startswith('Action Input:'):
                # Ensure proper JSON formatting
                if '{' in line:
                    # Extract JSON part (everything from first { onwards)
                    json_start = line.find('{')
                    # Find the end of JSON (last } on this line or next lines)
                    json_part = line[json_start:]
                    cleaned_line = f'Action Input: {json_part}'
                    cleaned_lines.append(cleaned_line)
                else:
                    # No JSON found, keep as-is
                    cleaned_lines.append(line)
            else:
                cleaned_lines.append(line)
        
        return '\n'.join(cleaned_lines)
    
    @staticmethod
    def clean_output(text: str) -> str:
        """
        Apply all cleaning operations to LLM output.
        
        This is the main entry point for cleaning LLM responses.
        
        Args:
            text: Raw LLM output
            
        Returns:
            Fully cleaned output ready for CrewAI parsing
        """
        if not isinstance(text, str):
            return text
        
        # Apply all cleaning operations in sequence
        text = LLMOutputCleaner.clean_action_lines(text)
        text = LLMOutputCleaner.clean_action_input_lines(text)
        
        return text
    
    @staticmethod
    def is_formatting_error(error_msg: str) -> bool:
        """
        Detect if an error is likely due to LLM output formatting issues.
        
        Args:
            error_msg: Error message to analyze
            
        Returns:
            True if this looks like a formatting error
        """
        error_lower = error_msg.lower()
        
        formatting_indicators = [
            "Action" in error_msg and "don't exist" in error_msg,
            "batch_browser_automation" in error_msg and "call with" in error_msg,
            "vision_browser_automation" in error_msg and "using" in error_msg,
            "Action Input" in error_msg and "using" in error_msg,
            "partial sentence" in error_lower,
            "extra text" in error_lower,
            "not properly formatted" in error_lower,
            "backtick" in error_lower,
        ]
        
        return any(formatting_indicators)


class LLMFormattingMonitor:
    """
    Monitor LLM formatting issues and track success rates.
    
    This class helps us understand how often formatting issues occur
    and whether our cleaning logic is effective.
    """
    
    def __init__(self):
        self.total_responses = 0
        self.cleaned_responses = 0
        self.formatting_errors_detected = 0
        self.formatting_errors_recovered = 0
    
    def log_response(self, was_cleaned: bool = False):
        """Log an LLM response."""
        self.total_responses += 1
        if was_cleaned:
            self.cleaned_responses += 1
    
    def log_formatting_error(self, was_recovered: bool = False):
        """Log a formatting error."""
        self.formatting_errors_detected += 1
        if was_recovered:
            self.formatting_errors_recovered += 1
    
    def get_stats(self) -> str:
        """Get formatted statistics string."""
        if self.total_responses == 0:
            return "No LLM responses processed yet"
        
        clean_rate = (self.cleaned_responses / self.total_responses) * 100
        
        if self.formatting_errors_detected > 0:
            recovery_rate = (self.formatting_errors_recovered / self.formatting_errors_detected) * 100
            return (
                f"LLM Responses: {self.total_responses} total, "
                f"{self.cleaned_responses} cleaned ({clean_rate:.1f}%), "
                f"Errors: {self.formatting_errors_detected} detected, "
                f"{self.formatting_errors_recovered} recovered ({recovery_rate:.1f}%)"
            )
        else:
            return (
                f"LLM Responses: {self.total_responses} total, "
                f"{self.cleaned_responses} cleaned ({clean_rate:.1f}%), "
                f"No formatting errors detected"
            )


# Global monitor instance
formatting_monitor = LLMFormattingMonitor()
