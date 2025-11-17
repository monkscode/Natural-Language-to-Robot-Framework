"""
Embedder configuration abstraction for CrewAI knowledge base.

This module provides a flexible embedder configuration system supporting:
- Google (Gemini) embeddings (online)
- Ollama embeddings (local)
- SentenceTransformers embeddings (local)

The abstraction allows switching between embedding providers without
changing agent code, and supports future extensibility.
"""

import os
import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from enum import Enum

logger = logging.getLogger(__name__)


class EmbedderProvider(str, Enum):
    """Supported embedding providers."""
    GOOGLE = "google"
    OLLAMA = "ollama"
    SENTENCE_TRANSFORMERS = "sentence_transformers"


class EmbedderConfig(ABC):
    """
    Abstract base class for embedder configurations.
    
    All embedder implementations must inherit from this class and implement
    the to_crewai_config() method to return a configuration dict compatible
    with CrewAI's Agent embedder parameter.
    """
    
    @abstractmethod
    def to_crewai_config(self) -> Dict[str, Any]:
        """
        Convert embedder configuration to CrewAI format.
        
        Returns:
            Dict with 'provider' and 'config' keys suitable for Agent(embedder=...)
        """
        pass
    
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return human-readable provider name for logging."""
        pass


class GoogleEmbedderConfig(EmbedderConfig):
    """
    Google (Gemini) embeddings configuration.
    
    Uses Google's text-embedding-004 model via their API.
    Requires GOOGLE_API_KEY environment variable.
    
    Advantages:
    - High quality embeddings
    - Works well with Gemini LLMs
    - Reliable API with good rate limits
    
    Usage:
        config = GoogleEmbedderConfig()
        agent = Agent(..., embedder=config.to_crewai_config())
    """
    
    def __init__(
        self,
        model: str = "models/gemini-embedding-001",
        api_key: Optional[str] = None
    ):
        """
        Initialize Google embedder configuration.
        
        Args:
            model: Google embedding model name (default: text-embedding-004)
            api_key: Google API key (defaults to GOOGLE_API_KEY env var)
        """
        self.model = model
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        
        if not self.api_key:
            logger.warning(
                "GEMINI_API_KEY not found in environment. "
                "Google embedder may fail without API key."
            )
    
    def to_crewai_config(self) -> Dict[str, Any]:
        """Convert to CrewAI embedder configuration format."""
        config = {
            "provider": "google-generativeai",
            "config": {
                "model": self.model
            }
        }
        
        # Only include API key if explicitly set
        if self.api_key:
            config["config"]["api_key"] = self.api_key
            print("Using Google embedder with API key")
        else:
            print("Using Google embedder without API key")
        print(f"Using Google embedder with model {self.model}")
        return config
    
    @property
    def provider_name(self) -> str:
        return f"Google ({self.model})"


class OllamaEmbedderConfig(EmbedderConfig):
    """
    Ollama embeddings configuration.
    
    Uses local Ollama server for embeddings. Requires Ollama to be running.
    
    Advantages:
    - Fully local (no API calls)
    - No API costs
    - Privacy-preserving
    - Works offline
    
    Requirements:
    - Ollama must be installed and running
    - Embedding model must be pulled (e.g., ollama pull mxbai-embed-large)
    
    Usage:
        config = OllamaEmbedderConfig()
        agent = Agent(..., embedder=config.to_crewai_config())
    """
    
    def __init__(
        self,
        model: str = "mxbai-embed-large",
        url: str = "http://localhost:11434/api/embeddings"
    ):
        """
        Initialize Ollama embedder configuration.
        
        Args:
            model: Ollama embedding model name (default: mxbai-embed-large)
            url: Ollama API endpoint (default: http://localhost:11434/api/embeddings)
        """
        self.model = model
        self.url = url
    
    def to_crewai_config(self) -> Dict[str, Any]:
        """Convert to CrewAI embedder configuration format."""
        return {
            "provider": "ollama",
            "config": {
                "model": self.model,
                "url": self.url
            }
        }
    
    @property
    def provider_name(self) -> str:
        return f"Ollama ({self.model})"


class SentenceTransformersEmbedderConfig(EmbedderConfig):
    """
    SentenceTransformers embeddings configuration.
    
    Uses local SentenceTransformers library for embeddings.
    Provides high-quality local embeddings without external dependencies.
    
    Advantages:
    - Fully local (no API calls or servers needed)
    - No API costs
    - Privacy-preserving
    - Works offline
    - No separate server process required (vs Ollama)
    
    Requirements:
    - sentence-transformers package must be installed
    - First run will download model (~400MB for all-MiniLM-L6-v2)
    
    Popular models:
    - all-MiniLM-L6-v2: Fast, small (80MB), good quality
    - all-mpnet-base-v2: Best quality, larger (420MB), slower
    - paraphrase-MiniLM-L6-v2: Good for semantic similarity
    
    Usage:
        config = SentenceTransformersEmbedderConfig()
        agent = Agent(..., embedder=config.to_crewai_config())
    """
    
    def __init__(
        self,
        model: str = "all-MiniLM-L6-v2",
        device: Optional[str] = None
    ):
        """
        Initialize SentenceTransformers embedder configuration.
        
        Args:
            model: HuggingFace model name (default: all-MiniLM-L6-v2)
            device: Device to run on ('cuda', 'cpu', or None for auto-detect)
        """
        self.model = model
        self.device = device
    
    def to_crewai_config(self) -> Dict[str, Any]:
        """
        Convert to CrewAI embedder configuration format.
        
        Note: CrewAI may not natively support sentence-transformers provider.
        This configuration is prepared for custom integration or future support.
        For now, users may need to use Ollama or Google instead.
        """
        config = {
            "provider": "sentence-transformer",
            "config": {
                "model": self.model
            }
        }
        
        if self.device:
            config["config"]["device"] = self.device
        
        logger.warning(
            "SentenceTransformers provider may require custom integration with CrewAI. "
            "Consider using Ollama or Google for native support."
        )
        
        return config
    
    @property
    def provider_name(self) -> str:
        return f"SentenceTransformers ({self.model})"


def get_embedder_config(
    provider: str = "google",
    **kwargs
) -> EmbedderConfig:
    """
    Factory function to create embedder configuration based on provider.
    
    This is the recommended way to create embedder configs as it provides
    a simple interface and handles provider selection logic.
    
    Args:
        provider: Provider name ('google', 'ollama', 'sentence_transformers')
        **kwargs: Provider-specific configuration options
        
    Returns:
        EmbedderConfig instance for the specified provider
        
    Raises:
        ValueError: If provider is not supported
        
    Examples:
        # Google embeddings (default)
        config = get_embedder_config("google")
        
        # Ollama embeddings with custom model
        config = get_embedder_config("ollama", model="nomic-embed-text")
        
        # SentenceTransformers with GPU
        config = get_embedder_config("sentence_transformers", device="cuda")
    """
    provider = provider.lower()
    
    if provider == EmbedderProvider.GOOGLE:
        return GoogleEmbedderConfig(**kwargs)
    elif provider == EmbedderProvider.OLLAMA:
        return OllamaEmbedderConfig(**kwargs)
    elif provider == EmbedderProvider.SENTENCE_TRANSFORMERS:
        return SentenceTransformersEmbedderConfig(**kwargs)
    else:
        raise ValueError(
            f"Unsupported embedder provider: {provider}. "
            f"Supported providers: {[p.value for p in EmbedderProvider]}"
        )


def get_default_embedder_config() -> EmbedderConfig:
    """
    Get default embedder configuration based on environment.
    
    Priority:
    1. Use provider specified in EMBEDDER_PROVIDER env var
    2. Use Google if GOOGLE_API_KEY is available
    3. Fall back to Ollama (assumes local setup)
    
    Returns:
        EmbedderConfig instance
    """
    provider = os.getenv("EMBEDDER_PROVIDER", "").lower()
    
    if provider:
        logger.info(f"Using embedder provider from environment: {provider}")
        return get_embedder_config(provider)
    
    # Check if Google API key is available
    if os.getenv("GOOGLE_API_KEY"):
        logger.info("GOOGLE_API_KEY found, using Google embeddings")
        return GoogleEmbedderConfig()
    
    # Fall back to Ollama
    logger.info("No GOOGLE_API_KEY found, falling back to Ollama embeddings")
    logger.info("Make sure Ollama is running with: ollama pull mxbai-embed-large")
    return OllamaEmbedderConfig()
