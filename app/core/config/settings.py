import json
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # LLM settings
    llm_model: str = "deepseek-chat"
    llm_api_key: str
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 4096

    # Skeleton compression settings
    skeleton_head_chars: int = 40
    skeleton_tail_chars: int = 30
    skeleton_enable_rle: bool = True
    skeleton_rle_threshold: int = 3
    skeleton_max_rle_group: int = 10

    # Sliding window settings
    sliding_window_threshold: int = 500
    window_size: int = 300
    window_overlap: int = 50

    # Fuzzy anchor settings
    fuzzy_anchor_radius: int = 5
    fuzzy_min_similarity: float = 0.4

    # Logging
    log_level: str = "INFO"

    # Rate limiting
    rate_limit_max_requests: int = 10
    rate_limit_window_seconds: int = 3600

    # App identity
    app_name: str = "Constellation"
    app_version: str = "0.2.0"

    # CORS
    cors_allow_origins: List[str] = ["*"]
    cors_allow_credentials: bool = False

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _parse_cors_allow_origins(cls, value):
        if value in (None, ""):
            return ["*"]
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return ["*"]
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    origins = [str(item).strip() for item in parsed if str(item).strip()]
                    return origins or ["*"]
            origins = [item.strip() for item in stripped.split(",") if item.strip()]
            return origins or ["*"]
        if isinstance(value, list):
            origins = [str(item).strip() for item in value if str(item).strip()]
            return origins or ["*"]
        return value


settings = Settings()
