from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BACKEND_DIR.parent
ENV_FILES = tuple(path for path in (BACKEND_DIR / ".env", PROJECT_DIR / ".env") if path.exists())


class Settings(BaseSettings):
    app_name: str = "Darwinbox FDE Migration Agent"
    environment: str = "development"
    database_url: str = "sqlite:///./data/migration_agent.db"
    uploads_dir: str = "./data/uploads"
    llm_provider: str = "openrouter"
    llm_model: str = "openrouter/free"
    llm_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_api_key: str = ""
    target_api_url: str = "http://localhost:8001"
    target_api_public_url: str = "http://localhost:8001"
    frontend_origin: str = "http://localhost:5173"
    semantic_model: str = "BAAI/bge-small-en-v1.5"
    auto_mapping_threshold: float = 0.86
    auto_mapping_margin: float = 0.08
    auto_fallback_threshold: float = 0.60
    llm_timeout_seconds: float = 45.0
    max_llm_retries: int = 2

    model_config = SettingsConfigDict(env_file=ENV_FILES or None, extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
