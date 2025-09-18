import os
import yaml
from pathlib import Path
from typing import Dict, Any, Optional
from pydantic_settings import BaseSettings
from pydantic import Field, validator
from dotenv import load_dotenv

from .models.healing_models import HealingConfiguration, LocatorStrategy

load_dotenv()

class Settings(BaseSettings):
    MODEL_PROVIDER: str = "online"
    GEMINI_API_KEY: str | None = None
    ONLINE_MODEL: str = "gemini-1.5-pro-latest"
    LOCAL_MODEL: str = "llama3"
    SECONDS_BETWEEN_API_CALLS: int = 0
    
    # Self-healing configuration
    SELF_HEALING_ENABLED: bool = Field(default=True, description="Enable/disable self-healing globally")
    SELF_HEALING_CONFIG_PATH: str = Field(default="config/self_healing.yaml", description="Path to self-healing config file")

    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'

settings = Settings()
