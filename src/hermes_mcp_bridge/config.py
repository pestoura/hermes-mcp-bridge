"""Environment-backed bridge configuration.

Secret-bearing settings support the Docker-secrets convention: ``<NAME>_FILE``
points at a mounted file and takes precedence over ``<NAME>``. Values are read
at settings construction; paths are never logged or echoed back.
"""

import os
from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .secretfiles import read_secret


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    hermes_api_base_url: str = "http://127.0.0.1:8642"
    hermes_api_key: SecretStr = SecretStr("")
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
    bridge_version: str = "0.9.0"

    @model_validator(mode="after")
    def _resolve_file_backed_secrets(self) -> "Settings":
        """Apply ``HERMES_API_KEY_FILE`` precedence and require a key."""

        if os.environ.get("HERMES_API_KEY_FILE"):
            value = read_secret("HERMES_API_KEY")
            if value:
                object.__setattr__(self, "hermes_api_key", SecretStr(value))
        if not self.hermes_api_key.get_secret_value():
            raise ValueError("HERMES_API_KEY (or HERMES_API_KEY_FILE) must be configured")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance."""

    return Settings()  # type: ignore[call-arg]
