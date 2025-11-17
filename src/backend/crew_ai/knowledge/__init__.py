"""
Knowledge base module for CrewAI agents.

This module provides embedder configurations for the knowledge base system,
supporting multiple embedding providers (Google, Ollama, SentenceTransformers).
"""

from .embedder_config import (
    EmbedderConfig,
    GoogleEmbedderConfig,
    OllamaEmbedderConfig,
    SentenceTransformersEmbedderConfig,
    get_embedder_config
)

__all__ = [
    'EmbedderConfig',
    'GoogleEmbedderConfig',
    'OllamaEmbedderConfig',
    'SentenceTransformersEmbedderConfig',
    'get_embedder_config'
]
