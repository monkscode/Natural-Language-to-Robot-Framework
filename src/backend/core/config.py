import os
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

load_dotenv()

class Settings(BaseSettings):
    MODEL_PROVIDER: str = "online"
    GEMINI_API_KEY: str | None = None
    ONLINE_MODEL: str = "gemini-1.5-pro-latest"
    LOCAL_MODEL: str = "llama3"
    SECONDS_BETWEEN_API_CALLS: int = 0

    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'

settings = Settings()
