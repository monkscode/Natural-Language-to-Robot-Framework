"""
LLM Output Cleaner - Utility functions for LLM output formatting.

This module provides:
1. Action/Action Input line cleaning (LLMs add extra text to tool calls)
2. Formatting error detection (for error handling)
3. Response monitoring (for debugging)

NOTE: JSON extraction and Robot Framework code wrapping are now handled by 
guardrails in tasks.py, which is a more robust solution.
"""

import re
import logging
from typing import Optional

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
        """
        if not text:
            return text

        lines = text.split('\n')
        cleaned_lines = []

        for line in lines:
            # Only process lines that look like Action or Using Tool lines
            stripped = line.strip()
            if stripped.startswith(('Action:', 'Using Tool:')):
                cleaned_line = LLMOutputCleaner._clean_single_action_line(line)
                cleaned_lines.append(cleaned_line)
            else:
                cleaned_lines.append(line)

        return '\n'.join(cleaned_lines)

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
            match = re.match(pattern, line.strip(), re.IGNORECASE)
            if match:
                # Return just the captured group (proper Action format)
                cleaned = match.group(1)
                if cleaned != line.strip():
                    logger.debug(f"🧹 Cleaned action line: '{line.strip()[:50]}...' → '{cleaned}'")
                return cleaned

        # No pattern matched, return original
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
        if not text:
            return text

        # Pattern: "Action Input:" followed by extra text before the JSON
        # e.g., "Action Input: Here's the input: {...}"
        pattern = r'(Action Input:\s*)(?:Here\'?s?[^{]*|The input[^{]*|I\'ll use[^{]*)(\{)'
        
        def replacer(match):
            return match.group(1) + match.group(2)
        
        cleaned = re.sub(pattern, replacer, text, flags=re.IGNORECASE)
        
        if cleaned != text:
            logger.debug("🧹 Cleaned Action Input line (removed prefix before JSON)")
        
        return cleaned

    @staticmethod
    def clean_output(text: str) -> str:
        """
        Apply all Action/ActionInput cleaning operations to LLM output.
        
        This is the main entry point for cleaning LLM responses in wrappers.
        Fixes formatting issues that would break CrewAI's parser.
        
        Note: Robot Framework code cleaning is handled by guardrails in tasks.py,
        not here. This method focuses only on Action/ActionInput formatting.
        
        Args:
            text: Raw LLM output
            
        Returns:
            Cleaned output ready for CrewAI parsing
        """
        if not isinstance(text, str):
            return text
        
        # Apply Action/ActionInput cleaning
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
        formatting_indicators = [
            'invalid format',
            'expected string or bytes-like object',
            'could not parse',
            'json',
            'parsing',
            'malformed',
            'unexpected',
            'invalid action',
            'action input',
            'actionparsingerror',
            'no completion choices',
        ]
        
        error_lower = error_msg.lower()
        return any(indicator in error_lower for indicator in formatting_indicators)


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
            recovery_rate = (self.formatting_errors_recovered /
                             self.formatting_errors_detected) * 100
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
