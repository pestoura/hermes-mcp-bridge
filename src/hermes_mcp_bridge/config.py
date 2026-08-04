"""Environment-backed bridge configuration."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    hermes_api_base_url: str = "http://127.0.0.1:8642"
    hermes_api_key: SecretStr
    hermes_model: str = "hermes-agent"
    hermes_request_timeout_seconds: float = Field(default=30.0, gt=0)
    hermes_run_poll_interval_seconds: float = Field(default=1.0, gt=0)
    hermes_run_default_wait_seconds: float = Field(default=45.0, ge=0)
    hermes_run_max_wait_seconds: float = Field(default=7200.0, gt=0)
    hermes_progress_interval_seconds: float = Field(default=15.0, gt=0)
    hermes_event_stream_connect_timeout_seconds: float = Field(default=30.0, gt=0)

    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8765, ge=1, le=65535)
    mcp_path: str = "/mcp"
    log_level: str = "INFO"
    bridge_state_db_path: str = "/var/lib/hermes-mcp-bridge/state.sqlite3"
    bridge_version: str = "0.8.0"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance."""

    return Settings()  # type: ignore[call-arg]
