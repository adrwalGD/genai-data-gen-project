"""Application settings loaded from environment variables / `.env` (pydantic-settings).

This is the ONLY place where the Gemini model id may appear as a literal (CLAUDE.md rule 2).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Gemini on Vertex AI (ADC auth; no API keys)
    google_cloud_project: str = "gd-gcp-gridu-genai"
    google_cloud_location: str = "us-central1"
    gemini_model: str = "gemini-2.5-flash"
    gemini_thinking_budget: int = Field(default=0, ge=0)
    llm_max_concurrency: int = Field(default=4, ge=1, le=16)
    llm_timeout_s: float = Field(default=120.0, gt=0)
    llm_max_retries: int = Field(default=5, ge=0)

    # Langfuse (optional)
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_base_url: str = "https://cloud.langfuse.com"

    # Storage
    database_url: str = "postgresql://datagen:datagen@localhost:5432/datagen"
    data_dir: Path = Path("data")

    # Generation / querying limits
    default_rows_per_table: int = Field(default=100, ge=1)
    max_rows_per_table: int = Field(default=5000, ge=1)
    sql_row_limit: int = Field(default=500, ge=1)
    sql_timeout_ms: int = Field(default=15_000, ge=100)

    debug: bool = False

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def datasets_dir(self) -> Path:
        return self.data_dir / "datasets"


@lru_cache
def get_settings() -> Settings:
    """Process-wide cached settings. Tests construct `Settings()` directly instead."""
    return Settings()
