"""Application settings, read from environment variables or a .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///data/salesagent.db"
    # Hard rule 5: real calls only when this is explicitly set to false.
    dry_run: bool = True
    call_window_start: str = "09:00"
    call_window_end: str = "17:00"
    call_timezone: str = "Europe/Amsterdam"
    max_call_attempts: int = 3
    kvk_api_key: str = ""
    kvk_api_base_url: str = "https://api.kvk.nl/api/v1"
    # Hard rule 7: retention periods in days. Defaults are provisional (open decision).
    retention_transcript_days: int = 90
    retention_contact_days: int = 365


@lru_cache
def get_settings() -> Settings:
    return Settings()
