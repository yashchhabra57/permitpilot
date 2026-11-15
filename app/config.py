"""Runtime configuration, read from environment variables (or a local .env file)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="PERMITPILOT_", extra="ignore")

    database_url: str = "sqlite:///./permitpilot.db"
    # LLM extraction is optional. Without a key the rule-based extractor is used.
    anthropic_api_key: str | None = None
    llm_model: str = "claude-opus-5-5"
    # Job queue tuning
    job_lease_seconds: int = 60
    job_max_attempts: int = 5
    job_backoff_base_seconds: int = 30
    job_backoff_cap_seconds: int = 3600
    seed_on_startup: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
