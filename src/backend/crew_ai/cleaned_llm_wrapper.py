"""
Cleaned LLM Wrapper - Intercepts and cleans LLM responses before CrewAI parsing.

This module provides wrapper classes that intercept LLM responses and clean
them before CrewAI's parser sees them. This prevents formatting errors from
breaking the workflow.

The wrapper is transparent - it behaves exactly like the original LLM but
with automatic output cleaning.
"""

import logging
from typing import Any, Dict, List, Optional
from crewai.llm import LLM
from langchain_ollama import OllamaLLM
from langchain_core.messages import BaseMessage, AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult, Generation

from .llm_output_cleaner import LLMOutputCleaner, formatting_monitor

logger = logging.getLogger(__name__)


class CleanedLLMWrapper(LLM):
    """
    Wrapper around CrewAI's LLM that cleans output before returning.
    
    This wrapper intercepts the LLM's responses and applies cleaning
    logic to fix common formatting issues (like extra text on Action lines).
    """
    
    def __init__(self, *args, **kwargs):
        """Initialize the wrapper with the same arguments as LLM."""
        super().__init__(*args, **kwargs)
        logger.info("🧹 Initialized CleanedLLMWrapper - will clean all LLM responses")
    
    def _generate(self, messages: List[BaseMessage], **kwargs) -> ChatResult:
        """
        Generate response and clean it before returning.
        
        This method intercepts the LLM's response and applies cleaning logic.
        """
        # Call the original _generate method
        result = super()._generate(messages, **kwargs)
        
        # Clean the response
        cleaned_result = self._clean_chat_result(result)
        
        return cleaned_result
    
    def _clean_chat_result(self, result: ChatResult) -> ChatResult:
        """
        Clean a ChatResult by applying output cleaning to all generations.
        
        Args:
            result: Original ChatResult from LLM
            
        Returns:
            Cleaned ChatResult with fixed formatting
        """
        if not result or not result.generations:
            return result
        
        cleaned_generations = []
        was_cleaned = False
        
        for generation in result.generations:
            if isinstance(generation, ChatGeneration) and generation.message:
                # Extract the text content
                original_text = generation.message.content
                
                # Clean it
                cleaned_text = LLMOutputCleaner.clean_output(original_text)
                
                # Check if cleaning was needed
                if cleaned_text != original_text:
                    was_cleaned = True
                    logger.debug(f"🧹 Cleaned LLM response (length: {len(original_text)} → {len(cleaned_text)})")
                
                # Create new message with cleaned content
                cleaned_message = AIMessage(content=cleaned_text)
                
                # Create new generation with cleaned message
                cleaned_generation = ChatGeneration(
                    message=cleaned_message,
                    generation_info=generation.generation_info
                )
                cleaned_generations.append(cleaned_generation)
            else:
                # Keep non-chat generations as-is
                cleaned_generations.append(generation)
        
        # Log to monitor
        formatting_monitor.log_response(was_cleaned=was_cleaned)
        
        # Create new result with cleaned generations
        return ChatResult(
            generations=cleaned_generations,
            llm_output=result.llm_output
        )


class CleanedOllamaLLMWrapper(OllamaLLM):
    """
    Wrapper around OllamaLLM that cleans output before returning.
    
    This wrapper provides the same cleaning functionality for local Ollama models.
    """
    
    def __init__(self, *args, **kwargs):
        """Initialize the wrapper with the same arguments as OllamaLLM."""
        super().__init__(*args, **kwargs)
        logger.info("🧹 Initialized CleanedOllamaLLMWrapper - will clean all LLM responses")
    
    def _generate(self, prompts: List[str], **kwargs) -> LLMResult:
        """
        Generate response and clean it before returning.
        
        This method intercepts the LLM's response and applies cleaning logic.
        """
        # Call the original _generate method
        result = super()._generate(prompts, **kwargs)
        
        # Clean the response
        cleaned_result = self._clean_llm_result(result)
        
        return cleaned_result
    
    def _clean_llm_result(self, result: LLMResult) -> LLMResult:
        """
        Clean an LLMResult by applying output cleaning to all generations.
        
        Args:
            result: Original LLMResult from LLM
            
        Returns:
            Cleaned LLMResult with fixed formatting
        """
        if not result or not result.generations:
            return result
        
        cleaned_generations_list = []
        was_cleaned = False
        
        for generation_list in result.generations:
            cleaned_generation_list = []
            
            for generation in generation_list:
                if isinstance(generation, Generation):
                    # Extract the text content
                    original_text = generation.text
                    
                    # Clean it
                    cleaned_text = LLMOutputCleaner.clean_output(original_text)
                    
                    # Check if cleaning was needed
                    if cleaned_text != original_text:
                        was_cleaned = True
                        logger.debug(f"🧹 Cleaned Ollama response (length: {len(original_text)} → {len(cleaned_text)})")
                    
                    # Create new generation with cleaned text
                    cleaned_generation = Generation(
                        text=cleaned_text,
                        generation_info=generation.generation_info
                    )
                    cleaned_generation_list.append(cleaned_generation)
                else:
                    # Keep other types as-is
                    cleaned_generation_list.append(generation)
            
            cleaned_generations_list.append(cleaned_generation_list)
        
        # Log to monitor
        formatting_monitor.log_response(was_cleaned=was_cleaned)
        
        # Create new result with cleaned generations
        return LLMResult(
            generations=cleaned_generations_list,
            llm_output=result.llm_output
        )


def get_cleaned_llm(model_provider: str, model_name: str, api_key: Optional[str] = None):
    """
    Get a cleaned LLM instance that automatically fixes formatting issues.
    
    This is a drop-in replacement for the original get_llm function,
    but returns wrapped instances that clean their output.
    
    Args:
        model_provider: "local" for Ollama, "online" for Gemini
        model_name: Model identifier (e.g., "llama3.1", "gemini-2.5-flash")
        api_key: API key for online models (optional, can use env var)
        
    Returns:
        Cleaned LLM wrapper instance
    """
    if model_provider == "local":
        logger.info(f"🧹 Creating CleanedOllamaLLMWrapper for model: {model_name}")
        return CleanedOllamaLLMWrapper(model=model_name)
    else:
        logger.info(f"🧹 Creating CleanedLLMWrapper for model: {model_name}")
        import os
        return CleanedLLMWrapper(
            api_key=api_key or os.getenv("GEMINI_API_KEY"),
            model=f"{model_name}",
            num_retries=3
        )
