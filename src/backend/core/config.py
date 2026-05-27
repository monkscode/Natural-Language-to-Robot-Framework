import os
import yaml
from pathlib import Path
from typing import Dict, Any, Optional
from pydantic_settings import BaseSettings
from pydantic import Field, validator
from dotenv import load_dotenv

load_dotenv("src/backend/.env")

class Settings(BaseSettings):
    # LLM Configuration
    # MODEL_PROVIDER controls which LLM backend is used:
    #   "vertex"  — Google Cloud Vertex AI (requires VERTEXAI_CREDENTIALS,
    #               VERTEXAI_PROJECT, VERTEXAI_LOCATION)
    #   "gemini"  — Google AI Studio (requires GEMINI_API_KEY)
    #   "local"   — Ollama (requires a running Ollama server)
    MODEL_PROVIDER: str = "vertex"
    GEMINI_API_KEY: str | None = None
    ONLINE_MODEL: str = "gemini-2.5-flash"
    LOCAL_MODEL: str = "llama3"

    # Vertex AI Configuration (only required when MODEL_PROVIDER=vertex)
    # VERTEXAI_CREDENTIALS is the env var LiteLLM reads to locate the service account
    # key file. Set it in .env (local dev) or via docker-compose.vertex.yml (Docker).
    VERTEXAI_PROJECT: str | None = None
    VERTEXAI_LOCATION: str | None = None
    
    # Note: SECONDS_BETWEEN_API_CALLS was removed during Phase 2 of codebase cleanup.
    # Rate limiting is no longer implemented as Google Gemini API has sufficient
    # rate limits (1500 RPM) for our use case. If rate limiting becomes necessary,
    # implement it at the API gateway level rather than wrapping individual LLM calls.
    
    # Service Configuration
    APP_PORT: int = Field(default=5000, description="Port for FastAPI service")
    BROWSER_USE_SERVICE_URL: str = Field(default="http://localhost:4999", description="URL for BrowserUse service")
    
    # Browser Configuration
    BROWSER_HEADLESS: bool = Field(default=True, description="Run browser in headless mode (no UI) for BrowserUse service")
    
    # Robot Framework Library Configuration
    ROBOT_LIBRARY: str = Field(default="selenium", description="Robot Framework library to use: 'selenium' or 'browser'")
    
    # Agent Retry Configuration
    MAX_AGENT_ITERATIONS: int = Field(default=3, description="Maximum iterations for agents with delegation enabled (retry attempts)")
    
    # Custom Actions Configuration
    ENABLE_CUSTOM_ACTIONS: bool = Field(default=True, description="Enable/disable custom actions for browser automation")
    CUSTOM_ACTION_TIMEOUT: int = Field(default=5, description="Timeout for custom action execution (in seconds)")
    MAX_LOCATOR_STRATEGIES: int = Field(default=21, description="Maximum number of locator strategies to try")
    TRACK_LLM_COSTS: bool = Field(default=True, description="Enable/disable LLM cost tracking and logging")
    
    # Optimization Configuration
    OPTIMIZATION_ENABLED: bool = Field(default=True, description="Enable/disable optimization system (pattern learning, ChromaDB)")
    OPTIMIZATION_CHROMA_DB_PATH: str = Field(default="./chroma_db", description="Path to ChromaDB storage directory")
    # Note: ChromaDB uses its default ONNX-based embedding function (no sentence-transformers/pytorch needed)
    OPTIMIZATION_KEYWORD_SEARCH_TOP_K: int = Field(default=3, description="Number of keywords to return from search")
    OPTIMIZATION_PATTERN_CONFIDENCE_THRESHOLD: float = Field(default=0.7, description="Minimum confidence for pattern prediction (0.0-1.0)")
    OPTIMIZATION_CONTEXT_PRUNING_ENABLED: bool = Field(default=True, description="Enable smart context pruning")
    OPTIMIZATION_CONTEXT_PRUNING_THRESHOLD: float = Field(default=0.6, description="Minimum confidence for category classification (0.0-1.0)")
    
    # Learning System Configuration (Adaptive Learning — Phase 1+)
    EXECUTION_MEMORY_DB: str = Field(default="./data/execution_memory.db", description="Path to execution memory SQLite database for the learning system")

    # LLM Observability Configuration (Enhancement #3)
    # OBSERVABILITY_BACKEND controls where OTel traces are exported:
    #   "sqlite"  — Built-in SQLite trace store (zero infra, default for pilots)
    #   "grafana" — Grafana Tempo via OTLP (requires Tempo in docker-compose)
    #   "otlp"    — Any OTel-compatible endpoint (Langfuse, Jaeger, Datadog, etc.)
    #   "none"    — Tracing disabled
    OBSERVABILITY_BACKEND: str = Field(
        default="sqlite",
        description="Trace export backend: sqlite | grafana | otlp | none",
    )
    OTLP_ENDPOINT: str = Field(
        default="http://localhost:4318",
        description="OTLP HTTP endpoint for grafana or otlp backends",
    )

    # Concurrency configuration
    MAX_CONCURRENT_WORKFLOWS: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum number of concurrent test generation/execution workflows"
    )

    @validator('MODEL_PROVIDER')
    def validate_model_provider(cls, v):
        """Validate that MODEL_PROVIDER is one of the supported providers."""
        if v.lower() not in ['gemini', 'vertex', 'local']:
            raise ValueError(f"MODEL_PROVIDER must be 'gemini', 'vertex', or 'local', got '{v}'")
        return v.lower()

    @validator('ROBOT_LIBRARY')
    def validate_robot_library(cls, v):
        """Validate that ROBOT_LIBRARY is either 'selenium' or 'browser'."""
        if v.lower() not in ['selenium', 'browser']:
            raise ValueError(f"ROBOT_LIBRARY must be 'selenium' or 'browser', got '{v}'")
        return v.lower()
    
    @validator('MAX_AGENT_ITERATIONS')
    def validate_max_iterations(cls, v):
        """Validate that MAX_AGENT_ITERATIONS is between 1 and 5."""
        if v < 1 or v > 5:
            raise ValueError(f"MAX_AGENT_ITERATIONS must be between 1 and 5, got {v}")
        return v
    
    @validator('CUSTOM_ACTION_TIMEOUT')
    def validate_custom_action_timeout(cls, v):
        """Validate that CUSTOM_ACTION_TIMEOUT is positive."""
        if v <= 0:
            raise ValueError(f"CUSTOM_ACTION_TIMEOUT must be positive, got {v}")
        return v
    
    @validator('MAX_LOCATOR_STRATEGIES')
    def validate_max_locator_strategies(cls, v):
        """Validate that MAX_LOCATOR_STRATEGIES is between 1 and 50."""
        if v < 1 or v > 50:
            raise ValueError(f"MAX_LOCATOR_STRATEGIES must be between 1 and 50, got {v}")
        return v
    
    @validator('OBSERVABILITY_BACKEND')
    def validate_observability_backend(cls, v):
        if v.lower() not in ('sqlite', 'grafana', 'otlp', 'none'):
            raise ValueError(f"OBSERVABILITY_BACKEND must be sqlite, grafana, otlp, or none, got '{v}'")
        return v.lower()

    @validator('OPTIMIZATION_PATTERN_CONFIDENCE_THRESHOLD', 'OPTIMIZATION_CONTEXT_PRUNING_THRESHOLD')
    def validate_confidence_threshold(cls, v):
        """Validate that confidence thresholds are between 0.0 and 1.0."""
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"Confidence threshold must be between 0.0 and 1.0, got {v}")
        return v

    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'
        extra = 'allow'  # Allow extra fields from .env file

settings = Settings()
