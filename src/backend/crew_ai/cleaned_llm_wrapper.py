"""
LLM Wrapper - LLM instantiation with output cleaning for CrewAI agents.

This module provides:
1. CleanedLLMWrapper - For online models (Gemini) with Action/ActionInput cleaning
2. CleanedOllamaLLMWrapper - For local models (Ollama) with Action/ActionInput cleaning

The wrapper is transparent - it behaves exactly like the original LLM but
with automatic output cleaning.

RATE LIMITING: Includes a global rate limiter for Gemini Free Tier (5 RPM).
"""

import logging
import time
import os
import re
from threading import Lock
from typing import Any, Dict, List, Optional
from crewai.llm import LLM
from langchain_ollama import OllamaLLM
from langchain_core.messages import BaseMessage, AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult, Generation

from .llm_output_cleaner import LLMOutputCleaner, formatting_monitor

logger = logging.getLogger(__name__)


class DynamicRateLimitHandler:
    """
    Dynamic rate limit handler that respects API-provided retry delays.
    
    Instead of using hardcoded intervals, this handler:
    1. Attempts the API call
    2. If 429 error occurs, extracts retryDelay from the error response
    3. Waits the specified time and retries
    
    Configuration via environment variables:
    - DISABLE_RATE_LIMIT: Set to "true" to disable retry handling entirely
    - LLM_MAX_RETRIES: Maximum retry attempts (default: 3)
    """
    
    @staticmethod
    def extract_retry_delay(error_message: str) -> Optional[float]:
        """
        Extract retryDelay from API error message.
        
        Looks for patterns like:
        - "retryDelay": "50s"
        - "Please retry in 42.284326757s"
        """
        # Pattern 1: "retryDelay": "50s"
        match = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)\s*s?"', error_message)
        if match:
            return float(match.group(1))
        
        # Pattern 2: Please retry in Xs
        match = re.search(r'retry in (\d+(?:\.\d+)?)\s*s', error_message, re.IGNORECASE)
        if match:
            return float(match.group(1))
        
        return None
    
    @staticmethod
    def is_rate_limit_error(exception) -> bool:
        """Check if exception is a rate limit (429) error."""
        error_str = str(exception).lower()
        return '429' in error_str or 'rate' in error_str or 'quota' in error_str


class CleanedLLMWrapper(LLM):
    """
    Wrapper around CrewAI's LLM that cleans Action/ActionInput lines.
    
    This wrapper intercepts LLM responses and applies cleaning to fix formatting
    issues that would break CrewAI's parser. Specifically:
    - Fixes 'Action: tool_name` extra text' → 'Action: tool_name'
    - Fixes 'Action Input: prefix {...}' → 'Action Input: {...}'
    
    This prevents parsing failures and saves retry costs.
    
    Rate limiting is NOT handled here - LiteLLM handles it internally with num_retries.
    RF code cleaning is NOT handled here - guardrails handle it in tasks.py.
    """
    
    def __init__(self, *args, **kwargs):
        """Initialize the wrapper with the same arguments as LLM."""
        super().__init__(*args, **kwargs)
        logger.info("🧹 Initialized CleanedLLMWrapper - will clean Action/ActionInput lines")
    
    def call(self, messages, *args, **kwargs):
        """
        Override call() to handle rate limit errors with dynamic retry.
        
        CrewAI uses call() -> _handle_non_streaming_response() -> litellm.completion().
        We intercept at call() level to catch and handle 429 errors.
        """
        # Check if rate limit handling is disabled
        if os.getenv("DISABLE_RATE_LIMIT", "").lower() == "true":
            return super().call(messages, *args, **kwargs)
        
        max_retries = int(os.getenv("LLM_MAX_RETRIES", "3"))
        
        for attempt in range(max_retries + 1):
            try:
                return super().call(messages, *args, **kwargs)
            except Exception as e:
                if not DynamicRateLimitHandler.is_rate_limit_error(e):
                    raise  # Re-raise non-rate-limit errors
                
                if attempt >= max_retries:
                    logger.error(f"❌ Rate limit: Max retries ({max_retries}) exceeded")
                    raise
                
                # Extract retry delay from error
                error_str = str(e)
                retry_delay = DynamicRateLimitHandler.extract_retry_delay(error_str)
                
                if retry_delay is None:
                    # Default fallback if we can't parse the delay
                    retry_delay = 60.0
                    logger.warning(f"⚠️ Could not parse retryDelay, using default {retry_delay}s")
                
                logger.info(f"⏱️ Rate limit hit (attempt {attempt + 1}/{max_retries + 1}). "
                           f"Waiting {retry_delay:.1f}s as specified by API...")
                time.sleep(retry_delay)
        
        # Should not reach here, but just in case
        return super().call(messages, *args, **kwargs)
    
    def _generate(self, messages: List[BaseMessage], **kwargs) -> ChatResult:
        """
        Generate response and clean Action/ActionInput lines before returning.
        """
        # Call the original _generate method
        result = super()._generate(messages, **kwargs)
        
        # Clean the response
        cleaned_result = self._clean_chat_result(result)
        
        return cleaned_result
    
    def _clean_chat_result(self, result: ChatResult) -> ChatResult:
        """
        Clean a ChatResult by applying output cleaning to all generations.
        """
        if not result or not result.generations:
            return result
        
        cleaned_generations = []
        was_cleaned = False
        
        for generation in result.generations:
            if isinstance(generation, ChatGeneration) and generation.message:
                original_text = generation.message.content
                
                # Clean Action/ActionInput formatting issues
                cleaned_text = LLMOutputCleaner.clean_output(original_text)
                
                if cleaned_text != original_text:
                    was_cleaned = True
                    logger.debug(f"🧹 Cleaned LLM response (length: {len(original_text)} → {len(cleaned_text)})")
                
                # Create new message with cleaned content
                cleaned_message = AIMessage(content=cleaned_text)
                cleaned_generation = ChatGeneration(
                    message=cleaned_message,
                    generation_info=generation.generation_info
                )
                cleaned_generations.append(cleaned_generation)
            else:
                cleaned_generations.append(generation)
        
        # Log to monitor for debugging
        formatting_monitor.log_response(was_cleaned=was_cleaned)
        
        return ChatResult(
            generations=cleaned_generations,
            llm_output=result.llm_output
        )


class CleanedOllamaLLMWrapper(OllamaLLM):
    """
    Wrapper around OllamaLLM that cleans Action/ActionInput lines.
    
    Provides the same cleaning functionality for local Ollama models.
    """
    
    def __init__(self, *args, **kwargs):
        """Initialize the wrapper with the same arguments as OllamaLLM."""
        super().__init__(*args, **kwargs)
        logger.info("🧹 Initialized CleanedOllamaLLMWrapper - will clean Action/ActionInput lines")
    
    def _generate(self, prompts: List[str], **kwargs) -> LLMResult:
        """
        Generate response and clean Action/ActionInput lines before returning.
        """
        result = super()._generate(prompts, **kwargs)
        cleaned_result = self._clean_llm_result(result)
        return cleaned_result
    
    def _clean_llm_result(self, result: LLMResult) -> LLMResult:
        """
        Clean an LLMResult by applying output cleaning to all generations.
        """
        if not result or not result.generations:
            return result
        
        cleaned_generations_list = []
        was_cleaned = False
        
        for generation_list in result.generations:
            cleaned_generation_list = []
            
            for generation in generation_list:
                if isinstance(generation, Generation):
                    original_text = generation.text
                    
                    # Clean Action/ActionInput formatting issues
                    cleaned_text = LLMOutputCleaner.clean_output(original_text)
                    
                    if cleaned_text != original_text:
                        was_cleaned = True
                        logger.debug(f"🧹 Cleaned Ollama response (length: {len(original_text)} → {len(cleaned_text)})")
                    
                    cleaned_generation = Generation(
                        text=cleaned_text,
                        generation_info=generation.generation_info
                    )
                    cleaned_generation_list.append(cleaned_generation)
                else:
                    cleaned_generation_list.append(generation)
            
            cleaned_generations_list.append(cleaned_generation_list)
        
        # Log to monitor for debugging
        formatting_monitor.log_response(was_cleaned=was_cleaned)
        
        return LLMResult(
            generations=cleaned_generations_list,
            llm_output=result.llm_output
        )


def get_llm(model_provider: str, model_name: str, api_key: Optional[str] = None):
    """
    Get a cleaned LLM instance that automatically fixes Action/ActionInput formatting.
    
    The returned wrapper:
    - Cleans 'Action: tool_name` extra text' → 'Action: tool_name'  
    - Cleans 'Action Input: prefix {...}' → 'Action Input: {...}'
    - Prevents parser failures that would cause costly retries
    
    Rate limiting is handled automatically by LiteLLM (CrewAI's internal LLM layer).
    Robot Framework code cleaning is handled by guardrails in tasks.py.
    
    Args:
        model_provider: "local" for Ollama, "online" for Gemini
        model_name: Model identifier (e.g., "llama3.1", "gemini-2.5-flash")
        api_key: API key for online models (optional, can use env var)
        
    Returns:
        Cleaned LLM wrapper instance ready for use with CrewAI
    """
    if model_provider == "local":
        logger.info(f"🧹 Creating CleanedOllamaLLMWrapper for model: {model_name}")
        return CleanedOllamaLLMWrapper(model=model_name)
    
    # Create cleaned wrapper for online provider
    logger.info(f"🧹 Creating CleanedLLMWrapper for model: {model_name}")
    return CleanedLLMWrapper(
        api_key=api_key or os.getenv("GEMINI_API_KEY"),
        model=model_name,
        num_retries=3  # LiteLLM internal retry for transient API errors (429, 503, etc.)
    )
