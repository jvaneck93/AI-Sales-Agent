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
    # Minimum hours between two attempts at the same company in one round.
    min_hours_between_attempts: int = 24
    # Call fatigue across all brands: max conversations per company per 90 days.
    max_contacts_per_quarter: int = 3
    # Decision 27-11-2026: only bv and nv are cold-called. Comma separated.
    callable_legal_forms: str = "bv,nv"
    kvk_api_key: str = ""
    kvk_api_base_url: str = "https://api.kvk.nl/api/v1"
    # Hard rule 7: retention periods in days. Defaults are provisional (open decision).
    retention_transcript_days: int = 90
    retention_contact_days: int = 365

    # External services. Empty key = mock implementation is used.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"
    research_max_age_days: int = 30
    elevenlabs_api_key: str = ""
    elevenlabs_agent_id: str = ""
    elevenlabs_phone_number_id: str = ""
    elevenlabs_webhook_secret: str = ""
    # Shared secret the voice agent sends to our tool endpoints (header X-Tool-Secret).
    tool_secret: str = ""
    # Numbers that must never be called for real (comma separated, E.164).
    test_block_numbers: str = ""
    company_name: str = "Belbureau"
    agent_name: str = "Robin"

    @property
    def callable_forms(self) -> frozenset[str]:
        return frozenset(f.strip().lower() for f in self.callable_legal_forms.split(",") if f)


@lru_cache
def get_settings() -> Settings:
    return Settings()
