"""
LLM Wrapper - LLM instantiation with output cleaning for CrewAI agents.

This module provides:
1. CleanedLLMWrapper - For online models (Gemini) with Action/ActionInput cleaning
2. CleanedOllamaLLMWrapper - For local models (Ollama) with Action/ActionInput cleaning

The wrappers intercept LLM responses and clean them before CrewAI's parser sees them.
This prevents Action/ActionInput parsing errors that would cause retries.

Rate limiting is handled automatically by LiteLLM (used internally by CrewAI).
Robot Framework code cleaning is handled by guardrails in tasks.py, not here.
"""

import logging
import os
from typing import Any, List, Optional
from crewai.llm import LLM
from langchain_ollama import OllamaLLM
from langchain_core.messages import BaseMessage, AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult, Generation

from .llm_output_cleaner import LLMOutputCleaner, formatting_monitor

logger = logging.getLogger(__name__)


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
