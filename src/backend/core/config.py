import os
import yaml
from pathlib import Path
from typing import Dict, Any, Optional
from pydantic_settings import BaseSettings
from pydantic import Field, validator
from dotenv import load_dotenv

from .models.healing_models import HealingConfiguration, LocatorStrategy

load_dotenv("src/backend/.env")

class Settings(BaseSettings):
    # LLM Configuration
    MODEL_PROVIDER: str = "online"  # "online" for Gemini, "local" for Ollama
    GEMINI_API_KEY: str | None = None
    ONLINE_MODEL: str = "gemini/gemini-2.5-flash"
    LOCAL_MODEL: str = "llama3"
    
    # Note: SECONDS_BETWEEN_API_CALLS was removed during Phase 2 of codebase cleanup.
    # Rate limiting is no longer implemented as Google Gemini API has sufficient
    # rate limits (1500 RPM) for our use case. If rate limiting becomes necessary,
    # implement it at the API gateway level rather than wrapping individual LLM calls.
    
    # Service Configuration
    APP_PORT: int = Field(default=5000, description="Port for FastAPI service")
    BROWSER_USE_SERVICE_URL: str = Field(default="http://localhost:4999", description="URL for BrowserUse service")
    
    # Robot Framework Library Configuration
    ROBOT_LIBRARY: str = Field(default="selenium", description="Robot Framework library to use: 'selenium' or 'browser'")
    
    # Self-healing configuration
    SELF_HEALING_ENABLED: bool = Field(default=True, description="Enable/disable self-healing globally")
    SELF_HEALING_CONFIG_PATH: str = Field(default="config/self_healing.yaml", description="Path to self-healing config file")
    
    @validator('ROBOT_LIBRARY')
    def validate_robot_library(cls, v):
        """Validate that ROBOT_LIBRARY is either 'selenium' or 'browser'."""
        if v.lower() not in ['selenium', 'browser']:
            raise ValueError(f"ROBOT_LIBRARY must be 'selenium' or 'browser', got '{v}'")
        return v.lower()

    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'
        extra = 'allow'  # Allow extra fields from .env file

settings = Settings()
